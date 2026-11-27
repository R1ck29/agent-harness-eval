#!/usr/bin/env python3
"""Catch the defect class no code graph can reach.

Port of harness/string_contracts.py from R1ck29/cc-review-tool-eval@b8372b2.
Usage: python string_contracts.py [ROOT]  (default: ../fixtures/repo)

A call/import graph links *symbols*. It cannot link two files that agree only on
a string: a SQL column name, an event topic, a serialized dict key. In the
benchmark these are the H-class defects, and they are exactly the ones that
survive every graph-based context strategy.

They are, however, cheap to find without any model at all: collect the string
literals each file produces and consumes, then flag near-misses across files.
This is a demonstration of the principle on the benchmark repo, not a
general-purpose linter - the point is that the residual gap is closable with a
deterministic pre-pass costing zero tokens.
"""
import ast
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

SQL_COLS = re.compile(r"SELECT\s+(.+?)\s+FROM", re.I | re.S)
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Vendored code is not under review, and scanning it turns this pass from
# seconds into minutes (a venv adds ~8,000 files).
SKIP = {".git", "node_modules", "site-packages", ".venv", "venv", "__pycache__",
        "build", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "repo"


def subscript_key(node):
    """Return the constant string a Subscript indexes, or None.

    Python <= 3.8 wraps the index in ``ast.Index``; 3.9+ stores the expression
    directly. Without this unwrap the dict-key rule below matches nothing on
    3.8 and reports *fewer findings with no error* - a silent downgrade that
    looks like a clean repo. main() reports unparsed files for the same
    reason: a rule that goes quiet must never look like a clean repo.
    """
    sl = node.slice
    sl = getattr(sl, "value", sl) if type(sl).__name__ == "Index" else sl
    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
        return sl.value
    return None


def source_files(root, pattern="*.py"):
    return [p for p in root.rglob(pattern) if not any(s in p.parts for s in SKIP)]


def norm(s):
    """snake_case / camelCase / dotted -> comparable token bag."""
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", s)
    return tuple(t for t in re.split(r"[._\-\s]+", s.lower()) if t)


def string_literals(path: Path):
    # Same exception set as the rule-2 parse site in main(): ValueError is what
    # CPython 3.8 raises for a NUL byte in the source, and it would otherwise
    # abort the whole run here - after rule 2 already succeeded, so the process
    # dies having printed nothing. UnicodeDecodeError cannot fire (read_text
    # uses errors="ignore") but is kept: it costs nothing and documents intent.
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, UnicodeDecodeError, ValueError, OSError):
        return []
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def sql_columns(path: Path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    # Splice adjacent string literals back together: a SQL statement written as
    # "SELECT a, b " \n "FROM t" is one query, not two fragments.
    text = re.sub(r"['\"]\s*['\"]", "", text)
    cols = set()
    for m in SQL_COLS.finditer(text):
        for part in m.group(1).split(","):
            words = part.strip().split()
            if not words:          # empty SELECT fragment (trailing comma, f-string hole)
                continue
            part = words[-1].strip('"`[]')
            if IDENT.match(part):
                cols.add(part)
    return cols


def ddl_columns(path: Path):
    cols = set()
    body = path.read_text(encoding="utf-8", errors="ignore")
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(INTEGER|TEXT|REAL|BLOB|NUMERIC)", line, re.I)
        if m:
            cols.add(m.group(1))
    return cols


def main(root):
    root = Path(root)
    findings = []

    # --- 1. SQL identifiers that no DDL in the repo defines --------------
    ddl = set()
    for sql in source_files(root, "*.sql"):
        ddl |= ddl_columns(sql)
    if ddl:
        for py in source_files(root):
            for col in sql_columns(py) - ddl - {"*"}:
                near = [d for d in ddl if SequenceMatcher(None, col, d).ratio() > 0.6]
                if near:
                    findings.append((py.relative_to(root).as_posix(),
                                     f"SQL identifier {col!r} is not a declared column; "
                                     f"did you mean {near[0]!r}?"))

    # --- 2. dict keys that differ only by naming convention -------------
    produced, consumed = {}, {}
    unparsed = []
    skipped_rules = []
    for py in source_files(root):
        rel = py.relative_to(root).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError, OSError):
            # A file the running interpreter cannot parse contributes neither
            # readers nor producers below. Silently skipping it makes the pass
            # report FEWER findings with no error - and, worse, can INVENT
            # findings (see the guard after this loop). Collect and report.
            #
            # ValueError is not redundant with SyntaxError: on CPython 3.8 a
            # NUL byte in the source raises ValueError("source code string
            # cannot contain null bytes"), which would otherwise escape and
            # abort the whole run before printing anything. 3.9+ raises
            # SyntaxError for the same input. OSError covers a broken symlink
            # that rglob happily yields.
            unparsed.append(rel)
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Dict):
                for k in n.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        produced.setdefault(k.value, set()).add(rel)
            if isinstance(n, ast.Subscript):
                key = subscript_key(n)
                if key is not None:
                    consumed.setdefault(key, set()).add(rel)

    # This rule fires on the ABSENCE of a producer ("no file writes this key,
    # but one writes a near-miss"). Absence is only evidence if every file was
    # read. A file dropped above contributes no producers, so its keys look
    # unwritten and the rule reports a mismatch that does not exist - verified:
    # a sole producer using 3.10 syntax yields 1 false finding on 3.8 and 0 on
    # 3.12. A pass sold as deterministic must not emit interpreter-dependent
    # false positives, so the rule is skipped rather than made unreliable.
    if unparsed:
        skipped_rules.append(
            f"dict-key mismatch (rule 2): needs every producer parsed, "
            f"{len(unparsed)} file(s) were not")
    else:
        by_norm = {}
        for other in produced:
            by_norm.setdefault(norm(other), []).append(other)
        for key, readers in consumed.items():
            if key in produced:
                continue
            for other in by_norm.get(norm(key), []):
                if key != other:
                    writers = sorted(produced[other])
                    who = ("the only producer" if len(writers) == 1
                           else f"all {len(writers)} producers")
                    verb = "writes" if len(writers) == 1 else "write"
                    findings.append((", ".join(sorted(readers)),
                                     f"reads key {key!r} but {who} "
                                     f"({', '.join(writers)}) {verb} {other!r}"))

    # --- 3. topic strings published with no matching subscriber ---------
    pub, sub = {}, {}
    for py in source_files(root):
        rel = py.relative_to(root).as_posix()
        try:
            src = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lit in string_literals(py):
            if norm(lit) and len(norm(lit)) > 1 and re.search(r"[._]", lit):
                (sub if "subscribe" in src else pub).setdefault(lit, set()).add(rel)
    for topic, subs in sub.items():
        for other in pub:
            if topic != other and norm(topic) == norm(other) and topic not in pub:
                findings.append((", ".join(sorted(subs)),
                                 f"subscribes to {topic!r} but publisher emits {other!r}"))

    for where, what in findings:
        print(f"  {where}: {what}")
    print(f"\n{len(findings)} string-contract finding(s), 0 tokens spent.")
    if unparsed:
        print(f"\nWARNING: {len(unparsed)} file(s) could not be parsed by "
              f"python {sys.version_info[0]}.{sys.version_info[1]}. Their dict "
              f"keys and string literals were not indexed, so results below "
              f"are BOTH incomplete (missed findings) and unreliable (a "
              f"missing producer can invent a finding). Re-run on a newer "
              f"interpreter before trusting this output.")
        for rule in skipped_rules:
            print(f"  SKIPPED {rule}")
        for rel in unparsed[:5]:
            print(f"  unparsed: {rel}")
        if len(unparsed) > 5:
            print(f"  ... and {len(unparsed) - 5} more")
    return findings


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT)
