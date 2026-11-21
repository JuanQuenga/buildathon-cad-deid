"""Private sqlite stores in WORK/: scan.sqlite (observations) and results.sqlite (rewrites)."""
from __future__ import annotations

import sqlite3

SCAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INT, sha256 TEXT, magic TEXT,
  adapter TEXT, adapter_version TEXT, status TEXT, fingerprint TEXT, notes TEXT, error TEXT,
  seconds REAL, parent TEXT);
CREATE TABLE IF NOT EXISTS observations(path TEXT, kind TEXT, field TEXT, value TEXT,
  evidence TEXT, confidence REAL);
CREATE TABLE IF NOT EXISTS refs(path TEXT, raw TEXT, kind TEXT, field TEXT);
CREATE INDEX IF NOT EXISTS obs_value ON observations(value);
CREATE INDEX IF NOT EXISTS obs_path ON observations(path);
CREATE INDEX IF NOT EXISTS refs_path ON refs(path);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS files_parent ON files(parent);
"""

RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS results(path TEXT PRIMARY KEY, out_path TEXT, key TEXT, status TEXT,
  adapter TEXT, adapter_version TEXT, counts TEXT, notes TEXT, error TEXT, out_size INT,
  seconds REAL, children TEXT, magic TEXT, in_size INT);
CREATE INDEX IF NOT EXISTS results_out ON results(out_path);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: str, schema: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(schema)
    return con


def scan_db(work: str) -> sqlite3.Connection:
    return connect(f"{work}/scan.sqlite", SCAN_SCHEMA)


def results_db(work: str) -> sqlite3.Connection:
    return connect(f"{work}/results.sqlite", RESULTS_SCHEMA)


def delete_scan_rows(con, path: str):
    """Remove a file and all of its members' rows (index range scans, no LIKE)."""
    lo, hi = path + "!/", path + "!0"   # '0' sorts right after '/'
    for t in ("observations", "refs", "files"):
        con.execute(f"DELETE FROM {t} WHERE path=? OR (path>=? AND path<?)", (path, lo, hi))
