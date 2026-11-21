"""Unit tests on synthetic values only (no corpus data)."""
import os
import unittest

from deid.identity.aliases import alias_token, norm_key
from deid.identity.plan import EXACT, FIELD, FLEX, Entry, IdentityPlan, load_plan, save_plan, split_ext

KEY = b"k" * 32


def mk(entries, inv=()):
    return IdentityPlan([Entry(*e) for e in entries], KEY, inv)


class AliasTests(unittest.TestCase):
    def test_deterministic_and_shape(self):
        a = alias_token("Zorblax42", KEY)
        self.assertEqual(a, alias_token("Zorblax42", KEY))
        self.assertEqual(len(a), 9)
        self.assertTrue(a[0].isupper() and a[1:7].islower() and a[7:].isdigit())
        self.assertNotEqual(a.lower(), "zorblax42")

    def test_case_variants_share_letters(self):
        self.assertEqual(alias_token("ZORBLAX", KEY).lower(), alias_token("zorblax", KEY).lower())

    def test_nonascii_same_encoded_length(self):
        for v in ("Bérénice", "Ålesund", "山田太郎", "Ковалёв", "ひらがな"):
            a = alias_token(v, KEY)
            self.assertEqual(len(a), len(v))
            self.assertEqual(len(a.encode("utf-8")), len(v.encode("utf-8")), v)
            self.assertEqual(len(a.encode("utf-16-le")), len(v.encode("utf-16-le")))
            self.assertNotEqual(a, v)

    def test_norm_key(self):
        self.assertEqual(norm_key("ACME_Corp"), norm_key("acme corp"))
        self.assertEqual(norm_key("Acme-Corp."), "acme corp")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.p = mk([
            ("Zorblax Industries", "company", FLEX),
            ("quuxcorp", "account", FLEX),
            ("frobnitz-widget-kit", "project", FLEX),
            ("Jane Q. Smithfield", "person", FLEX),
            ("zorblax.example-co.de", "domain", EXACT, ("de",)),
            ("abc", "account", FIELD),
        ])

    def test_consistent_variants(self):
        r1 = self.p.replace_text("Made by Zorblax Industries.")
        r2 = self.p.replace_text("made by ZORBLAX_INDUSTRIES")
        r3 = self.p.replace_text("zorblaxindustries")
        a1 = r1.split()[2:4]
        self.assertNotIn("Zorblax", r1)
        self.assertEqual(" ".join(a1).lower().rstrip("."), r2.split()[2].replace("_", " ").lower())
        self.assertEqual(r3.lower(), r2.split()[2].replace("_", "").lower())

    def test_length_preserved(self):
        t = "Owner: quuxcorp / Jane Q. Smithfield <jane.smithfield@zorblax.example-co.de>"
        r = self.p.replace_text(t)
        self.assertEqual(len(r), len(t))
        self.assertNotIn("quuxcorp", r)
        self.assertNotIn("Smithfield", r)
        self.assertNotIn("zorblax", r.lower())
        self.assertTrue(r.endswith(".de>"))

    def test_boundaries(self):
        self.assertEqual(self.p.replace_text("quuxcorporation"), "quuxcorporation")
        self.assertNotEqual(self.p.replace_text("quuxcorpParts"), "quuxcorpParts")  # camelCase
        self.assertNotEqual(self.p.replace_text("quuxcorp2"), "quuxcorp2")
        self.assertEqual(self.p.replace_text("abc def"), "abc def")  # FIELD entries not in free text

    def test_no_double_alias(self):
        # a known identity inside a user directory keeps its dictionary alias
        a = self.p.replace_text("quuxcorp")
        r = self.p.replace_text("C:\\Users\\quuxcorp\\Desktop\\x")
        self.assertEqual(r.split("\\")[2], a)
        r2 = self.p.replace_text("C:\\Utilisateurs\\Wilhelmina\\x")
        self.assertNotIn("Wilhelmina", r2)
        self.assertEqual(self.p.replace_text("myhome/thing/x"), "myhome/thing/x")
        e = self.p.replace_text("quuxcorp.x@zorblax.example-co.de")
        self.assertTrue(e.startswith(a + ".x@"))

    def test_field_alias(self):
        a = self.p.alias("abc", "account")
        self.assertIsNotNone(a)
        self.assertEqual(len(a), 3)
        self.assertIsNone(self.p.alias("Unknown", "person"))
        self.assertIsNone(self.p.alias("SolidWorks", "company"))
        self.assertEqual(self.p.alias("Zorblax Industries", "company"), self.p.replace_text("Zorblax Industries"))
        # unseen person in an identity field still gets a pseudonym
        self.assertNotEqual(self.p.alias("Wilhelmina Bogg", "person"), "Wilhelmina Bogg")

    def test_bytes_encodings(self):
        s = "author quuxcorp end"
        for enc in ("utf-8", "latin-1", "utf-16-le"):
            b = ("\x00\x01" + s).encode(enc) if enc != "utf-16-le" else b"\x01" + s.encode(enc)
            r = self.p.replace_bytes(b, (enc,), length_preserving=True)
            self.assertEqual(len(r), len(b))
            self.assertNotIn("quuxcorp".encode(enc), r)

    def test_paths(self):
        p = "sources/github/quuxcorp__frobnitz-widget-kit__0123456789ab/cad/frobnitz-widget-kit_base.prt.12"
        m = self.p.map_path(p)
        self.assertEqual(m.count("/"), p.count("/"))
        self.assertTrue(m.endswith("_base.prt.12"))
        self.assertIn("__0123456789ab/cad/", m)
        self.assertNotIn("quuxcorp", m)
        self.assertNotIn("frobnitz", m)
        slot = m.split("/")[2].split("__")
        self.assertEqual(m.split("/")[-1].split("_base")[0], slot[1])  # same alias in slot and filename
        self.assertEqual(self.p.map_path("sources/web/www.zorblax.example-co.de/a.pdf").split("/")[2][:4], "www.")
        mm = self.p.map_path("archives/quuxcorp__x.zip!/frobnitz-widget-kit-main/a.step")
        self.assertIn("!/", mm)
        self.assertTrue(mm.endswith("-main/a.step"))

    def test_split_ext(self):
        self.assertEqual(split_ext("a.prt.19"), ("a", ".prt.19"))
        self.assertEqual(split_ext("a.b.tar.gz"), ("a.b", ".tar.gz"))
        self.assertEqual(split_ext("README"), ("README", ""))

    def test_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            save_plan(self.p, d)
            q = load_plan(d)
            t = "quuxcorp Zorblax Industries frobnitz-widget-kit"
            self.assertEqual(q.replace_text(t), self.p.replace_text(t))
            self.assertEqual(q.version, self.p.version)


