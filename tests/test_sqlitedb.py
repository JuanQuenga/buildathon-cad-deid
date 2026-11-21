import sqlite3

from deid.pipeline import core
import deid.formats.sqlitedb  # noqa: F401  (registers the adapter)
from tests.test_archives import FakePlan


def test_sqlite_rewrite(tmp_path):
    src = tmp_path / "parts.sqlite"
    con = sqlite3.connect(src)
    con.execute("create table parts(id integer primary key, name text, vendor text, mass real, img blob)")
    for i in range(50):
        con.execute("insert into parts(name, vendor, mass, img) values(?,?,?,?)",
                    (f"bolt {i}", "ACME" if i % 2 else "other", i * 1.5, b"\x00\x01"))
    con.execute("delete from parts where id=3")   # leaves a free page candidate
    con.commit(); con.close()
    proc = core.Processor(plan=FakePlan(), tmp_root=str(tmp_path))
    ctx, head = proc.make_ctx("x/parts.sqlite", str(src))
    r = proc.rewrite(ctx, str(src), str(tmp_path / "o.sqlite"), head)
    assert r["status"] == "rewritten", r
    out = (tmp_path / "o.sqlite").read_bytes()
    assert b"ACME" not in out
    c = sqlite3.connect(tmp_path / "o.sqlite")
    assert c.execute("select count(*) from parts").fetchone()[0] == 49
    assert c.execute("select count(*) from parts where vendor='Company-A'").fetchone()[0] == 25
    assert c.execute("select sum(mass) from parts").fetchone()[0] == sum(i * 1.5 for i in range(50) if i != 2)
