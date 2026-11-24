#!/usr/bin/env python3
"""Build "DMC Definitive" on the Ver.3 engine: English V3 + the Digimon lines of Ver.1, 2, 4, 5 + egg choice.

Roster (94 Digimon):  V3 0-19 | V1 20-37 | V2 38-55 | V4 56-74 | V5 75-93
Egg ids (Baby I):     0, 20, 38, 56, 75

Everything is built from the original dumps (plus the English text sprites in english/):

  sprites    each line's Digimon frames (15 per Digimon) and its version-specific sprites
             (eggs, tombstone, evolution backgrounds, a few V5 icons) get new indices from 628.
             Pixels are deduplicated. V1 frames and all version-specific sprites live below
             byte 0x400000 (directly readable by the CPU); V2/V4/V5 frames live at 0x400000+.
             The blitter (sub_207cd3) is hooked: when a sprite row lies above 4 MB it is read
             with the ROM flash routine 0x49CC in 16-word chunks and copied into the line buffer.
             New offset table at byte 0x3F0000, new size table at word 0x210800.
  roster     stats table (94 x 17 words) at word 0x210000. The six "id <= 19" checks are
             replaced by calls to tiny helpers that compare with 93 and hand the flags back to
             the original conditional jump (they overwrite the SR saved by the call).
  evolution  the call to the V3 switch goes through a wrapper that picks the line and runs a
             relocated copy of that version's switch (id - base, then + base).
  sprites    draw_sprite() remaps the version-specific sprites of the current Digimon's line.
  battles    every time an area is entered, a random version (one of five) is rolled into RAM
             0x0D15 and the whole series of that area comes from its table (8 x 5 opponent/power
             pairs), with the opponent ids moved to that version's line (bt_roll / bt_tab replace
             the V3 table address).
  training   screen 8 (training) picks init, handler count and handler table by the pet's line:
             V1/V2/V4/V5 lines play their own version's minigame, copied from that dump to word
             0x212000+ with calls remapped to the matching V3 functions (tools/training.py). The
             V1 minigame's five animations replace the empty V3 animation slots 23-27.
  link       the device announces the pet line's version and the id local to it; the partner id is
             moved into its line; jogress uses the rules and results of all five versions.
  egg        screen 3 gets a new first sub-state: A cycles the five eggs (V1, V2, V3, V4, V5; V1 first after a reset), B confirms, then the
             original hatching animation runs. pet_init() keeps the line of the previous pet.

Code lives at words 0x20F400-0x20FFFF (end of segment 0x20), tables in segment 0x21.

Usage:
  python3 build_dmc_definitive.py V1 V2 V3 V4 V5 OUT.bin [--skip-dump-check]
"""
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dumps  # noqa: E402
import xver  # noqa: E402
import english  # noqa: E402
from dmc_sprites import resource_offsets, find_size_table  # noqa: E402
from dmc_stats import find_get_stat, find_evo_switch  # noqa: E402
import training as TR  # noqa: E402
from training import TRAINING, callee_map, relocate_block  # noqa: E402
from unsp_asm import Asm, SP, R1, R2, R3, R4, BP, self_test  # noqa: E402
from unsp_dis import decode  # noqa: E402

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


class _Reader:
    """Adapter: find_get_stat() wants an object with .read(word_address)."""
    def __init__(self, raw):
        self.raw = raw

    def read(self, w):
        return struct.unpack_from("<H", self.raw, w2b(w))[0]


NFIELDS = 17
FRAMES = 15
V3_NSPR = 628
EXTRA = {   # version-specific sprites (same index in every version, different picture)
    "V1": [122, 123, 133, 134, 135, 160, 180, 181, 182],
    "V2": [133, 134, 135, 159, 160, 180, 181, 182],
    "V4": [133, 134, 135, 159, 160, 180, 181, 182],
    "V5": [133, 134, 135, 160, 180, 181, 182, 190, 191, 200, 201],
}
ORDER = ["V1", "V2", "V4", "V5"]          # lines added after V3, in id order
HIGH = {"V2", "V4", "V5"}                 # Digimon frames stored above 4 MB

LOW_START, LOW_END = 0x2E2600, 0x3F0000   # free bytes below 4 MB
OFFTAB = 0x3F0000                         # new offset table (byte), hi 0x3F lo 0
HIGH_START, HIGH_END = 0x400000, 0x7F0000 # free bytes above 4 MB (save area at 0x7FD000)
CODE_ORG, CODE_END = 0x20F400, 0x210000
STATS_AT = 0x210000                       # word address, segment 0x21
SIZES_AT = 0x210800
TRAIN_ORG, TRAIN_END = 0x212000, 0x220000 # relocated training minigames + tr_tab (segment 0x21)

