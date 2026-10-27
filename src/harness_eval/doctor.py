"""Redacted readiness checks for execution profiles and sandbox backends."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from importlib.util import find_spec
from pathlib import Path
from typing import Protocol

from harness_eval.profiles import AuthKind, built_in_profiles
from harness_eval.sandbox import (
    TrustProfile,
    agentcore_067_capabilities,
    apple_container_067_capabilities,
    native_claude_capabilities,
)

_BENCHFLOW_VERSION = "benchflow 0.6.7"
_APPLE_MIN_VERSION = (1, 1, 0)
_MAX_CAPTURE_CHARS = 65_536
_SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_LOCAL_ROUTE_CONTROLS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_AWS_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_MANTLE_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CONFIG_DIR",
)
_BEDROCK_FORBIDDEN_CONTROLS = tuple(
    name for name in _LOCAL_ROUTE_CONTROLS if name not in {"CLAUDE_CODE_USE_BEDROCK"}
)
_AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")
_IAM_ROLE_PATTERN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$"
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Probe(Protocol):
    def which(self, command: str) -> str | None: ...

    def run(self, argv: Sequence[str]) -> CommandResult: ...

    def env(self) -> Mapping[str, str]: ...

    def platform_system(self) -> str: ...

    def platform_machine(self) -> str: ...


class SystemProbe:
    def which(self, command: str) -> str | None:
        candidates: tuple[Path, ...]
        if command == "bench":
            candidates = (Path(sys.executable).parent / "bench",)
        elif platform.system() == "Darwin" and command == "claude":
            candidates = (
                Path("/opt/homebrew/bin/claude"),
                Path("/usr/local/bin/claude"),
            )
        elif platform.system() == "Darwin" and command == "container":
            candidates = (
                Path("/usr/local/bin/container"),
                Path("/opt/homebrew/bin/container"),
            )
        else:
            candidates = ()
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def run(self, argv: Sequence[str]) -> CommandResult:
        if not argv or not Path(argv[0]).is_absolute():
            return CommandResult(1, "", "")
        environment = {
            name: os.environ[name]
            for name in ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL")
            if os.environ.get(name)
        }
        environment["PATH"] = _SAFE_PATH
        try:
            result = subprocess.run(
                list(argv),
                capture_output=True,
                env=environment,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return CommandResult(1, "", "")
        return CommandResult(
            result.returncode,
            result.stdout[:_MAX_CAPTURE_CHARS],
            result.stderr[:_MAX_CAPTURE_CHARS],
        )

    def env(self) -> Mapping[str, str]:
        return os.environ

    def platform_system(self) -> str:
        return platform.system()

    def platform_machine(self) -> str:
        return platform.machine()


class DoctorStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    check_id: str
    status: DoctorStatus
    message: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    profile_key: str
    backend: str
    trust_profile: str
    checks: tuple[DoctorCheck, ...]

    @property
    def failures(self) -> tuple[DoctorCheck, ...]:
        return tuple(
            check for check in self.checks if check.status is DoctorStatus.FAIL
        )

    @property
    def ready(self) -> bool:
        return all(check.status is DoctorStatus.PASS for check in self.checks)

    def to_json(self) -> str:
        payload = {
            "profile": self.profile_key,
            "backend": self.backend,
            "trust_profile": self.trust_profile,
            "ready": self.ready,
            "checks": [asdict(check) for check in self.checks],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _resolve_command(
    probe: Probe,
    command: str,
    check_id: str,
) -> tuple[DoctorCheck, str | None]:
    executable = probe.which(command)
    if executable:
        return (
            DoctorCheck(check_id, DoctorStatus.PASS, f"{command} is available"),
            executable,
        )
    return (
        DoctorCheck(check_id, DoctorStatus.FAIL, f"Install {command} before use"),
        None,
    )


def _benchflow_checks(probe: Probe) -> list[DoctorCheck]:
    command, executable = _resolve_command(probe, "bench", "benchflow.command")
    if command.status is DoctorStatus.FAIL:
        return [command]
    assert executable is not None
    result = probe.run((executable, "--version"))
    valid = result.returncode == 0 and result.stdout.strip() == _BENCHFLOW_VERSION
    return [
        command,
        DoctorCheck(
            "benchflow.version",
            DoctorStatus.PASS if valid else DoctorStatus.FAIL,
            "BenchFlow is exactly 0.6.7" if valid else "Require benchflow==0.6.7",
        ),
    ]


def _local_claude_checks(probe: Probe) -> list[DoctorCheck]:
    command, executable = _resolve_command(probe, "claude", "claude.command")
    checks = [command]
    if command.status is DoctorStatus.FAIL:
        return checks
    assert executable is not None
    version = probe.run((executable, "--version"))
    checks.append(
        DoctorCheck(
            "claude.version",
            DoctorStatus.PASS if version.returncode == 0 else DoctorStatus.FAIL,
            "Claude Code version is readable"
            if version.returncode == 0
            else "Claude Code version check failed",
        )
    )
    auth = probe.run((executable, "auth", "status", "--json"))
    logged_in = False
    if auth.returncode == 0:
        try:
            payload = json.loads(auth.stdout)
            subscription_type = payload.get("subscriptionType")
            logged_in = bool(payload.get("loggedIn")) and (
                isinstance(subscription_type, str)
                and subscription_type.casefold() == "pro"
            )
        except (json.JSONDecodeError, AttributeError):
            logged_in = False
    checks.append(
        DoctorCheck(
            "claude.subscription",
            DoctorStatus.PASS if logged_in else DoctorStatus.FAIL,
            "Claude subscription authentication is active"
            if logged_in
            else "Log in with the approved Claude subscription",
        )
    )
    present = [name for name in _LOCAL_ROUTE_CONTROLS if probe.env().get(name)]
    checks.append(
        DoctorCheck(
            "claude.route",
            DoctorStatus.FAIL if present else DoctorStatus.PASS,
            "Unset conflicting variables: " + ", ".join(present)
            if present
            else "No API or third-party provider override is active",
        )
    )
    return checks


def _bedrock_checks(probe: Probe) -> list[DoctorCheck]:
    environment = probe.env()
    region = environment.get("AWS_REGION", "")
    model = environment.get("ANTHROPIC_MODEL", "")
    bedrock_enabled = environment.get("CLAUDE_CODE_USE_BEDROCK", "").casefold() in {
        "1",
        "true",
    }
    problems: list[str] = []
    if not bedrock_enabled:
        problems.append("CLAUDE_CODE_USE_BEDROCK must be 1 or true")
    if not _AWS_REGION_PATTERN.fullmatch(region):
        problems.append("AWS_REGION must be an explicit AWS region")
    if not _is_pinned_bedrock_model(model):
        problems.append("ANTHROPIC_MODEL must be a pinned Bedrock model or profile")
    problems.extend(
        f"{name} must be unset"
        for name in _BEDROCK_FORBIDDEN_CONTROLS
        if environment.get(name)
    )
    return [
        DoctorCheck(
            "bedrock.route",
            DoctorStatus.FAIL if problems else DoctorStatus.PASS,
            "Invalid Bedrock configuration: " + ", ".join(problems)
            if problems
            else "Bedrock route and model pin are configured",
        )
    ]


def _is_pinned_bedrock_model(model: str) -> bool:
    if not model or any(character.isspace() for character in model):
        return False
    if model.startswith("arn:"):
        return bool(
            re.fullmatch(
                r"arn:(?:aws|aws-us-gov|aws-cn):bedrock:[a-z0-9-]+:\d{12}:"
                r"application-inference-profile/[A-Za-z0-9._:/+-]+",
                model,
            )
        )
    return "anthropic.claude-" in model and any(
        character.isdigit() for character in model
    )


def _apple_checks(probe: Probe) -> list[DoctorCheck]:
    checks = [
        DoctorCheck(
            "sandbox.apple.platform",
            DoctorStatus.PASS
            if probe.platform_system() == "Darwin"
            and probe.platform_machine().lower() in {"arm64", "aarch64"}
            else DoctorStatus.FAIL,
            "Apple Container host is macOS on Apple silicon",
        )
    ]
    command, executable = _resolve_command(
        probe,
        "container",
        "sandbox.apple.command",
    )
    checks.append(command)
    if command.status is DoctorStatus.FAIL:
        return checks
    assert executable is not None
    result = probe.run((executable, "system", "version", "--format", "json"))
    version = _parse_apple_version(result.stdout) if result.returncode == 0 else None
    supported = version is not None and version >= _APPLE_MIN_VERSION
    checks.append(
        DoctorCheck(
            "sandbox.apple.version",
            DoctorStatus.PASS if supported else DoctorStatus.FAIL,
            "Apple Container is version 1.1.0 or newer"
            if supported
            else "Upgrade Apple Container to version 1.1.0 or newer",
        )
    )
    service = probe.run((executable, "ls"))
    checks.append(
        DoctorCheck(
            "sandbox.apple.service",
            DoctorStatus.PASS if service.returncode == 0 else DoctorStatus.FAIL,
            "Apple Container service is running"
            if service.returncode == 0
            else "Start Apple Container with `container system start`",
        )
    )
    return checks


def _parse_apple_version(output: str) -> tuple[int, int, int] | None:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    rows = payload if isinstance(payload, list) else [payload]
    version = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("appName", "")).casefold() in {"container", "container cli"}:
            version = str(row.get("version", ""))
            break
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _backend_checks(
    probe: Probe,
    backend: str,
    trust: TrustProfile,
) -> list[DoctorCheck]:
    capability_factories = {
        "native": native_claude_capabilities,
        "apple-container": apple_container_067_capabilities,
        "agentcore": agentcore_067_capabilities,
    }
    factory = capability_factories.get(backend)
    if factory is None:
        return [DoctorCheck("sandbox.backend", DoctorStatus.FAIL, "Unknown backend")]
    authorized = factory().authorizes(trust)
    checks = [
        DoctorCheck(
            "sandbox.authorization",
            DoctorStatus.PASS if authorized else DoctorStatus.FAIL,
            f"{backend} authorizes {trust.value}"
            if authorized
            else f"{backend} does not authorize {trust.value}",
        )
    ]
    if backend == "native":
        return [
            *checks,
            DoctorCheck(
                "sandbox.native.platform",
                DoctorStatus.PASS
                if probe.platform_system() == "Darwin"
                else DoctorStatus.FAIL,
                "Native execution is enabled on macOS"
                if probe.platform_system() == "Darwin"
                else "Native execution is not enabled on this platform in Phase 0",
            ),
        ]
    if backend == "apple-container":
        return [*checks, *_apple_checks(probe)]
    if backend == "agentcore":
        required = (
            "BENCHFLOW_AGENTCORE_ROLE_ARN",
            "BENCHFLOW_AGENTCORE_CODEBUILD_ROLE_ARN",
        )
        invalid = [
            name
            for name in required
            if not _IAM_ROLE_PATTERN.fullmatch(probe.env().get(name, ""))
        ]
        dependencies_installed = find_spec("bedrock_agentcore") is not None
        return [
            *checks,
            DoctorCheck(
                "sandbox.agentcore.extra",
                DoctorStatus.PASS if dependencies_installed else DoctorStatus.FAIL,
                "BenchFlow AgentCore dependencies are installed"
                if dependencies_installed
                else "Install the project agentcore extra before use",
            ),
            DoctorCheck(
                "sandbox.agentcore.roles",
                DoctorStatus.FAIL if invalid else DoctorStatus.PASS,
                "Missing or invalid role ARNs: " + ", ".join(invalid)
                if invalid
                else "AgentCore and CodeBuild role ARNs are configured",
            ),
            DoctorCheck(
                "sandbox.agentcore.identity",
                DoctorStatus.UNKNOWN,
                "Live AWS identity and network-policy probe is not implemented",
            ),
        ]
    raise AssertionError("unreachable backend")


def run_doctor(
    *,
    profile_key: str,
    backend: str,
    trust: TrustProfile,
    probe: Probe,
) -> DoctorReport:
    trust_value = trust.value if isinstance(trust, TrustProfile) else "invalid"
    if not isinstance(trust, TrustProfile):
        return DoctorReport(
            profile_key,
            backend,
            trust_value,
            (DoctorCheck("trust", DoctorStatus.FAIL, "Unknown trust profile"),),
        )
    profile = built_in_profiles().get(profile_key)
    if profile is None:
        return DoctorReport(
            profile_key,
            backend,
            trust_value,
            (DoctorCheck("profile", DoctorStatus.FAIL, "Unknown profile"),),
        )
    if not profile.execution_enabled:
        check_id = (
            "codex.phase"
            if profile.auth is AuthKind.CODEX_EXTERNAL
            else "profile.execution"
        )
        return DoctorReport(
            profile_key,
            backend,
            trust_value,
            (
                DoctorCheck(
                    check_id,
                    DoctorStatus.FAIL,
                    "Execution is not enabled for this profile in Phase 0",
                ),
            ),
        )
    if backend not in profile.allowed_backends:
        return DoctorReport(
            profile_key,
            backend,
            trust_value,
            (
                DoctorCheck(
                    "profile.backend",
                    DoctorStatus.FAIL,
                    "Backend is not allowed for this profile",
                ),
            ),
        )
    checks = _benchflow_checks(probe)
    if profile.auth is AuthKind.CLAUDE_SUBSCRIPTION:
        checks.extend(_local_claude_checks(probe))
    elif profile.auth is AuthKind.AWS_BEDROCK:
        checks.extend(_bedrock_checks(probe))
    checks.extend(_backend_checks(probe, backend, trust))
    return DoctorReport(profile_key, backend, trust_value, tuple(checks))
