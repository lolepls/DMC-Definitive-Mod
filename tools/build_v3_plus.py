#!/usr/bin/env python3
"""Build "DMC Ver.3 Plus": English V3 + the whole Ver.1 line + egg choice at hatching.

Starts from the original V3 dump (plus the English sprites from build_v3_english) and:

  sprites   appends the 270 Ver.1 Digimon frames and 9 Ver.1-specific sprites (eggs,
            tombstone, backgrounds) as new indices 628..906, deduplicated, at byte 0x2E2600+;
            new offset table at byte 0x3F0000, new size table in the free code area.
  roster    stats table grows to 38 rows (V3 ids 0..19, V1 ids 0..17 -> 20..37);
            every "id <= 19" check in the code becomes "id <= 37".
  evolution the call to the V3 evolution switch goes through a wrapper: ids >= 20 run a
            relocated copy of the Ver.1 switch (id - 20, then + 20).
  sprites   draw_sprite() remaps the version-specific sprites to the V1 copies while the
            current Digimon belongs to the V1 line (id >= 20).
  egg       the hatching screen (screen 3) gets a new first sub-state: the egg is shown,
            button A (0x08) switches V3 egg <-> V1 egg, button B (0x10) confirms; then the
            original hatching animation runs. pet_init() keeps the line of the previous pet.

All new code/tables live in the unused end of the code segment (words 0x20F400-0x20FFFF).

Usage:
  python3 build_v3_plus.py V1_ORIG V3_ORIG OUT.bin [--skip-dump-check]
"""
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_v3_english import build as build_english  # noqa: E402
import dumps  # noqa: E402
from dmc_sprites import resource_offsets, find_size_table  # noqa: E402
from dmc_stats import find_get_stat  # noqa: E402
from unsp_asm import Asm, R1, R2, R3, R4, self_test  # noqa: E402
from unsp_dis import decode  # noqa: E402

V1_LINE = 20                 # first id of the V1 line in the merged roster
V1_COUNT = 18
NFIELDS = 17
V3_NSPR = 628                # sprites in the V3 resource table
V1_DIGI_SPR = (210, 480)     # V1 Digimon frames (18 x 15)
V1_EXTRA = [122, 123, 133, 134, 135, 160, 180, 181, 182]   # version-specific sprites
PIX_START = 0x2E2600         # free bytes after the last V3 sprite
OFFTAB = 0x3F0000            # new offset table (hi = 0x3F, lo = 0)
CODE_ORG = 0x20F400          # free words at the end of segment 0x20
CODE_END = 0x210000

# V3 routines / addresses (all checked below against the original words)
PET_INIT = 0x208E0D
EVO_V3 = 0x202111
HATCH_SUB = 0x200453         # original sub-state 0 of screen 3 (hatching animation)
CLEAR_SPR, DRAW_BG, DRAW_SPR, FLUSH = 0x2072F5, 0x20267C, 0x2075FC, 0x202724
BEEP, RESET_TIMERS, SAVE = 0x200069, 0x20912A, 0x203B88
V1_EVO = (0x202095, 0x20216D, 0x202189)   # code start, jump table, end (V1)

ID, KEY, SUBST, SUBST_PREV, BG = 0x0826, 0x081B, 0x0C8A, 0x0C8B, 0x0824


def w2b(w):
    return (w - 0x200000) * 2


class Image:
    def __init__(self, data):
        self.b = bytearray(data)
        self.log = []

    def rd(self, w):
        return struct.unpack_from("<H", self.b, w2b(w))[0]

    def wr(self, w, v):
        struct.pack_into("<H", self.b, w2b(w), v & 0xFFFF)

    def patch(self, w, old, new, why):
        old = [old] if isinstance(old, int) else old
        new = [new] if isinstance(new, int) else new
        cur = [self.rd(w + i) for i in range(len(old))]
        if cur != old:
            raise SystemExit("patch %s @%06x: atteso %s, trovato %s"
                             % (why, w, [hex(x) for x in old], [hex(x) for x in cur]))
        for i, v in enumerate(new):
            self.wr(w + i, v)
        self.log.append("%06x  %-40s %s -> %s" % (w, why, " ".join("%04x" % x for x in old),
                                                   " ".join("%04x" % x for x in new)))

    def put_words(self, w, words, why):
        for i, v in enumerate(words):
            if self.rd(w + i) != 0:
                raise SystemExit("area non libera a %06x (%s)" % (w + i, why))
            self.wr(w + i, v)
        self.log.append("%06x  %-40s %d word" % (w, why, len(words)))


def dump_reader(raw):
    return lambda w: struct.unpack_from("<H", raw, w2b(w))[0]


