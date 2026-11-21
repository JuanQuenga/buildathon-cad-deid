"""Minimal CFB (v3, 512-byte sectors) writer for synthetic test fixtures."""
import struct

END, FREE, FATSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD


def _dirent(name, typ, start, size, left=FREE, right=FREE, child=FREE):
    n = name.encode("utf-16-le") + b"\x00\x00"
    e = n.ljust(64, b"\x00") + struct.pack("<HBB", len(n), typ, 1) + struct.pack("<III", left, right, child)
    e += b"\x00" * 16 + b"\x00" * 4 + b"\x00" * 16 + struct.pack("<IQ", start, size)
    return e


def write_cfb(path, streams):
    """streams: list of (name, bytes) at root level."""
    sectors = []
    fat = [FATSECT, END]           # sector 0 = FAT, sector 1 = directory

    def append_blob(data, width, chain, blocks):
        start = len(chain)
        padded = data + bytes((-len(data)) % width)
        for off in range(0, len(padded), width):
            successor = len(chain) + 1 if off + width < len(padded) else END
            chain.append(successor)
            blocks.append(padded[off:off + width])
        return start if padded else END

    starts, mini, mini_fat = [], [], []
    for name, data in streams:
        starts.append(append_blob(data, 64, mini_fat, mini) if len(data) < 4096
                      else append_blob(data, 512, fat, sectors))
    mini_root = append_blob(b"".join(mini), 512, fat, sectors)
    if mini_fat:
        assert len(mini_fat) <= 128
        data = struct.pack("<128I", *(mini_fat + [FREE] * (128 - len(mini_fat))))
        mini_fat_start = append_blob(data, 512, fat, sectors)
    else:
        mini_fat_start = END
    assert len(fat) <= 128
    fat += [FREE] * (128 - len(fat))
    ents = [_dirent("Root Entry", 5, mini_root, 64 * len(mini), child=1)]
    for i, (name, data) in enumerate(streams):
        right = i + 2 if i + 1 < len(streams) else FREE
        ents.append(_dirent(name, 2, starts[i], len(data), right=right))
    d = b"".join(ents)
    d += (_dirent("", 0, 0, 0)) * ((-len(ents)) % 4)
    assert len(d) == 512
    hdr = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
           + b"\x00" * 6 + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, mini_fat_start, int(bool(mini_fat)), END, 0))
    hdr += struct.pack("<I", 0) + struct.pack("<I", FREE) * 108
    assert len(hdr) == 512
    with open(path, "wb") as f:
        f.write(hdr + struct.pack("<128I", *fat) + d + b"".join(sectors))


def propset(fmtid_bytes, props, codepage=1252, names=None, *, pad=True):
    """Construct a property set with optional dictionary and code-page strings."""
    allp = [(1, struct.pack("<IhH", 2, codepage, 0))]
    enc = "utf-16-le" if codepage == 1200 else f"cp{codepage}"
    nul = "\x00".encode(enc)
    if names:
        assert codepage != 1200  # current fixtures only need ANSI dictionaries
        entries = []
        for pid, name in names.items():
            data = name.encode(enc) + nul
            entries.append(struct.pack("<II", pid, len(data)) + data)
        dictionary = struct.pack("<I", len(entries)) + b"".join(entries)
        allp.append((0, dictionary + bytes((-len(dictionary)) % 4)))
    for pid, s in props:
        b = s.encode(enc) + nul
        value = struct.pack("<II", 0x1E, len(b)) + b + bytes((-len(b)) % 4)
        allp.append((pid, value))
    off = 8 + 8 * len(allp)
    table = b""
    vals = b""
    for pid, v in allp:
        table += struct.pack("<II", pid, off + len(vals))
        vals += v
    sec = struct.pack("<II", 8 + len(table) + len(vals), len(allp)) + table + vals
    hdr = b"\xfe\xff\x00\x00" + b"\x00" * 4 + b"\x00" * 16 + struct.pack("<I", 1) + fmtid_bytes + struct.pack("<I", 48)
    data = hdr + sec
    return data + bytes(4096 - len(data)) if pad else data
