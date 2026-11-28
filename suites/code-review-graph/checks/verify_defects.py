#!/usr/bin/env python3
"""Do the seeded defects actually misbehave?

Path-adjusted port of harness/verify_defects.py from R1ck29/cc-review-tool-eval@b8372b2.
Runs against suites/code-review-graph/fixtures/repo and also compares the result
with ../verdicts.json, so a drifted classification exits 1.

The benchmark asserts twelve defects via comments. If a "defect" does not
actually break anything, every recall number computed against it is measuring
nothing. This executes the benchmark repo and checks each one fires.

Verdicts:
  FIRES   - reproduced at runtime; a real defect
  LATENT  - no runtime failure in the current configuration; the defect is a
            coupling that will break on a future edit. Still a legitimate
            review finding, but weaker evidence than FIRES.
  STATIC  - not observable by execution (readability / resource-lifetime);
            verified by inspection instead.
"""
import json
import sqlite3
import sys
import traceback
from pathlib import Path

SUITE = Path(__file__).resolve().parent.parent
REPO = SUITE / "fixtures" / "repo"
VERDICTS = SUITE / "verdicts.json"
sys.dont_write_bytecode = True  # keep the fixture tree free of __pycache__
sys.path.insert(0, str(REPO))

results = []


def check(defect_id, verdict, detail):
    results.append((defect_id, verdict, detail))


def expect_raises(defect_id, exc_type, fn, note):
    try:
        fn()
    except exc_type as e:
        check(defect_id, "FIRES", f"{type(e).__name__}: {e}")
        return
    except Exception as e:  # noqa: BLE001
        check(defect_id, "FIRES", f"{type(e).__name__} (expected {exc_type.__name__}): {e}")
        return
    check(defect_id, "LATENT", note)


