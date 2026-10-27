import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness_eval.doctor import (
    CommandResult,
    DoctorStatus,
    Probe,
    SystemProbe,
    run_doctor,
)
from harness_eval.sandbox import TrustProfile

T1 = TrustProfile.T1_REVIEWED_STATIC
T2 = TrustProfile.T2_APPROVED_EXECUTABLE


@dataclass(frozen=True)
class FakeProbe(Probe):
    environment: Mapping[str, str]
    commands: Mapping[tuple[str, ...], CommandResult]
    executables: frozenset[str] = frozenset({"claude", "bench"})
    system: str = "Darwin"
    machine_name: str = "arm64"

    def which(self, command: str) -> str | None:
        return f"/fake/{command}" if command in self.executables else None

    def run(self, argv: Sequence[str]) -> CommandResult:
        normalized = (Path(argv[0]).name, *argv[1:])
        return self.commands.get(normalized, CommandResult(1, "", "not found"))

    def env(self) -> Mapping[str, str]:
        return self.environment

    def platform_system(self) -> str:
        return self.system

    def platform_machine(self) -> str:
        return self.machine_name


def _healthy_local_probe(environment: Mapping[str, str] | None = None) -> FakeProbe:
    return FakeProbe(
        environment=environment or {},
        commands={
            ("claude", "--version"): CommandResult(0, "2.1.220", ""),
            ("claude", "auth", "status", "--json"): CommandResult(
                0,
                json.dumps(
                    {
                        "loggedIn": True,
                        "subscriptionType": "pro",
                        "email": "private@example.com",
                    }
                ),
                "",
            ),
            ("bench", "--version"): CommandResult(0, "benchflow 0.6.7", ""),
        },
    )


def test_local_pro_doctor_accepts_subscription_without_api_fallback() -> None:
    report = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T1,
        probe=_healthy_local_probe(),
    )

    assert report.ready
    assert all(check.status is DoctorStatus.PASS for check in report.checks)


def test_local_pro_doctor_rejects_api_key_without_leaking_it() -> None:
    secret = "sk-ant-should-never-appear"
    report = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T1,
        probe=_healthy_local_probe({"ANTHROPIC_API_KEY": secret}),
    )

    serialized = report.to_json()
    assert not report.ready
    assert secret not in serialized
    assert "ANTHROPIC_API_KEY" in serialized


def test_probe_error_and_identity_fields_are_never_copied_to_report() -> None:
    secret = "秘密-aws-secret-値"
    probe = _healthy_local_probe()
    commands = dict(probe.commands)
    commands[("claude", "--version")] = CommandResult(1, "", secret)
    commands[("claude", "auth", "status", "--json")] = CommandResult(
        0,
        json.dumps(
            {
                "loggedIn": True,
                "subscriptionType": "pro",
                "email": secret,
            }
        ),
        "",
    )

    report = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T1,
        probe=FakeProbe(environment={}, commands=commands),
    )

    assert secret not in report.to_json()


def test_doctor_rejects_wrong_benchflow_version() -> None:
    probe = _healthy_local_probe()
    commands = dict(probe.commands)
    commands[("bench", "--version")] = CommandResult(0, "benchflow 0.6.8", "")

    report = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T1,
        probe=FakeProbe(environment={}, commands=commands),
    )

    assert not report.ready
    assert any(check.check_id == "benchflow.version" for check in report.failures)


def test_apple_container_missing_is_actionable_and_not_silently_downgraded() -> None:
    probe = _healthy_local_probe()
    report = run_doctor(
        profile_key="local-claude-pro",
        backend="apple-container",
        trust=T1,
        probe=FakeProbe(
            environment=probe.environment,
            commands=probe.commands,
            executables=frozenset({"claude", "bench"}),
        ),
    )

    assert not report.ready
    apple_check = next(
        check for check in report.checks if check.check_id == "sandbox.apple.command"
    )
    assert apple_check.status is DoctorStatus.FAIL
    assert "Install" in apple_check.message


def test_apple_container_supported_version_and_service_are_detected() -> None:
    probe = _healthy_local_probe()
    commands = dict(probe.commands)
    commands[("container", "system", "version", "--format", "json")] = CommandResult(
        0, '[{"appName":"container","version":"1.2.2"}]', ""
    )
    commands[("container", "ls")] = CommandResult(0, "", "")

    report = run_doctor(
        profile_key="local-claude-pro",
        backend="apple-container",
        trust=T1,
        probe=FakeProbe(
            environment={},
            commands=commands,
            executables=frozenset({"claude", "bench", "container"}),
        ),
    )

    assert report.ready