# --------------------------------------------------------------------------- sprites
def add_sprites(img, v1):
    v3 = bytes(img.b)
    offs3 = resource_offsets(v3)
    st3 = find_size_table(v3, offs3)
    offs1 = resource_offsets(v1)
    st1 = find_size_table(v1, offs1)
    assert len(offs3) == V3_NSPR, len(offs3)

    sizes = [struct.unpack_from("<HH", v3, st3 + 4 * i) for i in range(V3_NSPR)]
    rel = [o - 0x80000 for o in offs3]

    src = list(range(*V1_DIGI_SPR)) + V1_EXTRA
    new_index = {}
    seen = {}
    pos = PIX_START
    for k, s in enumerate(src):
        w, h = struct.unpack_from("<HH", v1, st1 + 4 * s)
        px = v1[offs1[s]: offs1[s] + w * h * 2]
        key = hashlib.sha1(px).digest() + bytes([w & 0xFF, h & 0xFF])
        if key not in seen:
            if any(img.b[pos:pos + len(px)]):
                raise SystemExit("area pixel non libera a 0x%x" % pos)
            img.b[pos:pos + len(px)] = px
            seen[key] = pos
            pos = (pos + len(px) + 15) & ~15
        new_index[s] = V3_NSPR + k
        rel.append(seen[key] - 0x80000)
        sizes.append((w, h))
    if pos > OFFTAB:
        raise SystemExit("pixel V1 oltre 0x%x" % OFFTAB)
    n = len(rel)
    if any(img.b[OFFTAB:OFFTAB + 4 * n]):
        raise SystemExit("area tabella offset non libera")
    for i, r in enumerate(rel):
        struct.pack_into("<I", img.b, OFFTAB + 4 * i, r)
    img.log.append("sprite: %d nuovi (%d unici), pixel 0x%06x-0x%06x, offset table @0x%06x"
                   % (len(src), len(seen), PIX_START, pos, OFFTAB))
    return new_index, sizes


# --------------------------------------------------------------------------- V1 evolution switch
def relocate_v1_evo(v1, dst):
    rd = dump_reader(v1)
    start, table, end = V1_EVO
    delta = dst - start
    words = [rd(a) for a in range(start, end)]
    inside = lambda t: start <= t < end  # noqa: E731
    a = start
    while a < table:
        i = decode(rd, a)
        k = a - start
        if i.kind == "jmp" and i.size == 2 and i.target is not None:
            if not inside(i.target):
                raise SystemExit("salto lungo fuori dallo switch V1 a %06x" % a)
            t = i.target + delta
            words[k], words[k + 1] = (words[k] & 0xFFC0) | (t >> 16), t & 0xFFFF
        if i.kind == "call":
            raise SystemExit("call inattesa nello switch V1 a %06x" % a)
        if words[k] == 0x970B and rd(a + 1) == (table & 0xFFFF) and rd(a + 2) == 0x9860:
            words[k + 1] = (table + delta) & 0xFFFF
        a += i.size
    ntab = 0
    for a in range(table, end, 2):
        t = rd(a) | rd(a + 1) << 16
        if not start <= t < table:
            raise SystemExit("voce jump table V1 inattesa %06x" % t)
        t += delta
        words[a - start], words[a + 1 - start] = t & 0xFFFF, t >> 16
        ntab += 1
    assert ((dst + len(words)) >> 16) == (dst >> 16) == 0x20
    return words, ntab


