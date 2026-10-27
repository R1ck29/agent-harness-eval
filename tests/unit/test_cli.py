import json

import pytest

from harness_eval import cli
from harness_eval.doctor import DoctorCheck, DoctorReport, DoctorStatus
from harness_eval.sandbox import TrustProfile


def test_doctor_cli_prints_only_sanitized_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-private-cli-secret"
    report = DoctorReport(
        profile_key="local-claude-pro",
        backend="native",
        trust_profile=TrustProfile.T1_REVIEWED_STATIC.value,
        checks=(DoctorCheck("safe", DoctorStatus.FAIL, "ANTHROPIC_API_KEY is set"),),
    )
    monkeypatch.setattr(cli, "run_doctor", lambda **_: report)

    exit_code = cli.main(
        [
            "doctor",
            "--profile",
            "local-claude-pro",
            "--backend",
            "native",
            "--trust",
            TrustProfile.T1_REVIEWED_STATIC.value,
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert secret not in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["ready"] is False


def test_doctor_cli_text_mode_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = DoctorReport(
        profile_key="local-claude-pro",
        backend="native",
        trust_profile=TrustProfile.T1_REVIEWED_STATIC.value,
        checks=(DoctorCheck("safe", DoctorStatus.PASS, "ready"),),
    )
    monkeypatch.setattr(cli, "run_doctor", lambda **_: report)

    exit_code = cli.main(
        [
            "doctor",
            "--profile",
            "local-claude-pro",
            "--backend",
            "native",
            "--trust",
            TrustProfile.T1_REVIEWED_STATIC.value,
        ]
    )

    assert exit_code == 0
    assert "[PASS] safe: ready" in capsys.readouterr().out
