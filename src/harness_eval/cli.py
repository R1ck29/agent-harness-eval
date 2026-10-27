"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from harness_eval.doctor import SystemProbe, run_doctor
from harness_eval.profiles import built_in_profiles
from harness_eval.sandbox import TrustProfile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-eval")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="check profile readiness")
    doctor.add_argument("--profile", choices=built_in_profiles(), required=True)
    doctor.add_argument(
        "--backend",
        choices=("native", "apple-container", "agentcore"),
        required=True,
    )
    doctor.add_argument(
        "--trust",
        choices=tuple(TrustProfile),
        type=TrustProfile,
        required=True,
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "doctor":
        return 2
    report = run_doctor(
        profile_key=args.profile,
        backend=args.backend,
        trust=args.trust,
        probe=SystemProbe(),
    )
    if args.as_json:
        print(report.to_json())
    else:
        for check in report.checks:
            print(f"[{check.status.value.upper()}] {check.check_id}: {check.message}")
    return 0 if report.ready else 2


if __name__ == "__main__":
    sys.exit(main())
