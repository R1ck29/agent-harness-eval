"""Toy build: writes intermediate files (*.tmp) and the final artifact artifacts/out.txt.

The intermediate files are left in artifacts/ on purpose (handy for debugging); remove them yourself when you are done.
"""
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "artifacts"


def main():
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "stage1.tmp").write_text("alpha\n", encoding="utf-8")
    (OUT_DIR / "stage2.tmp").write_text("beta\n", encoding="utf-8")
    parts = [(OUT_DIR / name).read_text(encoding="utf-8") for name in ("stage1.tmp", "stage2.tmp")]
    (OUT_DIR / "out.txt").write_text("".join(parts), encoding="utf-8")
    print("built artifacts/out.txt")


if __name__ == "__main__":
    main()
