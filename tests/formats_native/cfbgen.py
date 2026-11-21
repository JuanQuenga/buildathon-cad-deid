"""Minimal CFB (v3, 512-byte sectors) writer for synthetic test fixtures.
Streams must be >= 4096 bytes (no mini stream)."""
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
    starts = []
    nxt = 2
    for name, data in streams:
        assert len(data) >= 4096
        data = data + b"\x00" * ((-len(data)) % 512)
        n = len(data) // 512
        starts.append(nxt)
        for i in range(n):
            fat.append(nxt + i + 1 if i < n - 1 else END)
            sectors.append(data[i * 512:(i + 1) * 512])
        nxt += n
    assert len(fat) <= 128
    fat += [FREE] * (128 - len(fat))
    ents = [_dirent("Root Entry", 5, END, 0, child=1)]
    for i, (name, data) in enumerate(streams):
        right = i + 2 if i + 1 < len(streams) else FREE
        ents.append(_dirent(name, 2, starts[i], len(data), right=right))
    d = b"".join(ents)
    d += (_dirent("", 0, 0, 0)) * ((-len(ents)) % 4)
    assert len(d) == 512
    hdr = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
           + b"\x00" * 6 + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, END, 0, END, 0))
    hdr += struct.pack("<I", 0) + struct.pack("<I", FREE) * 108
    assert len(hdr) == 512
    with open(path, "wb") as f:
        f.write(hdr + struct.pack("<128I", *fat) + d + b"".join(sectors))


def propset(fmtid_bytes, props, codepage=1252):
    """props: list of (pid, str) stored as VT_LPSTR (cp1252) -> stream bytes (padded)."""
    body = []
    allp = [(1, struct.pack("<IhH", 2, codepage, 0))]
    for pid, s in props:
        b = s.encode("cp1252") + b"\x00"
        b += b"\x00" * ((-len(b)) % 4)
        allp.append((pid, struct.pack("<II", 0x1E, len(s) + 1) + b))
    off = 8 + 8 * len(allp)
    table = b""
    vals = b""
    for pid, v in allp:
        table += struct.pack("<II", pid, off + len(vals))
        vals += v
    sec = struct.pack("<II", 8 + len(table) + len(vals), len(allp)) + table + vals
    hdr = b"\xfe\xff\x00\x00" + b"\x00" * 4 + b"\x00" * 16 + struct.pack("<I", 1) + fmtid_bytes + struct.pack("<I", 48)
    data = hdr + sec
    return data + b"\x00" * (4096 - len(data))