class RefTests(unittest.TestCase):
    def setUp(self):
        inv = [
            "sources/github/quuxcorp__frobnitz-widget-kit/asm/top.SLDASM",
            "sources/github/quuxcorp__frobnitz-widget-kit/parts/Quuxcorp_Bracket.SLDPRT",
            "sources/github/quuxcorp__frobnitz-widget-kit/creo/gear.prt.3",
            "sources/github/quuxcorp__frobnitz-widget-kit/creo/gear.prt.7",
            "archives/quuxcorp__x.zip!/frobnitz-widget-kit-main/a.step",
        ]
        self.p = mk([("quuxcorp", "account", FLEX), ("frobnitz-widget-kit", "project", FLEX)], inv)
        self.ctx = "sources/github/quuxcorp__frobnitz-widget-kit/asm/top.SLDASM"

    def test_relative(self):
        raw = "..\\parts\\QUUXCORP_Bracket.SLDPRT"
        self.assertEqual(self.p.resolve_reference(self.ctx, raw), self.p.inventory[1])
        r = self.p.map_reference(self.ctx, raw)
        self.assertTrue(r.startswith("..\\parts\\"))
        self.assertNotIn("QUUXCORP", r)
        self.assertEqual(r.split("\\")[-1].lower(), self.p.map_path(self.p.inventory[1]).split("/")[-1].lower())

    def test_absolute_windows_userdir(self):
        raw = "C:\\Users\\wbogg\\Desktop\\quuxcorp\\parts\\Quuxcorp_Bracket.SLDPRT"
        r = self.p.map_reference(self.ctx, raw)
        self.assertTrue(r.startswith("C:\\Users\\"))
        self.assertNotIn("wbogg", r)
        self.assertNotIn("quuxcorp", r.lower())
        self.assertEqual(len(r), len(raw))

    def test_creo_unversioned(self):
        self.assertEqual(self.p.resolve_reference(self.ctx, "gear.prt"), self.p.inventory[3])

    def test_member(self):
        ctx = "archives/quuxcorp__x.zip!/frobnitz-widget-kit-main/b.step"
        self.assertEqual(self.p.resolve_reference(ctx, "a.step"), self.p.inventory[4])

    def test_dangling(self):
        r = self.p.map_reference(self.ctx, "/home/wbogg/frobnitz-widget-kit/old.step")
        self.assertNotIn("wbogg", r)
        self.assertNotIn("frobnitz", r)
        self.assertTrue(r.endswith("/old.step"))


if __name__ == "__main__":
    unittest.main()
