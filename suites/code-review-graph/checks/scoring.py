"""Deterministic scoring rule for the seeded-defect review benchmark.

Extracted from harness/blind_review.py of R1ck29/cc-review-tool-eval@b8372b2.
Only the pure scoring definitions (MATCH, findings, scored) are kept; the Claude
CLI invocation and blinding runner are replaced by the AHE runner.
matched_finding() is factored out of scored(); the rule itself is unchanged:
  - matching happens inside one finding, never across the whole review
  - every MATCH group needs >=1 alternative present in that finding
  - cross-file defects (X and H) carry an anchor group naming the other side
  - findings() tolerates decorated labels ("**FINDING:**", "- FINDING:", "1. FINDING:")
"""
import re

# Each entry: list of groups; a finding matches when every group has >=1
# alternative present *within that one finding*.
#
# Cross-file defects (X and H) carry a third "anchor" group naming the other
# side of the inconsistency. Without it the scorer credits any unrelated remark
# about the changed file - auditing the first pass showed exactly that:
# "get_order has no Optional[Order] type hint" and "discount_percent has
# ambiguous units" were both scored as detections. A cross-file defect is only
# found when the reviewer connects both sides, so the anchor is the test.
MATCH = {
    # L - single file; no anchor needed
    "D01": [["bulk", "discount", "threshold"],
            ["off-by-one", "off by one", ">=", "greater than or equal",
             "boundary", "equal to", "exactly", "reaches"]],
    "D02": [["parse_money"],
            ["valueerror", "exception", "malformed", "invalid", "crash",
             "unhandled", "not caught"]],
    "D04": [["connect", "connection", "conn"],
            ["leak", "close", "finally", "context manager", "with statement"]],
    # X / H - the anchor group names the other side
    "D05": [["compute_total"],
            ["argument", "parameter", "typeerror", "missing", "signature", "arity"],
            ["orders.py", "create_order"]],
    "D06": [["get_order"],
            ["none", "null", "attributeerror"],
            ["orders.py", "get_order_summary"]],
    "D07": [["discount"],
            ["attributeerror", "does not exist", "no such", "renamed", "mismatch",
             "not match", "does not match"],
            ["serializ", "order_to_dict"]],
    "D08": [["discount_pct"],
            ["column", "no such", "does not exist", "mismatch", "not match"],
            ["schema", "discount_percent", "ddl"]],
    "D09": [["order.created"],
            ["mismatch", "never", "not match", "no subscriber", "silently"],
            ["handlers", "order_created\"", "'order_created'", "`order_created`",
             "subscribes to \"order_created", "subscribes to 'order_created"]],
    "D10": [["customerid"],
            ["mismatch", "keyerror", "camel", "snake", "not match", "does not match"],
            ["customer_id", "serializ", "order_to_dict"]],
    "D11": [["on_order_created", "run_task"],
            ["dynamic", "getattr", "rename", "static"],
            ["tasks.py", "handlers"]],
    "D12": [["max_retries"],
            ["retry", "disable", "no attempt", "single attempt", "only once"],
            ["0", "zero"]],
}


def findings(text: str):
    """Split a review into individual FINDING lines.

    Matching must happen inside one finding, not across the whole review. A
    reviewer that mentions the right symbol in one finding and the right
    failure mode in an unrelated one has not identified the defect - scoring
    across the full text credits it anyway, which is how the first pass
    produced a false 88% for diff-only.
    """
    out = []
    for line in (text or "").splitlines():
        # Reviewers decorate the label: "**FINDING:**", "- FINDING:", "1. FINDING:".
        # Anchoring on a bare "FINDING:" prefix silently drops those lines and
        # turns a real detection into a scored miss, so strip decoration first.
        m = re.match(r"^[\s>*_\-#\d.)\[]*finding\s*:?\s*\**\s*(.*)$", line.strip(),
                     re.IGNORECASE)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out


def matched_finding(defect_id: str, text: str):
    """The first finding that satisfies every MATCH group of defect_id, or None."""
    for finding in findings(text):
        low = finding.lower()
        if all(any(re.search(re.escape(alt.lower()), low) for alt in group)
               for group in MATCH[defect_id]):
            return finding
    return None


def scored(defect_id: str, text: str) -> bool:
    return matched_finding(defect_id, text) is not None
