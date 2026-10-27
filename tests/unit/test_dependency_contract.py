import shutil
import subprocess
from importlib.metadata import version


def test_benchflow_release_is_exactly_pinned() -> None:
    assert version("benchflow") == "0.6.7"


def test_benchflow_public_cli_contract_is_available() -> None:
    command = shutil.which("bench")
    assert command is not None

    result = subprocess.run(
        [command, "--version"],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "benchflow 0.6.7"