PET_INIT, EVO_V3, HATCH_SUB = 0x208E0D, 0x202111, 0x200453
CLEAR_SPR, DRAW_BG, DRAW_SPR, FLUSH = 0x2072F5, 0x20267C, 0x2075FC, 0x202724
BEEP, RESET_TIMERS, SAVE, FLASH_READ = 0x200069, 0x20912A, 0x203B88, 0x0049CC
UNLOCK_BG = 0x200510                      # unlock background n (bitset 0x084C, n <= 12)
BG_UNLOCKED = 0x084C
LINE_BG = {"V3": 2, "V1": 0, "V2": 1, "V4": 3, "V5": 4}   # starting background of each version
ID, KEY, SUBST, SUBST_PREV, BG = 0x0826, 0x081B, 0x0C8A, 0x0C8B, 0x0824
LINEBUF = 0x07BA
TRANSPARENT = 0x07E0


# --------------------------------------------------------------------------- data gathering
class Version:
    def __init__(self, name, path):
        self.name = name
        self.raw = open(path, "rb").read()
        self.offs = resource_offsets(self.raw)
        self.st = find_size_table(self.raw, self.offs)
        self.rd = dump_reader(self.raw)
        self.gs = find_get_stat(_Reader(self.raw))
        self.n = self.gs["max_id"] + 1
        assert self.gs["nfields"] == NFIELDS

    def sprite(self, i):
        w, h = struct.unpack_from("<HH", self.raw, self.st + 4 * i)
        return w, h, self.raw[self.offs[i]: self.offs[i] + w * h * 2]

    def row(self, i):
        return [self.rd(self.gs["table"] + i * NFIELDS + f) for f in range(NFIELDS)]

    def evo_switch(self):
        ev = find_evo_switch(_Reader(self.raw))
        a = ev["at"]
        while self.rd(a) != 0xDA88:
            a -= 1
        return a, ev["table"], ev["table"] + 2 * ev["ncases"]


def relocate(ver, dst):
    """Copy a version's evolution switch (code + jump table) to word address dst."""
    start, table, end = ver.evo_switch()
    rd, delta = ver.rd, dst - start
    words = [rd(a) for a in range(start, end)]
    a = start
    fixed_tab = False
    while a < table:
        i = decode(rd, a)
        k = a - start
        if i.kind == "call":
            raise SystemExit("%s: call inattesa nello switch a %06x" % (ver.name, a))
        if i.kind == "jmp" and i.size == 2 and i.target is not None:
            if not start <= i.target < end:
                raise SystemExit("%s: salto fuori dallo switch a %06x" % (ver.name, a))
            t = i.target + delta
            words[k], words[k + 1] = (words[k] & 0xFFC0) | (t >> 16), t & 0xFFFF
        if words[k] == 0x970B and rd(a + 1) == (table & 0xFFFF) and rd(a + 2) == 0x9860:
            words[k + 1] = (table + delta) & 0xFFFF
            fixed_tab = True
        a += i.size
    assert fixed_tab, ver.name
    for a in range(table, end, 2):
        t = rd(a) | rd(a + 1) << 16
        if not start <= t < table:
            raise SystemExit("%s: voce jump table inattesa %06x" % (ver.name, t))
        t += delta
        words[a - start], words[a + 1 - start] = t & 0xFFFF, t >> 16
    assert (dst >> 16) == ((dst + len(words)) >> 16) == 0x20
    return words


class Pixels:
    """Deduplicating allocator for sprite pixels in the low or high flash area."""
    def __init__(self, img, existing):
        self.img = img
        self.seen = dict(existing)        # sha1 -> byte address
        self.pos = {False: LOW_START, True: HIGH_START}
        self.end = {False: LOW_END, True: HIGH_END}
        self.unique = 0

    def put(self, px, high):
        k = hashlib.sha1(px).digest()
        if k in self.seen:
            return self.seen[k]
        p = self.pos[high]
        if p + len(px) > self.end[high]:
            raise SystemExit("spazio pixel esaurito (%s)" % ("alto" if high else "basso"))
        if any(self.img.b[p:p + len(px)]):
            raise SystemExit("area pixel non libera a 0x%x" % p)
        self.img.b[p:p + len(px)] = px
        self.seen[k] = p
        self.pos[high] = (p + len(px) + 15) & ~15
        self.unique += 1
        return p


