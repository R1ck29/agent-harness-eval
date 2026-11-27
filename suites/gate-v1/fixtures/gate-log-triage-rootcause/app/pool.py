"""Tiny connection pool used by the job workers."""
import queue
import time


class PoolExhausted(Exception):
    pass


class Pool:
    def __init__(self, size, timeout_s):
        self._free = queue.Queue()
        for i in range(size):
            self._free.put(f"conn-{i}")
        self._timeout_s = timeout_s

    def acquire(self):
        try:
            return self._free.get(timeout=self._timeout_s)
        except queue.Empty:
            raise PoolExhausted(f"timed out after {self._timeout_s}s waiting for a connection") from None

    def release(self, conn):
        self._free.put(conn)


def resolve_host(name, cache_ttl_s):
    """Resolve a hostname; with cache_ttl_s == 0 every call goes to the resolver (slow but harmless)."""
    time.sleep(0)
    return name