# --------------------------------------------------------------------------- new code
def new_code(remap, sprite_max):
    a = Asm(CODE_ORG)

    # draw_sprite() hook: [bp+0x2a] = sprite index. V1 line -> V1 copies; clamp to range.
    a.label("remap")
    a.ld(R4, bp=0x2A)
    a.ld(R3, imm=ID)
    a.ldi(R3, R3)
    a.cmp(R3, V1_LINE)
    a.j("jb", "remap_clamp")
    for old, new in sorted(remap.items()):
        a.cmp(R4, old)
        a.j("jne", "remap_n%d" % old)
        a.ld(R4, imm=new)
        a.j("jmp", "remap_store")
        a.label("remap_n%d" % old)
    a.label("remap_clamp")
    a.cmp(R4, sprite_max)
    a.j("jbe", "remap_store")
    a.ld(R4, imm=0)
    a.label("remap_store")
    a.st_bp(R4, 0x2A)
    a.retf()

    # pet_init() hook (replaces "r2 = 0x00 ; r3 = [bp+3]"): new id keeps the old line.
    a.label("petinit")
    a.ld(R3, bp=0x03)
    a.cmp(R3, ID)                      # only the current pet (not the 0x0838 record)
    a.j("jne", "petinit_v3")
    a.ldi(R2, R3)
    a.cmp(R2, V1_LINE)
    a.j("jb", "petinit_v3")
    a.cmp(R2, V1_LINE + V1_COUNT - 1)
    a.j("ja", "petinit_v3")
    a.ld(R2, imm=V1_LINE)
    a.retf()
    a.label("petinit_v3")
    a.ld(R2, imm=0)
    a.retf()

    # evolution wrapper
    a.label("evo")
    a.ld(R4, imm=ID)
    a.ldi(R3, R4)
    a.cmp(R3, V1_LINE)
    a.j("jae", "evo_v1")
    a.jmpf(EVO_V3)
    a.label("evo_v1")
    a.sub(R3, V1_LINE)
    a.sti(R3, R4)
    a.call("v1evo")
    a.ld(R4, imm=ID)
    a.ldi(R3, R4)
    a.add(R3, V1_LINE)
    a.sti(R3, R4)
    a.retf()

    # screen 3, new sub-state 0: choose the egg
    a.label("egg")
    a.ld(R4, imm=SUBST)
    a.ldi(R3, R4)
    a.ld(R4, imm=SUBST_PREV)
    a.ldi(R4, R4)
    a.cmp(R3, rs=R4)
    a.j("je", "egg_run")
    a.ld(R4, imm=SUBST_PREV)           # first frame of the sub-state
    a.sti(R3, R4)
    a.ld(R4, imm=ID)                   # id must be one of the two eggs
    a.ldi(R3, R4)
    a.cmp(R3, V1_LINE)
    a.j("je", "egg_draw")
    a.ld(R3, imm=0)
    a.sti(R3, R4)
    a.j("jmp", "egg_draw")
    a.label("egg_run")
    a.ld(R4, imm=KEY)
    a.ldi(R3, R4)
    a.cmp(R3, 0x08)
    a.j("je", "egg_next")
    a.cmp(R3, 0x10)
    a.j("je", "egg_ok")
    a.retf()
    a.label("egg_next")
    a.ld(R3, imm=1)
    a.push(R3)
    a.call(BEEP)
    a.sp_add(1)
    a.ld(R4, imm=ID)
    a.ldi(R3, R4)
    a.xor(R3, V1_LINE)                 # 0 <-> 20
    a.sti(R3, R4)
    a.j("jmp", "egg_draw")
    a.label("egg_ok")
    a.ld(R3, imm=1)
    a.push(R3)
    a.call(BEEP)
    a.sp_add(1)
    a.ld(R3, imm=0)                    # pet_init(&0x0826) -> id kept by the hook
    a.push(R3)
    a.ld(R3, imm=ID)
    a.push(R3)
    a.call(PET_INIT)
    a.sp_add(2)
    a.ld(R4, imm=ID)
    a.ldi(R3, R4)
    a.push(R3)
    a.call(RESET_TIMERS)
    a.sp_add(1)
    a.call(SAVE)
    a.ld(R3, imm=1)                    # next: original hatching sub-state
    a.ld(R4, imm=SUBST)
    a.sti(R3, R4)
    a.retf()
    a.label("egg_draw")
    a.call(CLEAR_SPR)
    a.ld(R4, imm=BG)
    a.ldi(R3, R4)
    a.push(R3)
    a.call(DRAW_BG)
    a.sp_add(1)
    for v in (1, 0x18, 0x18, 133, 9):  # draw_sprite(slot 9, egg 133, x 24, y 24, 1)
        a.ld(R3, imm=v)
        a.push(R3)
    a.call(DRAW_SPR)
    a.sp_add(5)
    a.call(FLUSH)
    a.retf()

    # screen 3 sub-state table: [egg, original hatching], count
    a.label("subtab")
    a.addr("egg"); a.w(CODE_ORG >> 16)
    a.w(HATCH_SUB & 0xFFFF, HATCH_SUB >> 16)
    a.label("subcount")
    a.w(2)
    a.label("v1evo")
    return a