def main():
    # handlers is imported for its side effect: it subscribes 'order_created' (needed by D09)
    from app import api, config, db, events, handlers, orders, pricing, serializers, tasks, utils  # noqa: F401
    from app.models import Order

    # D01 - off-by-one at the bulk threshold
    got = pricing.apply_bulk_discount(10, 100)
    if got == 1000:
        check("D01", "FIRES", f"qty==threshold charged full price: {got} (expected 900)")
    else:
        check("D01", "LATENT", f"returned {got}")

    # D02 - unhandled ValueError on malformed money
    expect_raises("D02", ValueError, lambda: utils.parse_money("abc"),
                  "parse_money swallowed bad input")

    # D03 - local rebinding shadows the module constant
    before = orders.STATUS_OPEN
    orders._normalise("  OPEN ")
    after = orders.STATUS_OPEN
    check("D03", "STATIC",
          f"module constant unchanged ({before!r} -> {after!r}); "
          "no runtime failure, but the local rebinding reads as a mutation")

    # D04 - connection leaked on the exception path
    original_connect = db.connect
    leaked = {}

    class TrackingConn:
        def __init__(self):
            self.closed = False
            leaked["conn"] = self

        def cursor(self):
            raise sqlite3.OperationalError("no such table: orders")

        def close(self):
            self.closed = True

    db.connect = lambda: TrackingConn()
    try:
        db.fetch_order_row(1)
    except sqlite3.OperationalError:
        pass
    finally:
        db.connect = original_connect
    if leaked.get("conn") and not leaked["conn"].closed:
        check("D04", "FIRES", "connection never closed after cursor() raised")
    else:
        check("D04", "LATENT", "connection was closed")

    # D05 - caller passes two args to a three-arg function
    expect_raises("D05", TypeError, lambda: orders.create_order(1, 5, 100),
                  "create_order succeeded despite the new required param")

    # D06 - caller dereferences a None return.
    # The table must exist, or the call fails earlier on OperationalError and
    # never reaches the None-deref path we are trying to observe.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        dbfile = Path(tmp) / "orders.db"
        seeded = sqlite3.connect(dbfile)
        seeded.executescript((REPO / "schema.sql").read_text(encoding="utf-8"))
        # Give the row the column db.py actually selects, so only the missing
        # row - not the D08 column typo - drives this check.
        seeded.execute("ALTER TABLE orders ADD COLUMN discount_pct REAL DEFAULT 0.0")
        seeded.commit()
        seeded.close()
        db_path_backup = config.SETTINGS["DB_PATH"]
        config.SETTINGS["DB_PATH"] = str(dbfile)
        try:
            expect_raises("D06", AttributeError, lambda: orders.get_order_summary(999),
                          "get_order_summary handled the miss")
        finally:
            config.SETTINGS["DB_PATH"] = db_path_backup

    # D07 - serializer reads a renamed attribute
    order = Order(id=1, customer_id=2, status="open", discount_percent=0.1, total_cents=500)
    expect_raises("D07", AttributeError, lambda: serializers.order_to_dict(order),
                  "order_to_dict found .discount")

    # D08 - SQL selects a column the DDL does not declare
    conn = sqlite3.connect(":memory:")
    conn.executescript((REPO / "schema.sql").read_text(encoding="utf-8"))
    try:
        conn.execute("SELECT id, customer_id, status, discount_pct, total_cents FROM orders")
        check("D08", "LATENT", "discount_pct resolved against the schema")
    except sqlite3.OperationalError as e:
        check("D08", "FIRES", f"{e}")
    conn.close()

    # D09 - published topic never matches the subscribed one
    seen = []
    events.subscribe("order_created", lambda p: seen.append(p))
    events.publish_order_created({"id": 7})
    if not seen:
        check("D09", "FIRES", "publish('order.created') reached no 'order_created' subscriber")
    else:
        check("D09", "LATENT", f"handler ran: {seen}")

    # D10 - api reads a key the serializer never writes
    payload = serializers.order_to_dict.__doc__ and {}
    payload = {"customer_id": 2, "quantity": 5, "unit_cents": 100}
    expect_raises("D10", KeyError, lambda: api.ROUTES["/orders"](payload),
                  "api.create found customerId")

    # D11 - dynamic dispatch resolves today, but nothing links the two files
    try:
        tasks.run_task("order_created", {"id": 1})
        check("D11", "LATENT",
              "getattr(handlers, 'on_' + name) resolves in the current config; "
              "renaming on_order_created breaks it with no static reference to update")
    except LookupError as e:
        check("D11", "FIRES", f"LookupError: {e}")

    # D12 - MAX_RETRIES=0 silently collapses the retry loop
    calls = []
    original_fetch = db.fetch_order_row
    db.fetch_order_row = lambda oid: calls.append(oid) or None
    try:
        from app import repository
        repository.get_order(1)
    finally:
        db.fetch_order_row = original_fetch
    if len(calls) == 1:
        check("D12", "LATENT",
              f"MAX_RETRIES=0 -> {len(calls)} attempt; no exception, retry silently disabled")
    else:
        check("D12", "FIRES", f"{len(calls)} attempts")

    # ---- report ---------------------------------------------------------
    width = max(len(d) for _, _, d in results)
    print(f"\n{'id':5} {'verdict':8} detail")
    print("-" * min(width + 16, 100))
    for did, verdict, detail in sorted(results):
        print(f"{did:5} {verdict:8} {detail}")

    counts = {}
    for _, v, _ in results:
        counts[v] = counts.get(v, 0) + 1
    print(f"\n{len(results)} seeded defects: " +
          ", ".join(f"{v} {n}" for v, n in sorted(counts.items())))
    print("\nFIRES  = reproduced at runtime, unambiguously a defect")
    print("LATENT = no failure in the current config; a coupling that breaks on edit")
    print("STATIC = verified by inspection, not executable")
    expected = json.loads(VERDICTS.read_text(encoding="utf-8"))
    actual = {did: verdict for did, verdict, _ in results}
    drift = sorted(k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k))
    if drift:
        print(f"\nMISMATCH vs verdicts.json: {', '.join(drift)}")
        return 1
    return 0 if counts.get("FIRES", 0) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
