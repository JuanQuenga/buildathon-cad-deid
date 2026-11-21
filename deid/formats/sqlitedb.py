"""SQLite database files (e.g. part catalogs shipped inside archives).

Text cells are rewritten with plan.replace_text through the sqlite3 module on a
copy of the file, then VACUUM rebuilds the file so replaced values do not survive
in free pages. Schema, row counts and non-text values are preserved; verify runs
PRAGMA integrity_check and compares table row counts.
"""
from __future__ import annotations

import os
import shutil
import sqlite3

from deid.api import (Adapter, Observation, RewriteResult, ScanResult, VerifyResult, register,
                      REWRITTEN, SOURCE_EXCEPTION, VERIFIED_UNCHANGED)

MAX_CELLS = 5_000_000


def _tables(con):
    out = []
    for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"):
        if name.startswith("sqlite_") or not sql or "VIRTUAL" in sql.upper() or "WITHOUT ROWID" in sql.upper():
            continue
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
        out.append((name, cols))
    return out


def _counts(con):
    return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n, _ in _tables(con)}


class SqliteAdapter(Adapter):
    name = "sqlite"
    version = "1"
    priority = 20
    magics = ("sqlite",)
    path_independent = True
    timeout_s = 1800.0

    def matches(self, ctx, head):
        return head.startswith(b"SQLite format 3\x00")

    def _open_ro(self, ctx, src):
        tmp = os.path.join(ctx.tmp_dir, "ro.sqlite")
        shutil.copyfile(src, tmp)   # never open the source in place (journals/locks)
        return sqlite3.connect(tmp)

    def scan(self, ctx, src):
        try:
            con = self._open_ro(ctx, src)
            con.text_factory = lambda b: b.decode("utf-8", "replace")
            obs, seen = [], set()
            for t, cols in _tables(con):
                for row in con.execute(f'SELECT * FROM "{t}" LIMIT 200000'):
                    for v in row:
                        if isinstance(v, str) and 3 <= len(v) <= 200 and v not in seen and len(seen) < 2000:
                            seen.add(v)
                            obs.append(Observation("free_text", f"sqlite.{t}", v, confidence=0.2))
            fp = {"tables": _counts(con)}
            con.close()
            return ScanResult(observations=obs, fingerprint=fp)
        except sqlite3.DatabaseError as e:
            return ScanResult(status="source_exception", notes=[f"unreadable database ({type(e).__name__})"])

    def rewrite(self, ctx, src, dst, plan):
        shutil.copyfile(src, dst)
        try:
            con = sqlite3.connect(dst)
            con.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
            con.execute("PRAGMA journal_mode=DELETE")
            changed = cells = 0
            for t, cols in _tables(con):
                qcols = ", ".join(f'"{c}"' for c in cols)
                rows = con.execute(f'SELECT rowid, {qcols} FROM "{t}"').fetchall()
                for row in rows:
                    rid, vals = row[0], row[1:]
                    upd = {}
                    for c, v in zip(cols, vals):
                        cells += 1
                        if isinstance(v, str) and v:
                            try:
                                nv = plan.replace_text(v)
                            except Exception:
                                nv = v
                            if nv != v:
                                upd[c] = nv
                    if upd:
                        sets = ", ".join(f'"{c}"=?' for c in upd)
                        con.execute(f'UPDATE "{t}" SET {sets} WHERE rowid=?', (*upd.values(), rid))
                        changed += len(upd)
                    if cells > MAX_CELLS:
                        raise RuntimeError("database too large for cell rewrite")
            con.commit()
            if not changed:
                con.close()
                os.unlink(dst)
                return RewriteResult(status=VERIFIED_UNCHANGED, counts={"cells": cells})
            con.execute("VACUUM")
            con.close()
            return RewriteResult(status=REWRITTEN, counts={"cells": cells, "cells_replaced": changed})
        except sqlite3.DatabaseError as e:
            if os.path.exists(dst):
                os.unlink(dst)
            return RewriteResult(status=SOURCE_EXCEPTION, notes=[f"unreadable database ({type(e).__name__})"])

    def verify(self, ctx, src, dst, plan):
        try:
            a = self._open_ro(ctx, src)
            b = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
            ok = b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            same = _counts(a) == _counts(b)
            a.close()
            b.close()
            return VerifyResult(ok and same, [] if ok and same else ["integrity or row counts differ"])
        except sqlite3.DatabaseError as e:
            return VerifyResult(False, [type(e).__name__])


register(SqliteAdapter())
