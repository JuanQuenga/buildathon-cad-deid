from conftest import run_adapter

SAT7 = (b"700 0 1 0           \n"
        b"25 Acme Widgets CAD exporter 12 ACIS 7.0 NT 24 Tue Jul 22 00:01:16 2025 \n"
        b"1 9.9999999999999995e-07 1e-10 \n"
        b"body $1 -1 $-1 $2 $-1 $-1 #\n"
        b"string_attrib-name_attrib-gen-attrib $-1 -1 $-1 $-1 $0 2 1 1 @17 ATTRIB_XACIS_NAME @12 Bracket_Main #\n"
        b"lump $-1 -1 $-1 $-1 $3 $0 #\n"
        b"rh_material-rh_entity $-1 -1 4 4 @5 plain @5 color -4 1 1 1 #\n"
        b"named-attrib $-1 -1 @23 has @ and # Jane Roe x #\n"
        b"End-of-ACIS-data \n")

SAT6 = (b"600 0 1 0           \n"
        b"35 Exported by Jane Roe - Acme Widgets 18 ACIS 6.0.3 Solaris 24 Sat Apr 13 20:04:15 2002 \n"
        b"1 9.999999999999999547e-07 1.000000000000000036e-10\n"
        b"body $-1 $1 $-1 $-1 #\n"
        b"attrib_gssl_step_person_details-attrib_gssl_step-attrib $-1 $-1 $-1 $-1 4 Jane 3 Roe 0  0 0 0 #\n"
        b"attrib_gssl_step_org_details-attrib_gssl_step-attrib $-1 $-1 $-1 $-1 12 Acme Widgets 1994 #\n"
        b"End-of-ACIS-data \n")


def test_sat7(tmp_path, plan):
    sr, rr, vr, out = run_adapter(tmp_path, "sat", "a.sat", SAT7, plan)
    assert rr.status == "rewritten"
    assert b"@10 Part_Gamma #" in out
    assert b"25 Company Beta CAD exporter 12 ACIS 7.0 NT" in out
    assert b"@27 has @ and # Person Alpha x #" in out
    assert b"Jane" not in out and b"Acme" not in out
    assert out.endswith(b"End-of-ACIS-data \n")
    vals = {o.value for o in sr.observations}
    assert "Bracket_Main" in vals and "ATTRIB_XACIS_NAME" not in vals


def test_sat6(tmp_path, plan):
    sr, rr, vr, out = run_adapter(tmp_path, "sat", "b.sat", SAT6, plan)
    assert b"6 Person 5 Alpha 0  0 0 0 #" in out
    assert b"12 Company Beta 1994 #" in out
    assert b"Roe" not in out
