#!/usr/bin/env python3
"""English text sprites for the V3 base, kept as PNG files in english/ (no firmware data).

english/sprites.json lists, for each translated sprite, the SHA-1 of the original Japanese pixels
(RGB565 LE) and its PNG. apply() replaces every V3 sprite with those Japanese pixels and the same size.

Usage:
  python3 english.py export V1_ORIG V1_ENGLISH OUT_DIR   # extract the translated sprites of a V1 dump
"""
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dmc_sprites import resource_offsets, find_size_table, sprite, write_png, read_png, rgb565  # noqa: E402

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "english")


def sprites(raw):
    offs = resource_offsets(raw)
    st = find_size_table(raw, offs)
    return [(i, off) + struct.unpack_from("<HH", raw, st + 4 * i) for i, off in enumerate(offs)], offs, st


def export(v1_orig, v1_eng, out_dir):
    o, e = open(v1_orig, "rb").read(), open(v1_eng, "rb").read()
    if len(o) != len(e) or o[:0x80954] != e[:0x80954]:
        raise SystemExit("il V1 inglese cambia codice o tabelle: atteso solo grafica")
    os.makedirs(out_dir, exist_ok=True)
    lst, offs, st = sprites(e)
    manifest = []
    for i, off, w, h in lst:
        a, b = off, off + w * h * 2
        if o[a:b] == e[a:b]:
            continue
        _, _, px = sprite(e, offs, st, i)
        if rgb565(px) != e[a:b]:
            raise SystemExit("sprite %d: conversione PNG non reversibile" % i)
        name = "v1_%04d.png" % i
        write_png(os.path.join(out_dir, name), w, h, [px[y * w:(y + 1) * w] for y in range(h)])
        manifest.append({"png": name, "v1_sprite": i, "width": w, "height": h,
                         "japanese_sha1": hashlib.sha1(o[a:b]).hexdigest()})
    with open(os.path.join(out_dir, "sprites.json"), "w") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")
    return manifest


def apply(v3, eng_dir=DEFAULT_DIR):
    """Replace the Japanese text sprites of the V3 image (bytearray) in place; returns log lines."""
    manifest = json.load(open(os.path.join(eng_dir, "sprites.json")))
    by_hash = {}
    for i, off, w, h in sprites(bytes(v3))[0]:
        by_hash.setdefault(hashlib.sha1(v3[off:off + w * h * 2]).hexdigest(), []).append((i, off, w, h))
    log = []
    for m in manifest:
        w, h, px = read_png(os.path.join(eng_dir, m["png"]))
        if (w, h) != (m["width"], m["height"]):
            raise SystemExit("%s: dimensioni %dx%d, attese %dx%d" % (m["png"], w, h, m["width"], m["height"]))
        targets = by_hash.get(m["japanese_sha1"], [])
        if not targets:
            raise SystemExit("%s: sprite giapponese non trovato in V3" % m["png"])
        for j, off, tw, th in targets:
            if (tw, th) != (w, h):
                raise SystemExit("%s: dimensioni diverse da V3 #%d" % (m["png"], j))
            v3[off:off + w * h * 2] = rgb565(px)
            log.append("inglese %s (%dx%d) -> V3 #%d @ 0x%06x" % (m["png"], w, h, j, off))
    return log


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "export":
        raise SystemExit(__doc__)
    print("%d sprite esportati" % len(export(*sys.argv[2:5])))