def build(v1_orig, v3_orig, check=True):
    assert self_test()
    if check:
        dumps.check("V1", v1_orig)
    data, log = build_english(v3_orig, False, check)
    img = Image(data)
    img.log += log
    v1 = open(v1_orig, "rb").read()
    v3r = dump_reader(open(v3_orig, "rb").read())

    # ---- sprites
    new_index, sizes = add_sprites(img, v1)
    sprite_max = len(sizes) - 1
    remap = {s: new_index[s] for s in V1_EXTRA}

    # ---- code + V1 evolution switch
    a = new_code(remap, sprite_max)
    code = a.assemble()
    v1evo_at = a.labels["v1evo"]
    evo_words, ntab = relocate_v1_evo(v1, v1evo_at)
    code += evo_words
    stats_at = CODE_ORG + len(code)
    nrows = V1_LINE + V1_COUNT
    size_at = stats_at + nrows * NFIELDS
    end = size_at + 2 * len(sizes)
    if end > CODE_END:
        raise SystemExit("spazio codice insufficiente: fine %06x" % end)

    img.put_words(CODE_ORG, code, "codice nuovo + switch V1 (%d casi)" % ntab)

    # ---- stats table
    g1 = find_get_stat(_Reader(v1))
    g3 = find_get_stat(_Reader(bytes(img.b)))
    assert g3["func"] == 0x209217 and g3["table"] == 0x208EAC and g3["max_id"] == 19
    assert g1["max_id"] == V1_COUNT - 1 and g1["nfields"] == NFIELDS
    rows = []
    for i in range(V1_LINE):
        rows += [img.rd(g3["table"] + i * NFIELDS + f) for f in range(NFIELDS)]
    r1 = dump_reader(v1)
    for i in range(V1_COUNT):
        row = [r1(g1["table"] + i * NFIELDS + f) for f in range(NFIELDS)]
        assert V1_DIGI_SPR[0] <= row[0] < V1_DIGI_SPR[1], row
        row[0] = new_index[row[0]]
        rows += row
    img.put_words(stats_at, rows, "tabella stats (%d Digimon)" % nrows)

    # ---- size table
    st = []
    for w, h in sizes:
        st += [w, h]
    img.put_words(size_at, st, "tabella dimensioni (%d sprite)" % len(sizes))

    # ---- patches in the V3 code
    lo = lambda x: x & 0xFFFF  # noqa: E731
    lim = 0x4840 | (nrows - 1)                       # cmp r4, 37
    for w in (0x2026C3, 0x20921C, 0x203C41, 0x203C6E, 0x20349A, 0x208333):
        img.patch(w, 0x4853, lim, "limite id 19 -> %d" % (nrows - 1))
    img.patch(0x209229, [0x990C, 0x8EAC], [0x990C, lo(stats_at)], "get_stat: nuova tabella")
    assert img.rd(0x20922C) == 0x9860   # r4 = 0x20 (segmento invariato)
    img.patch(0x207657, [0x970B, 0x4B74], [0x970B, lo(size_at)], "draw_sprite: tabella dimensioni (w)")
    img.patch(0x20767C, [0x970B, 0x4B74], [0x970B, lo(size_at)], "draw_sprite: tabella dimensioni (h)")
    img.patch(0x20775E, 0x0848, 0x0840 | (OFFTAB >> 16), "draw_sprite: tabella offset 0x%x" % OFFTAB)
    rm = a.labels["remap"]
    img.patch(0x207605, [0x982A, 0x490C, 0x0273], [0xF040 | rm >> 16, lo(rm), 0xEE03],
              "draw_sprite: call remap ; jmp 0x20760b")
    img.patch(0x208841, [0x490C, 0x0273], [0x490C, sprite_max], "debug viewer: limite sprite")
    img.patch(0x20885A, [0x970B, 0x0273], [0x970B, sprite_max], "debug viewer: ultimo sprite")
    ev = a.labels["evo"]
    img.patch(0x202078, [0xF060, 0x2111], [0xF040 | ev >> 16, lo(ev)], "evoluzione: wrapper V1/V3")
    pi = a.labels["petinit"]
    img.patch(0x208E10, [0x9440, 0x9603], [0xF040 | pi >> 16, lo(pi)], "pet_init: id dell'uovo")
    img.patch(0x20042A, [0x990C, 0x0452], [0x990C, lo(a.labels["subcount"])], "schermata 3: numero sotto-stati")
    img.patch(0x20043D, [0x970B, 0x0450, 0x9860], [0x970B, lo(a.labels["subtab"]), 0x9860],
              "schermata 3: tabella sotto-stati")
    assert v3r(0x200429) == 0xFE20

    img.log.append("layout: codice %06x-%06x, stats %06x, dimensioni %06x-%06x"
                   % (CODE_ORG, stats_at, stats_at, size_at, end))
    img.log.append("etichette: " + ", ".join("%s=%06x" % (k, v) for k, v in sorted(a.labels.items())
                                             if not k.startswith("remap_") and not k.startswith(("egg_", "evo_", "petinit_"))))
    return bytes(img.b), img.log


class _Reader:
    """Adapter: find_get_stat() wants an object with .read(word_address)."""
    def __init__(self, raw):
        self.raw = raw

    def read(self, w):
        return struct.unpack_from("<H", self.raw, w2b(w))[0]


def main():
    args = [a for a in sys.argv[1:] if a != dumps.SKIP_FLAG]
    if len(args) != 3:
        raise SystemExit(__doc__)
    data, log = build(args[0], args[1], dumps.SKIP_FLAG not in sys.argv)
    open(args[2], "wb").write(data)
    print("\n".join(log))
    print("scritto %s (%d byte), sha1 %s" % (args[2], len(data), hashlib.sha1(data).hexdigest()))


if __name__ == "__main__":
    main()
