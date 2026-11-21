"""Synthetic PDF fixtures only (no corpus data)."""
import subprocess

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, String

from _plan import DictPlan
from deid.api import FileContext, REWRITTEN, VERIFIED_UNCHANGED
from deid.formats.pdf import PdfAdapter, parse_tounicode, FontInfo


def _tounicode(mapping):
    lines = "\n".join(f"<{c:04X}> <{ord(u):04X}>" for c, u in mapping.items())
    return (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            + f"{len(mapping)} beginbfchar\n{lines}\nendbfchar\n".encode()
            + b"endcmap CMapName currentdict /CMap defineresource pop end end")


def make_pdf(path, author="Alice Example"):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(842, 595))
    page = pdf.pages[0]
    helv = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                                        Encoding=Name.WinAnsiEncoding))
    # Type0 Identity-H font whose codes are 0x0100 + ord(ch)
    text2 = "Checked: Bob Sample"
    tu = pdf.make_stream(_tounicode({0x100 + ord(c): c for c in set(text2)}))
    cid = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.CIDFontType2, BaseFont=Name("/ABCDEF+Arial"),
                                       CIDSystemInfo=Dictionary(Registry=String("Adobe"), Ordering=String("Identity"), Supplement=0),
                                       DW=600))
    t0 = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type0, BaseFont=Name("/ABCDEF+Arial"),
                                      Encoding=Name("/Identity-H"), DescendantFonts=Array([cid]), ToUnicode=tu))
    page.obj.Resources = Dictionary(Font=Dictionary(F1=helv, F2=t0))
    codes = b"".join((0x100 + ord(c)).to_bytes(2, "big") for c in text2)
    content = (b"BT /F1 10 Tf 600 60 Td (Drawn by Alice Example) Tj ET\n"
               b"BT /F1 10 Tf 600 40 Td [(Dimension 25.4 mm)] TJ ET\n"
               b"BT /F2 9 Tf 600 20 Td <" + codes.hex().encode() + b"> Tj ET\n"
               b"0 0 m 100 100 l S\n")
    page.obj.Contents = pdf.make_stream(content)
    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
        m["dc:creator"] = [author]
    pdf.docinfo["/Author"] = author
    pdf.docinfo["/Title"] = "Bracket for Example Corp"
    pdf.docinfo["/Producer"] = "SomeWriter 1.0"
    annot = Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 10, 10],
                       A=Dictionary(S=Name.URI, URI=String("https://example-corp.test/alice")))
    page.obj.Annots = pdf.make_indirect(Array([pdf.make_indirect(annot)]))
    pdf.save(path)


PLAN = DictPlan({"Alice Example": "Person Alpha", "Bob Sample": "Person Beta",
                 "Example Corp": "Company Gamma", "example-corp.test": "company-gamma.test"})


def _text(p):
    return subprocess.run(["pdftotext", p, "-"], capture_output=True, text=True).stdout


def test_pdf_roundtrip(tmp_path):
    src, dst = str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")
    make_pdf(src)
    ad = PdfAdapter()
    ctx = FileContext(logical_path="x/a.pdf", magic="pdf", tmp_dir=str(tmp_path))
    s = ad.scan(ctx, src)
    vals = {(o.kind, o.value) for o in s.observations}
    assert ("person", "Alice Example") in vals
    assert any(r.kind == "uri" for r in s.references)
    r = ad.rewrite(ctx, src, dst, PLAN)
    assert r.status == REWRITTEN
    assert r.counts["text_spans_replaced"] >= 2
    v = ad.verify(ctx, src, dst, PLAN)
    assert v.ok, v.notes
    t = _text(dst)
    assert "Alice Example" not in t and "Bob Sample" not in t
    assert "Person Alpha" in t and "Person Beta" in t
    assert "Dimension 25.4 mm" in t and "Drawn by" in t and "Checked:" in t
    with pikepdf.open(dst) as p:
        assert str(p.docinfo["/Author"]) == "Person Alpha"
        assert str(p.docinfo["/Title"]) == "Bracket for Company Gamma"
        assert str(p.docinfo["/Producer"]) == "SomeWriter 1.0"
        assert "Alice" not in p.Root.Metadata.read_bytes().decode()
        uri = str(p.pages[0].obj.Annots[0].A.URI)
        assert "company-gamma.test" in uri
    raw = open(dst, "rb").read()
    assert b"Alice Example" not in raw


def test_pdf_unchanged(tmp_path):
    src, dst = str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")
    make_pdf(src, author="Nobody Known")
    ad = PdfAdapter()
    ctx = FileContext(logical_path="x/a.pdf", magic="pdf", tmp_dir=str(tmp_path))
    r = ad.rewrite(ctx, src, dst, DictPlan({}))
    assert r.status == VERIFIED_UNCHANGED


def test_tounicode_ranges():
    data = (b"1 begincodespacerange <00> <FF> endcodespacerange\n"
            b"1 beginbfrange <41> <43> <0061> endbfrange\n"
            b"1 beginbfrange <50> <51> [<0078> <0079>] endbfrange\n")
    m, sp = parse_tounicode(data)
    assert m[0x41] == "a" and m[0x43] == "c" and m[0x51] == "y"
    assert sp == [(1, 0, 255)]
