"""Job worker: every worker thread holds one pooled connection for the whole job."""
import json
import threading

from .pool import Pool, PoolExhausted, resolve_host


def load_settings(path="config/settings.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run(jobs, settings):
    pool = Pool(settings["pool_size"], settings["pool_timeout_s"])
    results = []

    def work(job):
        resolve_host("db.internal", settings["dns_cache_ttl_s"])
        try:
            conn = pool.acquire()
        except PoolExhausted as e:
            results.append((job, f"failed: {e}"))
            return
        try:
            results.append((job, "ok"))
        finally:
            pool.release(conn)

    threads = [threading.Thread(target=work, args=(j,)) for j in jobs[: settings["worker_threads"]]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
