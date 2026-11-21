from conftest import run_adapter, StubPlan

STAR = b"**ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz**************************\n" \
       b"**PARASOLID !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~0123456789**************************\n"


def test_parasolid_lines(tmp_path, plan):
    data = (STAR + b"**PART1;\nMC=AMD64;\nAPPL=SomeCAD 2020;\nSITE=Acme Widgets;\nUSER=jroe;\nFILE=Bracket_Main.x_t;\n"
            b"**PART2;\nSCH=SCH_1_2;\n**PART3;\n**END_OF_HEADER" + b"*" * 65 + b"\n"
            b"T51 : TRANSMIT FILE created by modeller version 3110290 SCH_3110290_31006_13006\n1 2 3 Jane Roe\n")
    sr, rr, vr, out = run_adapter(tmp_path, "parasolid_text", "a.x_t", data, plan)
    assert b"SITE=Company Beta;\nUSER=user01;\nFILE=Part_Gamma.x_t;" in out
    assert out.endswith(b"1 2 3 Jane Roe\n")  # body untouched


def test_parasolid_wrapped(tmp_path, plan):
    hdr = b"**PART1;MC=sun;MC_MODEL=Sun-3;MC_ID=unknown;OS=SunOS;OS_RELEASE=unknown;FRU=sdl_parasolid;APPL=x;SITE=Acme Widgets;USER=jroe;FORMAT=text;"
    lines = b"\n".join(hdr[i:i + 80] for i in range(0, len(hdr), 80)) + b"\n"
    data = STAR + lines + b"**PART2;SCH=SCH_1;\n**PART3;\n**END_OF_HEADER" + b"*" * 65 + b"\nT1 body\n"
    sr, rr, vr, out = run_adapter(tmp_path, "parasolid_text", "b.xmt_txt", data, plan)
    head = out.split(b"**PART2;")[0].split(b"**PART1;")[0]
    block = b"**PART1;" + out.split(b"**PART1;")[1].split(b"**PART2;")[0]
    assert all(len(l) == 80 for l in block.splitlines()[:-1])
    assert b"SITE=Company Beta;USER=user01;" in b"".join(block.splitlines())


def test_unv(tmp_path, plan):
    data = (b"    -1\n   151\n/home/jroe/model/Bracket_Main.mf1\nBracket_Main model by Jane Roe\nI-deas 6\n"
            b"12-Jan-98   10:46:48\n    -1\n    -1\n  2411\n         1         1         1        11\n"
            b"   1.0000000000000000E+00   2.5000000000000000E+00   0.0000000000000000D+00\n    -1\n")
    sr, rr, vr, out = run_adapter(tmp_path, "unv", "a.unv", data, plan)
    assert b"/home/user01/model/Part_Gamma.mf1\nPart_Gamma model by Person Alpha\nI-deas 6\n" in out
    assert out.endswith(b"   1.0000000000000000E+00   2.5000000000000000E+00   0.0000000000000000D+00\n    -1\n")


def test_scad(tmp_path, plan):
    data = b'// Bracket_Main by Jane Roe\ninclude <libs/Acme Widgets.scad>\nx = 10; /* (c) Acme Widgets */\ntext("ACME-4711");\n'
    sr, rr, vr, out = run_adapter(tmp_path, "scad", "a.scad", data, plan)
    assert out == (b'// Part_Gamma by Person Alpha\ninclude <libs/Company Beta.scad>\nx = 10; /* (c) Company Beta */\n'
                   b'text("PART-0001");\n')


def test_neu(tmp_path, plan):
    data = (b"#UGC:2 NEUTRAL 639 185 0 0 0 @0    0\n#Pro/ENGINEER  TM  Release 11.0\n0 Neutral_part ->\n"
            b"1 revnum 179\n2 outline[0] -3.1925,-1.4985,0.\n3 name Bracket_Main\n3 owner Jane Roe\n")
    sr, rr, vr, out = run_adapter(tmp_path, "neu", "a.neu", data, plan)
    assert out.endswith(b"3 name Part_Gamma\n3 owner Person Alpha\n")
    assert b"2 outline[0] -3.1925,-1.4985,0.\n" in out


def test_text_free_and_fixed(tmp_path, plan):
    sr, rr, vr, out = run_adapter(tmp_path, "text", "notes.txt", b"Contact Jane Roe at Acme Widgets\nline2\n", plan)
    assert out == b"Contact Person Alpha at Company Beta\nline2\n"
    data = b"ROW  Jane Roe      0012.50\n"
    sr, rr, vr, out = run_adapter(tmp_path, "text", "table.zzz", data, plan)
    assert len(out) == len(data)


def test_brep_claimed(tmp_path, plan):
    data = b"DBRep_DrawableShape\n\nCASCADE Topology V1, (c) Matra-Datavision\nLocations 0\n"
    sr, rr, vr, out = run_adapter(tmp_path, "brep", "a.brep", data, plan)
    assert rr.status == "verified_unchanged"