# --------------------------------------------------------------------------- new code
def new_code(lines, remaps, sprite_max, nrows, sw_lens):
    """lines: list of (name, base_id, count). remaps: name -> {old: new}."""
    a = Asm(CODE_ORG)
    last_id = nrows - 1
    added = [ln for ln in lines if ln[0] != "V3"]

    # ---- id-limit helpers: cmp with last_id, flags handed back through the saved SR
    for name, load in (("lim_bp5", ("bp", 5)), ("lim_bp7", ("bp", 7)), ("lim_ds", ("ds", 0))):
        a.label(name)
        if load[0] == "bp":
            a.ld(R4, bp=load[1])
        else:
            a.w(0x98E4)                    # r4 = [ds:r4]
        a.cmp(R4, last_id)
        a.ld(R3, rs=6)                     # r3 = sr (flags of the cmp)
        a.lea(R2, SP, 1)                   # r2 -> SR saved by the call
        a.sti(R3, R2)
        a.retf()

    # ---- draw_sprite() hook: version-specific sprites of the current line, then clamp
    a.label("remap")
    a.ld(R4, bp=0x2A)
    a.ld(R3, imm=ID)
    a.ldi(R3, R3)
    for name, base, cnt in added:
        a.cmp(R3, base)
        a.jf("jb", "remap_clamp")
        a.cmp(R3, base + cnt - 1)
        a.jf("jbe", "remap_" + name)
    a.jmpf("remap_clamp")
    for name, base, cnt in added:
        a.label("remap_" + name)
        for old, new in sorted(remaps[name].items()):
            a.cmp(R4, old)
            a.j("jne", "rn_%s_%d" % (name, old))
            a.ld(R4, imm=new)
            a.jmpf("remap_store")
            a.label("rn_%s_%d" % (name, old))
        a.jmpf("remap_clamp")
    a.label("remap_clamp")
    a.cmp(R4, sprite_max)
    a.j("jbe", "remap_store")
    a.ld(R4, imm=0)
    a.label("remap_store")
    a.st_bp(R4, 0x2A)
    a.retf()

    # ---- pet_init() hook: the new egg keeps the line of the previous pet (record 0x0826 only)
    a.label("petinit")
    a.ld(R3, bp=0x03)
    a.cmp(R3, ID)
    a.j("jne", "petinit_v3")
    a.ldi(R2, R3)
    for name, base, cnt in reversed(added):
        a.cmp(R2, base)
        a.j("jb", "pi_next_" + name)
        a.cmp(R2, base + cnt - 1)
        a.j("ja", "petinit_v3")
        a.ld(R2, imm=base)
        a.retf()
        a.label("pi_next_" + name)
    a.label("petinit_v3")
    a.ld(R2, imm=0)
    a.retf()

    # ---- evolution wrapper
    a.label("evo")
    a.ld(R4, imm=ID)
    a.ldi(R3, R4)
    for name, base, cnt in reversed(added):
        a.cmp(R3, base)
        a.jf("jae", "evo_" + name)
    a.jmpf(EVO_V3)
    for name, base, cnt in added:
        a.label("evo_" + name)
        a.alu("sub", R3, imm=base)
        a.sti(R3, R4)
        a.call("sw_" + name)
        a.ld(R4, imm=ID)
        a.ldi(R3, R4)
        a.alu("add", R3, imm=base)
        a.sti(R3, R4)
        a.retf()

    # ---- screen 3, new sub-state 0: choose the egg
    eggs = [base for _, base in sorted([("V3", 0)] + [(n, b) for n, b, _ in added])]   # V1, V2, V3, V4, V5
    a.label("egg")
    a.ld(R4, imm=SUBST)
    a.ldi(R3, R4)
    a.ld(R4, imm=SUBST_PREV)
    a.ldi(R4, R4)
    a.cmp(R3, rs=R4)
    a.j("je", "egg_run")
    a.ld(R4, imm=SUBST_PREV)               # first frame of the sub-state
    a.sti(R3, R4)
    a.call("first_egg")                    # first egg since reset: start from the V1 egg
    a.cmp(R4, 0)
    a.j("je", "egg_first")
    a.ld(R4, imm=ID)                       # otherwise keep the previous line's egg (id must be an egg)
    a.ldi(R3, R4)
    for e in eggs:
        a.cmp(R3, e)
        a.jf("je", "egg_draw")
    a.label("egg_first")
    a.ld(R4, imm=ID)
    a.ld(R3, imm=eggs[0])
    a.sti(R3, R4)
    a.jmpf("egg_draw")
    a.label("egg_run")
    a.ld(R4, imm=KEY)
    a.ldi(R3, R4)
    a.cmp(R3, 0x08)
    a.j("je", "egg_next")
    a.cmp(R3, 0x10)
    a.jf("je", "egg_ok")
    a.retf()
    a.label("egg_next")
    a.ld(R3, imm=1)
    a.push(R3)
    a.call(BEEP)
    a.sp_add(1)
    a.ld(R4, imm=ID)
    a.ldi(R3, R4)
    for k, e in enumerate(eggs):           # next egg in the cycle
        a.cmp(R3, e)
        a.j("jne", "egg_c%d" % k)
        a.ld(R3, imm=eggs[(k + 1) % len(eggs)])
        a.jmpf("egg_set")
        a.label("egg_c%d" % k)
    a.ld(R3, imm=eggs[0])
    a.label("egg_set")
    a.sti(R3, R4)
    a.jmpf("egg_draw")
    a.label("egg_ok")
    a.ld(R3, imm=1)
    a.push(R3)
    a.call(BEEP)
    a.sp_add(1)
    a.ld(R3, imm=0)                        # pet_init(&0x0826): id kept by the hook
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
    a.ld(R4, imm=ID)                       # background of the chosen line
    a.ldi(R3, R4)
    a.call("bg_of_egg")
    a.push(R3)                             # argument of UNLOCK_BG, kept on the stack
    a.call("first_egg")
    a.cmp(R4, 0)
    a.j("jne", "egg_unlock")
    a.lea(R4, SP, 1)                       # first egg since reset: move to that background
    a.ldi(R3, R4)
    a.ld(R4, imm=BG)
    a.sti(R3, R4)
    a.label("egg_unlock")
    a.call(UNLOCK_BG)
    a.sp_add(1)
    a.call(SAVE)
    a.ld(R3, imm=1)                        # next: original hatching sub-state
    a.ld(R4, imm=SUBST)
    a.sti(R3, R4)
    a.retf()
    a.label("egg_draw")
    a.call(CLEAR_SPR)
    a.ld(R4, imm=BG)                       # current background, or the egg's line on the first egg
    a.ldi(R3, R4)
    a.call("first_egg")
    a.cmp(R4, 0)
    a.j("jne", "egg_bg")
    a.ld(R4, imm=ID)
    a.ldi(R3, R4)
    a.call("bg_of_egg")
    a.j("jmp", "egg_bg2")
    a.label("egg_bg")
    a.ld(R4, imm=BG)
    a.ldi(R3, R4)
    a.label("egg_bg2")
    a.push(R3)
    a.call(DRAW_BG)
    a.sp_add(1)
    for v in (1, 0x18, 0x18, 133, 9):      # draw_sprite(slot 9, egg 133, x 24, y 24, 1)
        a.ld(R3, imm=v)
        a.push(R3)
    a.call(DRAW_SPR)
    a.sp_add(5)
    a.call(FLUSH)
    a.retf()

    # first_egg: r4 = unlocked line backgrounds (bits 0-4 of 0x084C); 0 = first egg since reset
    a.label("first_egg")
    a.ld(R4, imm=BG_UNLOCKED)
    a.ldi(R4, R4)
    a.alu("and", R4, imm=0x1F)
    a.retf()
    # bg_of_egg: r3 = egg id -> r3 = starting background of that line
    a.label("bg_of_egg")
    for name, base, _ in lines:
        a.cmp(R3, base)
        a.j("jne", "bge_" + name)
        a.ld(R3, imm=LINE_BG[name])
        a.retf()
        a.label("bge_" + name)
    a.ld(R3, imm=LINE_BG["V3"])
    a.retf()

    # ---- blitter hooks (replace "r4 = 0 ; [bp+6] = r4" before each row-copy loop).
    # Blitter frame: [bp+2] pixel count, [bp+4] position in the line buffer,
    # [bp+7]:[bp+8] word address of the first pixel to copy (forward) or of the last
    # one (mirrored, copied backwards), [bp+6] loop counter.
    for name, fwd in (("blit_fwd", 1), ("blit_rev", 0)):
        a.label(name)
        a.ld(R4, imm=0)
        a.st_bp(R4, 6)
        a.ld(R4, bp=8)
        a.cmp(R4, 0x40)
        a.j("jae", name + "_hi")
        a.retf()
        a.label(name + "_hi")
        a.ld(R1, imm=fwd)
        a.jmpf("blit_hi")
    a.label("blit_hi")
    a.ld(R2, bp=2)                         # n
    a.st_bp(R2, 6)                         # the original loop will not run
    a.ld(R3, bp=4)
    a.lea(R3, R3, LINEBUF)                 # destination of the first word read
    a.cmp(R1, 0)
    a.j("jne", "bh_d")
    a.alu("add", R3, rs=R2)                # mirrored: lowest address goes to pos + n - 1
    a.alu("sub", R3, imm=1)
    a.label("bh_d")
    a.push(R3)                             # [bp+21] dst
    a.push(R1)                             # [bp+20] direction
    a.push(R2)                             # [bp+19] remaining
    a.ld(R3, bp=7)
    a.ld(R4, bp=8)
    a.cmp(R1, 0)
    a.j("jne", "bh_a")
    a.alu("sub", R3, rs=R2)                # mirrored: start = ptr - (n - 1)
    a.alu("sbc", R4, imm=0)
    a.alu("add", R3, imm=1)
    a.alu("adc", R4, imm=0)
    a.label("bh_a")
    a.alu("sub", R4, imm=0x20)             # word address -> byte address
    a.shift(R4, R4, "lsl", 1)
    a.shift(R2, R3, "lsr", 4)
    a.shift(R2, R2, "lsr", 4)
    a.shift(R2, R2, "lsr", 4)
    a.shift(R2, R2, "lsr", 3)
    a.alu("or", R4, rs=R2)
    a.shift(R3, R3, "lsl", 1)
    a.push(R4)                             # [bp+18] byte address hi
    a.push(R3)                             # [bp+17] byte address lo
    a.push(BP)                             # [bp+16] blitter bp
    a.alu("sub", SP, imm=16)               # [bp+0..15] buffer
    a.lea(BP, SP, 1)
    a.label("bh_loop")
    a.ld(R2, bp=19)
    a.cmp(R2, 0)
    a.jf("je", "bh_done")
    a.cmp(R2, 16)
    a.j("jbe", "bh_k1")
    a.ld(R2, imm=16)
    a.label("bh_k1")
    a.ld(R4, bp=18)                        # flash_read(buf, k, addr_lo, addr_hi)
    a.push(R4)
    a.ld(R4, bp=17)
    a.push(R4)
    a.push(R2)
    a.ld(R4, rs=BP)
    a.push(R4)
    a.call(FLASH_READ)
    a.sp_add(4)
    a.ld(R2, bp=19)                        # k again (registers are not preserved)
    a.cmp(R2, 16)
    a.j("jbe", "bh_k2")
    a.ld(R2, imm=16)
    a.label("bh_k2")
    a.ld(R3, bp=19)
    a.alu("sub", R3, rs=R2)
    a.st_bp(R3, 19)
    a.shift(R3, R2, "lsl", 1)              # address += 2k bytes
    a.ld(R4, bp=17)
    a.alu("add", R4, rs=R3)
    a.st_bp(R4, 17)
    a.ld(R4, bp=18)
    a.alu("adc", R4, imm=0)
    a.st_bp(R4, 18)
    a.ld(R1, rs=BP)
    a.label("bh_copy")
    a.ldi(R3, R1)
    a.alu("add", R1, imm=1)
    a.cmp(R3, TRANSPARENT)
    a.j("je", "bh_skip")
    a.ld(R4, bp=21)
    a.sti(R3, R4)
    a.label("bh_skip")
    a.ld(R4, bp=21)
    a.ld(R3, bp=20)
    a.cmp(R3, 0)
    a.j("je", "bh_dec")
    a.alu("add", R4, imm=1)
    a.j("jmp", "bh_st")
    a.label("bh_dec")
    a.alu("sub", R4, imm=1)
    a.label("bh_st")
    a.st_bp(R4, 21)
    a.alu("sub", R2, imm=1)
    a.j("jne", "bh_copy")
    a.jmpf("bh_loop")
    a.label("bh_done")
    a.sp_add(16)
    a.pop_bp()
    a.sp_add(5)
    a.retf()

    # ---- screen 3 sub-state table: [egg, original hatching], count
    a.label("subtab")
    a.addr("egg")
    a.w(CODE_ORG >> 16)
    a.w(HATCH_SUB & 0xFFFF, HATCH_SUB >> 16)
    a.label("subcount")
    a.w(2)

    # ---- room for the relocated evolution switches (filled in by build())
    for name, _, _ in added:
        a.label("sw_" + name)
        a.w(*[0] * sw_lens[name])
    return a


