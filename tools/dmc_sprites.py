#!/usr/bin/env python3
"""Sprite access for Digital Monster Color flash dumps (no external dependencies).

Layout (byte offsets in the 8 MB dump):
  0x080000            resource offset table: N x uint32 LE, relative to 0x080000
  0x080000 + off[i]   sprite i, raw RGB565 LE, width*height*2 bytes
  size table          N x (uint16 width, uint16 height), located in the code area
                      (Ver1: word 0x204acc -> byte 0x9598); found automatically.

Usage:
  python3 dmc_sprites.py DUMP.bin sheet OUT.png FIRST [COUNT] [STEP]
      -> contact sheet of sprites FIRST, FIRST+STEP, ... (2x scale)
"""
import struct
import sys
import zlib

RES_BASE = 0x080000


def load(path):
    return open(path, "rb").read()


def resource_offsets(d):
    first = struct.unpack_from("<I", d, RES_BASE)[0]
    n = first // 4
    return [RES_BASE + struct.unpack_from("<I", d, RES_BASE + 4 * i)[0] for i in range(n)]


def find_size_table(d, offs):
    """Find the (w,h) table whose sizes match the gaps between consecutive resources."""
    n = len(offs)
    sizes = [offs[i + 1] - offs[i] for i in range(n - 1)]
    for pos in range(0, 0x20000, 2):
        ok = True
        for i in range(0, min(40, n - 1)):
            w, h = struct.unpack_from("<HH", d, pos + 4 * i)
            if w * h * 2 != sizes[i] or w == 0:
                ok = False
                break
        if ok:
            return pos
    raise RuntimeError("size table not found")


def sprite(d, offs, size_tab, i):
    w, h = struct.unpack_from("<HH", d, size_tab + 4 * i)
    raw = d[offs[i]: offs[i] + w * h * 2]
    px = []
    for k in range(w * h):
        v = raw[2 * k] | (raw[2 * k + 1] << 8)
        r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
        px.append(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
    return w, h, px


def write_png(path, w, h, rows):
    def chunk(t, data):
        c = struct.pack(">I", len(data)) + t + data
        return c + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(v for p in row for v in p) for row in rows)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    open(path, "wb").write(png)


def read_png(path):
    """Minimal PNG reader: 8-bit RGB or RGBA, not interlaced. Returns (w, h, [(r, g, b), ...])."""
    d = open(path, "rb").read()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s: non è un PNG" % path)
    pos, idat, hdr = 8, b"", None
    while pos < len(d):
        n, t = struct.unpack_from(">I4s", d, pos)
        body = d[pos + 8: pos + 8 + n]
        if t == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif t == b"IDAT":
            idat += body
        pos += 12 + n
    w, h, depth, ctype, _, _, interlace = hdr
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise ValueError("%s: serve un PNG RGB/RGBA a 8 bit, non interlacciato" % path)
    bpp = 3 if ctype == 2 else 4
    raw, stride, prev, px = zlib.decompress(idat), w * bpp, bytearray(w * bpp), []
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        px += [tuple(line[x * bpp: x * bpp + 3]) for x in range(w)]
        prev = line
    return w, h, px


def rgb565(px):
    """[(r, g, b)] -> raw RGB565 LE bytes (exact inverse of sprite())."""
    return b"".join(struct.pack("<H", (r >> 3) << 11 | (g >> 2) << 5 | b >> 3) for r, g, b in px)


def sheet(d, out, ids, scale=2, cols=6, cell=52):
    offs = resource_offsets(d)
    st = find_size_table(d, offs)
    rows_n = (len(ids) + cols - 1) // cols
    W, H = cols * cell * scale, rows_n * cell * scale
    img = [[(40, 40, 40)] * W for _ in range(H)]
    for n, i in enumerate(ids):
        w, h, px = sprite(d, offs, st, i)
        ox, oy = (n % cols) * cell * scale, (n // cols) * cell * scale
        for y in range(min(h, cell)):
            for x in range(min(w, cell)):
                for sy in range(scale):
                    for sx in range(scale):
                        img[oy + y * scale + sy][ox + x * scale + sx] = px[y * w + x]
    write_png(out, W, H, img)


if __name__ == "__main__":
    d = load(sys.argv[1])
    if sys.argv[2] == "sheet":
        first = int(sys.argv[4], 0)
        count = int(sys.argv[5], 0) if len(sys.argv) > 5 else 18
        step = int(sys.argv[6], 0) if len(sys.argv) > 6 else 15
        sheet(d, sys.argv[3], [first + k * step for k in range(count)])
