"""Deterministic context recall + context-cost benchmark (no model, no network).

Port of harness/run_bench.py from R1ck29/cc-review-tool-eval@b8372b2, adjusted to
suites/code-review-graph/{fixtures/repo,defects.json}. For each seeded defect it asks:

  context recall - is every file needed to *see* the defect present in the context
                   a strategy assembles? If not, no model can find it, so this is a
                   hard ceiling on review quality.
  context cost   - estimated tokens of the selected files, plus the tool-output
                   payload tokens the graph CLI itself returns (the payload is part
                   of the conversation, so it is billed context too).

Strategies: full-repo / diff-only / graph (`impact`) / graph+callers (`impact` +
`query callers_of`). The `code-review-graph` CLI is reached only through
run_cli(), so tests can pass a stub and CI does not need the CLI installed.
Running it for real requires the fixture to be a git repo with a built graph
(git init && git commit, then `code-review-graph build --repo <workdir>`).
"""
import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

SUITE = Path(__file__).resolve().parent.parent
DEFAULT_REPO = SUITE / "fixtures" / "repo"
DEFAULT_DEFECTS = SUITE / "defects.json"
SRC_EXT = {".py", ".sql", ".ts", ".js", ".go", ".rs", ".java"}
STRATEGIES = ("full-repo", "diff-only", "graph", "graph+callers")


def est_tokens(text: str) -> int:
    """~4 chars/token. An estimate, but it cancels in the ratios we report."""
    return (len(text) + 3) // 4


def repo_files(root: Path):
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and p.suffix in SRC_EXT and ".git" not in rel.parts \
                and ".code-review-graph" not in rel.parts and "__pycache__" not in rel.parts:
            out.append(rel.as_posix())
    return out


def tokens_for(root: Path, rel_files):
    total = 0
    for rel in rel_files:
        fp = root / rel
        if fp.exists():
            total += est_tokens(fp.read_text(encoding="utf-8", errors="ignore"))
    return total


def run_cli(cmd):
    """Run the graph CLI; returns (returncode, stdout, stderr). Tests replace this."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _rel(root: Path, fp: str):
    try:
        return Path(fp).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def graph_context(cli: str, root: Path, changed, depth: int, run=None):
    """Files code-review-graph says are in the blast radius of `changed`, and the payload tokens."""
    run = run_cli if run is None else run
    code, out, err = run([cli, "impact", "--repo", str(root), "--files", *changed, "--depth", str(depth)])
    if code != 0:
        raise RuntimeError(err[-500:])
    data = json.loads(out)
    files = set(changed)
    for key in ("changed_nodes", "impacted_nodes", "affected_files", "impacted_files"):
        for item in data.get(key, []) or []:
            fp = item.get("file_path") if isinstance(item, dict) else item
            rel = _rel(root, fp) if fp else None
            if rel:
                files.add(rel)
    return sorted(files), est_tokens(out)


def caller_context(cli: str, root: Path, changed, base_files, run=None):
    """`impact` + every file containing a *caller* of a symbol defined in the changed
    files, via the symbol-level `query callers_of`.

    This exists because the two disagree: the CALLS edges are complete, but the
    blast radius is derived from file-level IMPORTS edges and silently drops
    callers that used `from package import module`.
    """
    run = run_cli if run is None else run
    files = set(base_files)
    extra_tokens = 0
    for rel in changed:
        src = root / rel
        if src.suffix != ".py":
            continue
        try:
            tree = ast.parse(src.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        names = [n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        for name in names:
            code, out, _ = run([cli, "query", "--repo", str(root), "callers_of", name])
            if code != 0:
                continue
            extra_tokens += est_tokens(out)
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                continue
            results = data.get("results", [])
            if data.get("status") == "ambiguous":
                results = data.get("candidates", [])
            for n in results if isinstance(results, list) else []:
                fp = n.get("file_path") if isinstance(n, dict) else None
                r = _rel(root, fp) if fp else None
                if r:
                    files.add(r)
    return sorted(files), extra_tokens


def measure(root: Path, defects, cli: str = "code-review-graph", depth: int = 2, run=None):
    """One row per defect: hit / tokens / missing per strategy, plus graph payload tokens."""
    run = run_cli if run is None else run
    root = Path(root)
    all_files = repo_files(root)
    full_tokens = tokens_for(root, all_files)
    rows = []
    for d in defects:
        # `changed` is a single path in the manifest and a list in multi-file variants.
        changed = d["changed"] if isinstance(d["changed"], list) else [d["changed"]]
        gfiles, payload_tokens = graph_context(cli, root, changed, depth, run)
        cfiles, callers_tokens = caller_context(cli, root, changed, gfiles, run)
        need = set(d["evidence"])
        strategies = {
            "full-repo": (all_files, full_tokens),
            "diff-only": (changed, tokens_for(root, changed)),
            "graph": (gfiles, tokens_for(root, gfiles)),
            "graph+callers": (cfiles, tokens_for(root, cfiles)),
        }
        row = {"id": d["id"], "class": d["class"], "changed": changed, "evidence": d["evidence"],
               "static_edge": d["static_edge"], "graph_payload_tokens": payload_tokens,
               "callers_payload_tokens": callers_tokens, "graph_files": gfiles}
        for name, (files, toks) in strategies.items():
            row[f"{name}_hit"] = need.issubset(set(files))
            row[f"{name}_tokens"] = toks
            row[f"{name}_missing"] = sorted(need - set(files))
        rows.append(row)
    return {"repo": root.name, "depth": depth, "repo_files": len(all_files),
            "full_repo_tokens": full_tokens, "rows": rows}


def recall(rows, strategy: str, cls: str = "*") -> float:
    sub = rows if cls == "*" else [r for r in rows if r["class"] == cls]
    return sum(r[f"{strategy}_hit"] for r in sub) / len(sub) if sub else 0.0


def load_defects(path: Path, only_verdict: str | None = None):
    defects = json.loads(Path(path).read_text(encoding="utf-8"))["defects"]
    if only_verdict:
        verdicts = json.loads((Path(path).parent / "verdicts.json").read_text(encoding="utf-8"))
        defects = [d for d in defects if verdicts.get(d["id"]) == only_verdict]
    return defects


def report(result) -> str:
    rows = result["rows"]
    lines = [f"repo: {result['repo_files']} source files, {result['full_repo_tokens']} est. tokens, depth={result['depth']}", ""]
    for r in rows:
        m = " ".join(f"{s}={'HIT' if r[f'{s}_hit'] else 'MISS'}" for s in STRATEGIES)
        lines.append(f"{r['id']:4} {r['class']:2} {m}  payload={r['graph_payload_tokens']}+{r['callers_payload_tokens']}")
    lines += ["", "recall by class: " + "  ".join(STRATEGIES)]
    for cls in ("L", "X", "H", "*"):
        if cls == "*" or any(r["class"] == cls for r in rows):
            lines.append(f"  {cls}: " + "  ".join(f"{recall(rows, s, cls):.0%}" for s in STRATEGIES))
    return "\n".join(lines)


def main(argv=None, run=None) -> int:
    run = run_cli if run is None else run
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--defects", default=str(DEFAULT_DEFECTS))
    ap.add_argument("--cli", default="code-review-graph")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--fires-only", action="store_true", help="only defects classified FIRES in verdicts.json")
    ap.add_argument("--out", help="write the JSON result here (no absolute paths are recorded)")
    a = ap.parse_args(argv)
    defects = load_defects(Path(a.defects), "FIRES" if a.fires_only else None)
    result = measure(Path(a.repo), defects, a.cli, a.depth, run=run)
    print(report(result))
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