# --------------------------------------------------------------------------- build
def build(paths, check=True):
    assert self_test()
    if check:
        for k, p in paths.items():
            dumps.check(k, p)
    xver.P.update({k: os.path.abspath(p) for k, p in paths.items()})    # cross-version matching reads these
    xver._cache.clear()
    img = Image(open(paths["V3"], "rb").read())
    img.log += english.apply(img.b)
    vers = {k: Version(k, p) for k, p in paths.items()}
    v3 = _v3_from_image(img)
    assert len(v3.offs) == V3_NSPR and v3.n == 20

    # ---- line layout
    lines, base = [("V3", 0, 20)], 20
    for name in ORDER:
        lines.append((name, base, vers[name].n))
        base += vers[name].n
    nrows = base
    assert nrows - 1 <= 0xFFFF

    # ---- sprites
    existing = {}
    for i, o in enumerate(v3.offs):
        existing.setdefault(hashlib.sha1(v3.sprite(i)[2]).digest(), o)
    pix = Pixels(img, existing)
    rel = [o - 0x80000 for o in v3.offs]
    sizes = [v3.sprite(i)[:2] for i in range(V3_NSPR)]
    new_base, remaps = {}, {}

    def add(ver, idx, high):
        w, h, px = ver.sprite(idx)
        rel.append(pix.put(px, high) - 0x80000)
        sizes.append((w, h))
        return len(rel) - 1

    for name in ORDER:
        ver = vers[name]
        for d in range(ver.n):
            b = ver.row(d)[0]
            if b not in new_base.setdefault(name, {}):
                new_base[name][b] = len(rel)
                for f in range(FRAMES):
                    add(ver, b + f, name in HIGH)
        remaps[name] = {s: add(ver, s, False) for s in EXTRA[name]}
    v3_by_pixels = {}
    for i in range(V3_NSPR):
        v3_by_pixels.setdefault(v3.sprite(i), i)
    train_sprites = {}
    for name, spec in TRAINING.items():       # training sprites: reuse an identical V3 one or add it
        for key, sp in ((name, spec), (name + "_anim", spec.get("anims"))):
            if sp:
                train_sprites[key] = {s: v3_by_pixels.get(vers[name].sprite(s)) for s in sp["sprites"]}
                for s, i in train_sprites[key].items():
                    if i is None:
                        train_sprites[key][s] = add(vers[name], s, False)
    n_spr = len(rel)
    if any(img.b[OFFTAB:OFFTAB + 4 * n_spr]):
        raise SystemExit("area tabella offset non libera")
    for i, r in enumerate(rel):
        struct.pack_into("<I", img.b, OFFTAB + 4 * i, r)
    img.log.append("sprite: %d totali (%d nuovi, %d pixel unici); basso fino a 0x%06x, alto 0x%06x-0x%06x"
                   % (n_spr, n_spr - V3_NSPR, pix.unique, pix.pos[False], HIGH_START, pix.pos[True]))
    assert OFFTAB + 4 * n_spr <= 0x400000

    # ---- code + relocated switches
    sw_lens = {}
    for name in ORDER:
        start, table, end = vers[name].evo_switch()
        sw_lens[name] = end - start
    a = new_code(lines, remaps, n_spr - 1, nrows, sw_lens)
    code = a.assemble()
    for name in ORDER:
        at = a.labels["sw_" + name]
        sw = relocate(vers[name], at)
        assert len(sw) == sw_lens[name]
        code[at - CODE_ORG: at - CODE_ORG + len(sw)] = sw
    if CODE_ORG + len(code) > CODE_END:
        raise SystemExit("spazio codice insufficiente: %06x" % (CODE_ORG + len(code)))
    img.put_words(CODE_ORG, code, "codice nuovo + 4 switch rilocati")

    # ---- stats and size tables (segment 0x21)
    rows = []
    for name, base, cnt in lines:
        ver = v3 if name == "V3" else vers[name]
        for d in range(cnt):
            r = ver.row(d)
            if name != "V3":
                r[0] = new_base[name][r[0]]
            rows += r
    img.put_words(STATS_AT, rows, "tabella stats (%d Digimon)" % nrows)
    st = []
    for w, h in sizes:
        st += [w, h]
    if SIZES_AT < STATS_AT + len(rows) or SIZES_AT + len(st) > 0x220000:
        raise SystemExit("tabelle sovrapposte")
    img.put_words(SIZES_AT, st, "tabella dimensioni (%d sprite)" % n_spr)

    # ---- training minigames (segment 0x21, after the size table)
    if SIZES_AT + len(st) > TRAIN_ORG:
        raise SystemExit("tabella dimensioni oltre TRAIN_ORG")
    tw, pos, tr_lines, tables, speeds, anim_ptrs = [], TRAIN_ORG, [], {}, {}, {}
    base_of = {n: (b, c) for n, b, c in lines}

    def place(name, spec, sprite_map, what):
        nonlocal pos
        ver = vers[name]
        words, fixed = relocate_block(ver.rd, spec, pos, callee_map(name, ver.rd, spec), sprite_map)
        img.log.append("allenamento %s %s: %d word a %06x, %s, sprite %s" % (
            name, what, len(words), pos, fixed, {hex(k): v for k, v in sprite_map.items()}))
        tw.extend(words)
        at, pos = pos, pos + len(words)
        return at
    for name in ORDER:
        spec = TRAINING[name]
        at = place(name, spec, train_sprites[name], "minigioco")
        tr_lines.append((name,) + base_of[name])
        tables[name] = at + spec["table"] - spec["start"]
        if "speed" in spec:
            speeds[name] = at + spec["speed"] - spec["start"]
            assert (speeds[name] & 0xFFFF) + 8 <= 0xFFFF
        if "anims" in spec:
            an = dict(spec["anims"], table=None)
            at = place(name, an, train_sprites[name + "_anim"], "animazioni")
            for k, f in an["ids"].items():
                anim_ptrs[k] = at + f - an["start"]
    bt_tables = {}
    for name in ORDER:                        # battle series of each line (ids moved to the line's range)
        b, c = base_of[name]
        bt_tables[name] = pos
        words = TR.battle_table(vers[name].rd, name, b, c)
        tw.extend(words)
        pos += len(words)
    ta = Asm(pos)
    TR.helpers(ta, tr_lines, tables, speeds, TR.RANDOM)
    TR.battle_helper(ta, tr_lines, bt_tables, TR.RANDOM)
    TR.link_helpers(ta, lines, {n: v.rd for n, v in vers.items()})
    tw += ta.assemble()
    if TRAIN_ORG + len(tw) > TRAIN_END or any(img.rd(TRAIN_ORG + k) for k in range(len(tw))):
        raise SystemExit("area allenamenti non libera")
    img.put_words(TRAIN_ORG, tw, "minigiochi allenamento + tr_tab")

    # ---- patches in the V3 code
    lo = lambda x: x & 0xFFFF  # noqa: E731
    far = lambda op, t: [op | (t >> 16), lo(t)]  # noqa: E731
    L = a.labels
    img.patch(0x2026C2, [0x9805, 0x4853], far(0xF040, L["lim_bp5"]), "disegno Digimon: limite id")
    img.patch(0x20921B, [0x9807, 0x4853], far(0xF040, L["lim_bp7"]), "get_stat: limite id")
    for w, why in ((0x203C40, "verifica salvataggio (pet)"), (0x203C6D, "verifica salvataggio (precedente)"),
                   (0x203499, "record pet precedente"), (0x208332, "selettore debug")):
        img.patch(w, [0x98E4, 0x4853], far(0xF040, L["lim_ds"]), why + ": limite id")
    img.patch(0x209229, [0x990C, 0x8EAC], [0x990C, lo(STATS_AT)], "get_stat: tabella (lo)")
    img.patch(0x20922C, 0x9860, 0x9840 | (STATS_AT >> 16), "get_stat: tabella (segmento)")
    for w in (0x207657, 0x20767C):
        img.patch(w, [0x970B, 0x4B74, 0x9860], [0x970B, lo(SIZES_AT), 0x9840 | (SIZES_AT >> 16)],
                  "draw_sprite: tabella dimensioni")
    img.patch(0x20775E, 0x0848, 0x0840 | (OFFTAB >> 16), "draw_sprite: tabella offset")
    img.patch(0x207605, [0x982A, 0x490C, 0x0273], far(0xF040, L["remap"]) + [0xEE03],
              "draw_sprite: call remap ; jmp 0x20760b")
    img.patch(0x208841, [0x490C, 0x0273], [0x490C, n_spr - 1], "debug viewer: limite sprite")
    img.patch(0x20885A, [0x970B, 0x0273], [0x970B, n_spr - 1], "debug viewer: ultimo sprite")
    img.patch(0x202078, [0xF060, 0x2111], far(0xF040, L["evo"]), "evoluzione: wrapper per linea")
    img.patch(0x208E10, [0x9440, 0x9603], far(0xF040, L["petinit"]), "pet_init: uovo della linea")
    img.patch(0x20042A, [0x990C, 0x0452], [0x990C, lo(L["subcount"])], "schermata 3: numero sotto-stati")
    img.patch(0x20043D, [0x970B, 0x0450, 0x9860], [0x970B, lo(L["subtab"]), 0x9860],
              "schermata 3: tabella sotto-stati")
    img.patch(0x203A3D, [0xF060, 0x0510], far(0xF040, L["first_egg"]),
              "reset: non sbloccare lo sfondo V3 (lo fa la scelta dell'uovo)")
    img.patch(0x207F78, [0x9840, 0xD806], far(0xF040, L["blit_fwd"]), "blitter: righe oltre 4 MB")
    img.patch(0x207FC3, [0x9840, 0xD806], far(0xF040, L["blit_rev"]), "blitter: righe specchiate oltre 4 MB")
    init_old = [0xFE00, 0x990C, 0x085C, 0x98E4, 0x9640, 0x9309, TR.V3_SPEED & 0xFFFF, 0x9460, 0x0901,
                0x1702, 0xF02B, 0x96E4, 0xFE00, 0x990C, 0x085D, 0xD6E4]
    assert len(init_old) == TR.V3_INIT_END - TR.V3_INIT_AT
    assert [img.rd(TR.V3_INIT_AT + k) for k in range(len(init_old))] == init_old, "init V3 inattesa"
    skip = TR.V3_INIT_END - (TR.V3_INIT_AT + 3)
    img.patch(TR.V3_INIT_AT, init_old[:3], far(0xF040, ta.labels["tr_init"]) + [0xEE00 | skip],
              "allenamento: init per linea ; jmp 0x%06x" % TR.V3_INIT_END)
    img.patch(TR.V3_CNT_AT, [0xFE20, 0x990C, 0x5632, 0x96E4], far(0xF040, ta.labels["tr_cnt"]) + [0xFE00, 0xFE00],
              "allenamento: numero sotto-stati per linea")
    img.patch(TR.V3_TAB_AT, [0x970B, TR.V3_TAB & 0xFFFF, 0x9840 | TR.V3_TAB >> 16],
              far(0xF040, ta.labels["tr_tab"]) + [0xFE00], "allenamento: tabella sotto-stati per linea")
    for w in TR.V3_BATTLE_AT:
        n = img.rd(w + 2)
        assert n & 0xFFC0 == 0xD800, hex(n)
        img.patch(w, [0x990C, TR.BATTLE["V3"] & 0xFFFF, n, 0x9860],
                  far(0xF040, ta.labels["bt_roll" if w == TR.V3_BATTLE_AT[0] else "bt_tab"]) + [n, 0x9903],
                  "battaglia: serie di avversari da una versione casuale")
    tl = ta.labels
    img.patch(TR.LK_START, [0x9642, 0xFE00, 0x990C, 0x0CF5, 0xD6E4], far(0xF040, tl["lk_ver"]) + [0xFE00] * 3,
              "collegamento: versione = linea del Digimon")
    img.patch(TR.LK_SEND, [0xFE00, 0x990C, 0x0826, 0x96E4], far(0xF040, tl["lk_local"]) + [0xFE00] * 2,
              "collegamento: id inviato locale alla versione")
    img.patch(TR.LK_RECV, [0xFE00, 0x990C, 0x0CBC, 0x96E4, 0xFE00, 0x990C, 0x0CFA, 0xD6E4],
              far(0xF040, tl["lk_rx"]) + [0xFE00] * 6, "collegamento: id del partner nella sua linea")
    jc, jc_end = TR.JG_CHECK
    img.patch(jc, [0x9840, 0xD800, 0xFE00, 0x990C, 0x0CF5, 0x98E4],
              far(0xF040, tl["jg_check"]) + [0xD200, 0xD401] + far(0xFE80, jc_end),
              "jogress: regole di tutte le versioni ; jmpf 0x%06x" % jc_end)
    jr, jr_end = TR.JG_RESULT
    img.patch(jr, [0xFE00, 0x990C, 0x0827, 0x98E4],
              far(0xF040, tl["jg_result"]) + [0xEE00 | (jr_end - (jr + 3)), 0xFE00],
              "jogress: risultato della linea ; jmp 0x%06x" % jr_end)
    for k, t in sorted(anim_ptrs.items()):
        w = TR.V3_ANIM_TAB + 2 * k
        old = [img.rd(w), img.rd(w + 1)]
        assert img.rd(old[1] << 16 | old[0]) == 0xDA88 and img.rd((old[1] << 16 | old[0]) + 3) == 0x9241, k
        img.patch(w, old, [t & 0xFFFF, t >> 16], "animazione %d: V1 (allenamento)" % k)
    assert img.rd(0x200429) == 0xFE20

    img.log.append("linee: " + ", ".join("%s %d-%d" % (n, b, b + c - 1) for n, b, c in lines))
    img.log.append("codice %06x-%06x; stats %06x; dimensioni %06x-%06x"
                   % (CODE_ORG, CODE_ORG + len(code), STATS_AT, SIZES_AT, SIZES_AT + len(st)))
    img.log.append("etichette: " + ", ".join("%s=%06x" % (k, v) for k, v in sorted(L.items())
                                             if k in ("egg", "evo", "petinit", "remap", "blit_fwd", "blit_rev",
                                                      "blit_hi", "lim_bp5", "lim_bp7", "lim_ds", "subtab")
                                             or k.startswith("sw_")))
    return bytes(img.b), img.log


def _v3_from_image(img):
    v = Version.__new__(Version)
    v.name = "V3"
    v.raw = bytes(img.b)
    v.offs = resource_offsets(v.raw)
    v.st = find_size_table(v.raw, v.offs)
    v.rd = dump_reader(v.raw)
    v.gs = find_get_stat(_Reader(v.raw))
    v.n = v.gs["max_id"] + 1
    return v


def main():
    args = [a for a in sys.argv[1:] if a != dumps.SKIP_FLAG]
    if len(args) != 6:
        raise SystemExit(__doc__)
    v1, v2, v3, v4, v5, out = args
    data, log = build({"V1": v1, "V2": v2, "V3": v3, "V4": v4, "V5": v5}, dumps.SKIP_FLAG not in sys.argv)
    open(out, "wb").write(data)
    print("\n".join(log))
    print("scritto %s (%d byte), sha1 %s" % (out, len(data), hashlib.sha1(data).hexdigest()))


if __name__ == "__main__":
    main()