def test_malformed_auth_status_fails_closed() -> None:
    probe = _healthy_local_probe()
    commands = dict(probe.commands)
    commands[("claude", "auth", "status", "--json")] = CommandResult(0, "not-json", "")

    report = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T1,
        probe=FakeProbe(environment={}, commands=commands),
    )

    assert not report.ready
    assert any(check.check_id == "claude.subscription" for check in report.failures)


def test_corporate_bedrock_records_config_but_requires_a_live_aws_probe() -> None:
    secret = "aws-secret-must-not-appear"
    environment = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1",
        "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
        "BENCHFLOW_AGENTCORE_ROLE_ARN": "arn:aws:iam::123456789012:role/runtime",
        "BENCHFLOW_AGENTCORE_CODEBUILD_ROLE_ARN": (
            "arn:aws:iam::123456789012:role/build"
        ),
        "AWS_SECRET_ACCESS_KEY": secret,
    }
    report = run_doctor(
        profile_key="corporate-claude-bedrock",
        backend="agentcore",
        trust=T1,
        probe=FakeProbe(
            environment=environment,
            commands={("bench", "--version"): CommandResult(0, "benchflow 0.6.7", "")},
            executables=frozenset({"bench"}),
        ),
    )

    assert not report.ready
    assert any(
        check.check_id == "sandbox.agentcore.identity"
        and check.status is DoctorStatus.UNKNOWN
        for check in report.checks
    )
    assert secret not in report.to_json()


def test_bedrock_false_flag_or_direct_anthropic_key_fails_closed() -> None:
    direct_secret = "sk-ant-direct-route"
    environment = {
        "CLAUDE_CODE_USE_BEDROCK": "0",
        "AWS_REGION": "us-east-1",
        "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
        "ANTHROPIC_API_KEY": direct_secret,
        "BENCHFLOW_AGENTCORE_ROLE_ARN": "arn:aws:iam::123456789012:role/runtime",
        "BENCHFLOW_AGENTCORE_CODEBUILD_ROLE_ARN": (
            "arn:aws:iam::123456789012:role/build"
        ),
    }
    report = run_doctor(
        profile_key="corporate-claude-bedrock",
        backend="agentcore",
        trust=T1,
        probe=FakeProbe(
            environment=environment,
            commands={("bench", "--version"): CommandResult(0, "benchflow 0.6.7", "")},
            executables=frozenset({"bench"}),
        ),
    )

    assert not report.ready
    assert direct_secret not in report.to_json()


def test_unknown_profile_and_backend_fail_closed() -> None:
    probe = _healthy_local_probe()

    unknown_profile = run_doctor(
        profile_key="unknown",
        backend="native",
        trust=T1,
        probe=probe,
    )
    unknown_backend = run_doctor(
        profile_key="local-claude-pro",
        backend="unknown",
        trust=T1,
        probe=probe,
    )

    assert not unknown_profile.ready
    assert not unknown_backend.ready


def test_native_and_stock_agentcore_reject_executable_harnesses() -> None:
    local = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T2,
        probe=_healthy_local_probe(),
    )
    bedrock = run_doctor(
        profile_key="corporate-claude-bedrock",
        backend="agentcore",
        trust=T2,
        probe=FakeProbe(
            environment={
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_REGION": "us-east-1",
                "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
                "BENCHFLOW_AGENTCORE_ROLE_ARN": (
                    "arn:aws:iam::123456789012:role/runtime"
                ),
                "BENCHFLOW_AGENTCORE_CODEBUILD_ROLE_ARN": (
                    "arn:aws:iam::123456789012:role/build"
                ),
            },
            commands={("bench", "--version"): CommandResult(0, "benchflow 0.6.7", "")},
            executables=frozenset({"bench"}),
        ),
    )

    assert not local.ready
    assert not bedrock.ready
    assert any(check.check_id == "sandbox.authorization" for check in local.failures)
    assert any(check.check_id == "sandbox.authorization" for check in bedrock.failures)


def test_corporate_codex_is_explicitly_unavailable_in_phase_zero() -> None:
    report = run_doctor(
        profile_key="corporate-codex",
        backend="agentcore",
        trust=T1,
        probe=FakeProbe(
            environment={
                "BENCHFLOW_AGENTCORE_ROLE_ARN": (
                    "arn:aws:iam::123456789012:role/runtime"
                ),
                "BENCHFLOW_AGENTCORE_CODEBUILD_ROLE_ARN": (
                    "arn:aws:iam::123456789012:role/build"
                ),
            },
            commands={("bench", "--version"): CommandResult(0, "benchflow 0.6.7", "")},
            executables=frozenset({"bench"}),
        ),
    )

    assert not report.ready
    assert any(check.check_id == "codex.phase" for check in report.failures)


