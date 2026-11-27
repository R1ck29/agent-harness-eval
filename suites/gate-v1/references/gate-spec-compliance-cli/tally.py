"""tally: word frequency CLI (see SPEC.md)."""
import re
import sys
from collections import Counter

USAGE = "usage: tally.py [--top N] FILE"


def usage_error(message):
    print(f"error: {message}\n{USAGE}", file=sys.stderr)
    return 2


def main(argv):
    top = None
    path = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--help":
            print(USAGE)
            return 0
        if arg == "--top":
            i += 1
            if i >= len(argv) or not re.fullmatch(r"[0-9]+", argv[i]) or int(argv[i]) < 1:
                return usage_error("--top needs a positive integer")
            top = int(argv[i])
        elif arg.startswith("--") or (arg.startswith("-") and arg != "-"):
            return usage_error(f"unknown option {arg}")
        elif path is None:
            path = arg
        else:
            return usage_error("too many arguments")
        i += 1
    if path is None:
        return usage_error("FILE is required")
    try:
        text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 1
    counts = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    for word, n in rows[:top]:
        print(f"{word} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
