"""A tiny dictionary-backed Plan for adapter tests (no raw corpus values)."""
import re


class DictPlan:
    version = "test-1"

    def __init__(self, mapping=None, paths=None, case_insensitive=False):
        self.mapping = dict(mapping or {})
        self.paths = dict(paths or {})
        keys = sorted(self.mapping, key=len, reverse=True)
        flags = re.I if case_insensitive else 0
        self._rx = re.compile("|".join(re.escape(k) for k in keys), flags) if keys else None
        self._lower = {k.lower(): v for k, v in self.mapping.items()}

    def alias(self, value, kind=None):
        return self.mapping.get(value)

    def replace_text(self, text):
        if not self._rx or not text:
            return text
        return self._rx.sub(lambda m: self.mapping.get(m.group(0), self._lower.get(m.group(0).lower(), m.group(0))), text)

    def replace_bytes(self, data, encodings=("utf-8",), length_preserving=False):
        for enc in encodings:
            for k, v in self.mapping.items():
                kb, vb = k.encode(enc), v.encode(enc)
                if length_preserving:
                    if len(vb) > len(kb):
                        continue
                    vb = vb + b" " * (len(kb) - len(vb))
                data = data.replace(kb, vb)
        return data

    def map_path(self, logical_path):
        out = logical_path
        for k, v in self.paths.items():
            out = out.replace(k, v)
        return out

    def map_reference(self, ctx, raw_ref):
        out = raw_ref
        for k, v in self.paths.items():
            out = out.replace(k, v)
        return out
