"""Regression: replace_text / replace_bytes / map_path are idempotent on their own output
(synthetic values only)."""
import unittest

from deid.identity.build import _mode_for
from deid.identity.plan import EXACT, FIELD, FLEX, Entry, IdentityPlan

KEY = b"q" * 32
ENTRIES = [
    ("Zorblax Quentaro", "person", FLEX), ("Vexmira Holdings", "company", FLEX),
    ("qzorbl", "account", FLEX), ("vexmira.example-corp.io", "domain", EXACT),
    ("Plonkatron", "project", FLEX), ("Fizwick", "person", FIELD),
]
TEXTS = [
    r"C:\Users\qzorbl\Desktop\Plonkatron\part.SLDPRT",
    r"C:\Users\Wobblethorpe\Documents\x.step",
    "/home/grentle/cad/model.stp",
    "Drawn by Zorblax Quentaro for Vexmira Holdings (zorblax.quentaro@vexmira.example-corp.io)",
    "mail plonk@unrelated-host.net and qzorbl@vexmira.example-corp.io",
    "https://vexmira.example-corp.io/Plonkatron/v2",
]
PATHS = [
    "sources/github/qzorbl__Plonkatron__0123456789ab/cad/Plonkatron_base.step",
    "sources/catalog/Vexmira Holdings/Zorblax parts/a.prt.3",
    "sources/web/vexmira.example-corp.io/files/b.zip!/Plonkatron/c.stl",
]


class Idempotence(unittest.TestCase):
    def setUp(self):
        self.p = IdentityPlan([Entry(v, k, m) for v, k, m in ENTRIES], KEY, PATHS)

    def test_replace_text(self):
        for t in TEXTS:
            r1 = self.p.replace_text(t)
            self.assertEqual(self.p.replace_text(r1), r1, t)

    def test_userdir_aliased_once(self):
        r1 = self.p.replace_text(TEXTS[1])
        self.assertNotIn("Wobblethorpe", r1)
        self.assertEqual(self.p.replace_text(r1), r1)

    def test_replace_bytes(self):
        for t in TEXTS:
            for enc in ("utf-8", "utf-16-le"):
                b1 = self.p.replace_bytes(t.encode(enc), encodings=(enc,))
                self.assertEqual(self.p.replace_bytes(b1, encodings=(enc,)), b1, (t, enc))
                lp = self.p.replace_bytes(t.encode(enc), encodings=(enc,), length_preserving=True)
                self.assertEqual(self.p.replace_bytes(lp, encodings=(enc,), length_preserving=True), lp)

    def test_map_path(self):
        for path in PATHS:
            m1 = self.p.map_path(path)
            self.assertEqual(m1.count("/"), path.count("/"))
            self.assertEqual(self.p.map_path(m1), m1, path)


class ProtectedVocabulary(unittest.TestCase):
    def test_generic_words_not_identities(self):
        for v in ("config", "Config-0", "CONFIG_CONTROL_DESIGN", "Noname", "design_owner", "approverID",
                  "Jetson", "Default 2"):
            self.assertIsNone(_mode_for(v, kind="person") if v.lower() not in ("jetson",) else
                              (None if _mode_for(v, kind="project") in (None, FIELD) else "aliased"), v)

    def test_alias_none_for_vendor_words(self):
        mode = _mode_for("Jetson", kind="project")
        self.assertIn(mode, (None, FIELD))
        p = IdentityPlan([Entry("Jetson", "project", mode)] if mode else [], KEY)
        self.assertIsNone(p.alias("Jetson"))
        self.assertIn("Jetson", p.replace_text("Jetson Nano carrier"))


if __name__ == "__main__":
    unittest.main()


class FieldGuard(unittest.TestCase):
    def test_field_entries_not_used_in_part_slots(self):
        p = IdentityPlan([Entry("Zorbeline", "person", FIELD)], KEY)
        self.assertIsNotNone(p.alias("Zorbeline", "person"))
        self.assertIsNone(p.alias("Zorbeline", "part_number"))
        self.assertIsNone(p.alias("design", "part_number"))


class EntryScope(unittest.TestCase):
    def test_entry_scope_no_shared_tokens(self):
        ents = [Entry("Zorblax Quentaro", "person", FLEX), Entry("Zorblax Wimmeridge", "person", FLEX)]
        tok = IdentityPlan(ents, KEY)
        ent = IdentityPlan(ents, KEY, meta={"alias_scope": "entry"})
        a, b = tok.replace_text("Zorblax Quentaro"), tok.replace_text("Zorblax Wimmeridge")
        self.assertEqual(a.split()[0], b.split()[0])          # token scope shares the first-name alias
        a, b = ent.replace_text("Zorblax Quentaro"), ent.replace_text("Zorblax Wimmeridge")
        self.assertNotEqual(a.split()[0], b.split()[0])       # entry scope does not
        self.assertEqual(ent.replace_text("ZORBLAX QUENTARO").lower(), a.lower())   # case variants agree
        for t in ("Zorblax Quentaro", r"C:\Users\Zorblax Quentaro\x"):
            r1 = ent.replace_text(t)
            self.assertEqual(ent.replace_text(r1), r1)
        self.assertNotEqual(tok.version, ent.version)
