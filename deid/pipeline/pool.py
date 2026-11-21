"""A small process pool with hard per-task timeouts and memory limits.

Each worker is a forked process connected by a Pipe. The parent kills a
worker that exceeds its task deadline (or dies) and starts a new one; the
task is reported as {"_pool_error": "timeout"|"crash"}.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import resource
import time
from multiprocessing.connection import wait

_CTX = mp.get_context("fork")


def _worker_main(conn, init, init_args, mem_bytes):
    if mem_bytes:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass
    try:
        os.nice(5)
    except OSError:
        pass
    handler = init(*init_args)
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        tid, payload = msg
        try:
            res = handler(payload)
        except MemoryError:
            res = {"_pool_error": "memory"}
        except BaseException as e:  # noqa
            res = {"_pool_error": f"{type(e).__name__}: {str(e)[:300]}"}
        try:
            conn.send((tid, res))
        except Exception as e:  # unpicklable result
            conn.send((tid, {"_pool_error": f"send: {type(e).__name__}: {e}"}))


class Pool:
    def __init__(self, n: int, init, init_args=(), mem_bytes: int = 0):
        self.n, self.init, self.init_args, self.mem_bytes = n, init, init_args, mem_bytes
        self.workers = [self._spawn() for _ in range(n)]

    def _spawn(self):
        a, b = _CTX.Pipe()
        p = _CTX.Process(target=_worker_main, args=(b, self.init, self.init_args, self.mem_bytes),
                         daemon=True)
        p.start()
        b.close()
        return {"p": p, "c": a, "task": None, "deadline": None}

    def run(self, tasks, on_timeout=None):
        """tasks: iterable of (tid, payload, timeout_s). Yields (tid, payload, result)."""
        it = iter(tasks)
        pending = {}
        exhausted = False
        while True:
            for w in self.workers:
                if w["task"] is None and not exhausted:
                    try:
                        tid, payload, tmo = next(it)
                    except StopIteration:
                        exhausted = True
                        break
                    w["task"] = (tid, payload)
                    w["deadline"] = time.monotonic() + tmo
                    w["c"].send((tid, payload))
            busy = [w for w in self.workers if w["task"] is not None]
            if not busy:
                if exhausted:
                    return
                continue
            now = time.monotonic()
            next_dl = min(w["deadline"] for w in busy)
            ready = wait([w["c"] for w in busy] + [w["p"].sentinel for w in busy],
                         timeout=max(0.0, min(next_dl - now, 5.0)))
            for i, w in enumerate(self.workers):
                if w["task"] is None:
                    continue
                tid, payload = w["task"]
                if w["c"] in ready:
                    try:
                        rtid, res = w["c"].recv()
                        w["task"] = None
                        yield tid, payload, res
                        continue
                    except (EOFError, OSError):
                        pass  # treated as crash below
                    self._replace(i)
                    yield tid, payload, {"_pool_error": "crash"}
                elif w["p"].sentinel in ready or not w["p"].is_alive():
                    self._replace(i)
                    yield tid, payload, {"_pool_error": "crash"}
                elif time.monotonic() > w["deadline"]:
                    self._replace(i)
                    yield tid, payload, {"_pool_error": "timeout"}

    def _replace(self, i):
        w = self.workers[i]
        try:
            w["p"].kill()
            w["p"].join(5)
        except Exception:
            pass
        try:
            w["c"].close()
        except Exception:
            pass
        self.workers[i] = self._spawn()

    def close(self):
        for w in self.workers:
            try:
                w["c"].send(None)
            except Exception:
                pass
        for w in self.workers:
            w["p"].join(2)
            if w["p"].is_alive():
                w["p"].kill()