def test_local_pro_rejects_free_subscription_type() -> None:
    probe = _healthy_local_probe()
    commands = dict(probe.commands)
    commands[("claude", "auth", "status", "--json")] = CommandResult(
        0,
        json.dumps({"loggedIn": True, "subscriptionType": "free"}),
        "",
    )

    report = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T1,
        probe=FakeProbe(environment={}, commands=commands),
    )

    assert not report.ready
    assert any(check.check_id == "claude.subscription" for check in report.failures)


def test_apple_version_parser_ignores_unrelated_versions() -> None:
    probe = _healthy_local_probe()
    commands = dict(probe.commands)
    commands[("container", "system", "version", "--format", "json")] = CommandResult(
        0,
        json.dumps(
            [
                {"appName": "schema", "version": "9.9.9"},
                {"appName": "container", "version": "0.1.0"},
            ]
        ),
        "",
    )
    commands[("container", "ls")] = CommandResult(0, "", "")

    report = run_doctor(
        profile_key="local-claude-pro",
        backend="apple-container",
        trust=T1,
        probe=FakeProbe(
            environment={},
            commands=commands,
            executables=frozenset({"claude", "bench", "container"}),
        ),
    )

    assert not report.ready
    assert any(check.check_id == "sandbox.apple.version" for check in report.failures)


def test_profile_backend_cross_product_is_rejected_before_probing() -> None:
    for profile, backend in (
        ("local-claude-pro", "agentcore"),
        ("corporate-claude-bedrock", "native"),
        ("corporate-claude-bedrock", "apple-container"),
    ):
        report = run_doctor(
            profile_key=profile,
            backend=backend,
            trust=T1,
            probe=FakeProbe(environment={}, commands={}, executables=frozenset()),
        )

        assert not report.ready
        assert report.checks == (report.checks[0],)
        assert report.checks[0].check_id == "profile.backend"


def test_route_overrides_and_unpinned_bedrock_model_fail_closed() -> None:
    local = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T1,
        probe=_healthy_local_probe(
            {
                "ANTHROPIC_BASE_URL": "https://proxy.invalid",
                "ANTHROPIC_AUTH_TOKEN": "private-token",
                "CLAUDE_CODE_USE_MANTLE": "1",
            }
        ),
    )
    bedrock = run_doctor(
        profile_key="corporate-claude-bedrock",
        backend="agentcore",
        trust=T1,
        probe=FakeProbe(
            environment={
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_REGION": "us-east-1",
                "ANTHROPIC_MODEL": "sonnet",
                "ANTHROPIC_BEDROCK_BASE_URL": "https://gateway.invalid",
                "BENCHFLOW_AGENTCORE_ROLE_ARN": (
                    "arn:aws:iam::123456789012:role/runtime"
                ),
                "BENCHFLOW_AGENTCORE_CODEBUILD_ROLE_ARN": (
                    "arn:aws:iam::123456789012:role/build"
                ),
            },
            commands={("bench", "--version"): CommandResult(0, "benchflow 0.6.7", "")},
            executables=frozenset({"bench"}),
        ),
    )

    assert not local.ready
    assert not bedrock.ready
    local_json = local.to_json()
    assert "private-token" not in local_json
    assert "ANTHROPIC_BASE_URL" in local_json
    assert "ANTHROPIC_BEDROCK_BASE_URL" in bedrock.to_json()


def test_native_execution_is_mac_only_in_phase_zero() -> None:
    probe = _healthy_local_probe()
    report = run_doctor(
        profile_key="local-claude-pro",
        backend="native",
        trust=T1,
        probe=FakeProbe(
            environment=probe.environment,
            commands=probe.commands,
            system="Windows",
            machine_name="AMD64",
        ),
    )

    assert not report.ready
    assert any(check.check_id == "sandbox.native.platform" for check in report.failures)


def test_system_probe_does_not_resolve_commands_from_ambient_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert SystemProbe().which("claude") != str(fake)


def test_system_probe_scrubs_secrets_and_bounds_captured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "must-not-reach-child"
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(["/usr/bin/true"], 0, "x" * 70_000, secret)

    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setattr("harness_eval.doctor.subprocess.run", fake_run)

    result = SystemProbe().run(("/usr/bin/true",))

    assert secret not in str(captured["env"])
    assert len(result.stdout) == 65_536
    assert result.stderr == secret
