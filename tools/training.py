"""Per-line training minigames: copy each version's screen-8 sub-states into the V3 image.

V3 screen 8 (training, dispatcher 0x20558F) runs an init when the screen is entered, then calls one
of the handlers in its sub-state table (minigame, success, failure, result...). Every version has
the same dispatcher skeleton and the same prize functions; what differs is
  * the init (V3: 0x085D = table[stage]; V2/V5: nothing; V4: 0x085D = rom_5daa(table[stage], 11);
    V1: round counter 0x0D02, 0x085C = 0x085D = 0),
  * the number of handlers (V1 has 6, the others 4) and the handlers themselves,
  * for V1 only, five animations (ids 23-27) that are empty stubs in V3.
We copy each version's handler block verbatim to free flash and fix:
  * far calls -> the V3 function with the same instruction skeleton (or a fixed override);
    ROM calls (< 0x10000) are kept, the mask ROM is the same chip in every version
  * jmpf / internal calls / the handler table   -> relocated
  * flash data refs (r1 = lo ; r2 = seg)          -> relocated
  * sprite ids passed to draw_sprite (`r3 = id ; r4 = sp + 2`) -> identical V3 sprite or a new one
Three hooks in the V3 dispatcher pick the line's init, handler count and handler table by pet id.
"""
from unsp_asm import Asm, R1, R2, R3, R4  # noqa: F401
from unsp_dis import decode

ID = 0x0826
V3_INIT_AT, V3_INIT_END = 0x2055E7, 0x2055F7   # `0x085D = table[0x085C]` (16 words)
V3_CNT_AT = 0x2055F7                            # `ds = 0x20 ; r4 = 0x5632 ; r3 = [ds:r4]`
V3_TAB_AT, V3_TAB = 0x20560B, 0x20561E          # `r3 = 0x561E ; r4 = 0x20`
V3_SPEED, V3_COUNT = 0x20562C, 4
V3_ANIM_TAB = 0x20B62D                          # 50 (lo, hi) pointers, used by 0x20B78E / 0x20B7C9
ROM_MOD, RANDOM = 0x005DAA, 0x200344            # V4 init / V1 init helpers (V1 0x200344 == V3 0x200344)

# Per version: handler block [start, end) = table + count + data + code. `calls` fixes the callees
# whose skeleton matches several V3 functions: 0x200084 is the same everywhere, the "strength" and
# "training count" prize functions follow from the constant offset between that version and V3
# (checked against the V3 handlers, which call 0x2093DB and 0x209527 at the same places).
TRAINING = {
    "V1": dict(start=0x205002, end=0x205484, table=0x205002, count=6, init="V1",
               sprites=(0x238, 0x239),
               calls={0x200084: 0x200084, 0x2090E2: 0x2093DB, 0x20922E: 0x209527},
               anims=dict(start=0x20D2D8, end=0x20D770, ids={23: 0x20D2D8, 24: 0x20D424, 25: 0x20D539,
                                                            26: 0x20D5CA, 27: 0x20D6DF},
                          sprites=(0x237,), calls={0x2076F8: 0x2079CF})),
    "V2": dict(start=0x2039E1, end=0x203D76, table=0x2039E1, count=4, init=None,
               sprites=(0x237, 0x1F8, 0x1F9, 0x1FA, 0x1FB),
               calls={0x200084: 0x200084, 0x208FD5: 0x2093DB, 0x209121: 0x209527}),
    "V4": dict(start=0x204BE3, end=0x205024, table=0x204BE3, count=4, init="V4", speed=0x204BEB, count_at=0x204BF1,
               sprites=(0x207, 0x252),
               calls={0x200084: 0x200084, 0x2092DA: 0x2093DB, 0x209426: 0x209527}),
    "V5": dict(start=0x205096, end=0x205503, table=0x205096, count=4, init=None,
               sprites=(0x208, 0x209, 0x20A, 0x20B, 0x247),
               calls={0x200084: 0x200084, 0x2092EF: 0x2093DB, 0x20943B: 0x209527}),
}


def handlers(rd, spec):
    t = spec["table"]
    return [rd(t + 2 * k) | rd(t + 2 * k + 1) << 16 for k in range(spec["count"])]


def code_start(rd, spec):
    return min(handlers(rd, spec)) if spec.get("table") else spec["start"]


def callee_map(ver_name, rd, spec):
    """Call target in that version -> V3 target, by unique skeleton match unless overridden."""
    from xver import match
    start, end, overrides = spec["start"], spec["end"], spec["calls"]
    out, a = {}, code_start(rd, spec)
    while a < end:
        i = decode(rd, a)
        t = i.target
        if i.kind == "call" and not start <= t < end and t not in out:
            if t < 0x10000:
                out[t] = t                                      # mask ROM routine
            elif t in overrides:
                out[t] = overrides[t]
            else:
                m = match(t, "V3", ver_name)
                if len(m) != 1:
                    raise SystemExit("%s: call %06x senza corrispondente V3 unico %s"
                                     % (ver_name, t, [hex(x) for x in m]))
                out[t] = m[0]
        a += 1 if i.text.startswith(".dw") else i.size
    return out


def relocate_block(rd, spec, dst, calls, sprite_map):
    """Return the relocated words of a block placed at word address dst."""
    start, end, table = spec["start"], spec["end"], spec.get("table")
    delta = dst - start
    words = [rd(a) for a in range(start, end)]
    assert dst >> 16 == (dst + len(words) - 1) >> 16, "il blocco attraversa un segmento"
    seg = dst >> 16

    def inside(t):
        return start <= t < end

    if table:                                     # handler table (lo, hi) pairs + count
        n = spec["count"]
        assert rd(spec.get("count_at", table + 2 * n)) == n
        for k, t in enumerate(handlers(rd, spec)):
            assert inside(t), hex(t)
            t += delta
            words[table - start + 2 * k], words[table - start + 2 * k + 1] = t & 0xFFFF, t >> 16

    a = code_start(rd, spec)                      # skip count + data until the first handler
    fixed = {"call": 0, "jmpf": 0, "data": 0, "sprite": 0}
    sprites_seen = []
    while a < end:
        i = decode(rd, a)
        k = a - start
        if i.text.startswith(".dw"):              # padding / data between handlers
            a += 1
            continue
        w0 = rd(a)
        if i.kind == "call":
            t = i.target + delta if inside(i.target) else calls[i.target]
            words[k], words[k + 1] = 0xF040 | t >> 16, t & 0xFFFF
            fixed["call"] += 1
        elif i.size == 2 and (w0 & 0xFFC0) == 0xFE80:      # jmpf
            if not inside(i.target):
                raise SystemExit("jmpf fuori dal blocco a %06x" % a)
            t = i.target + delta
            words[k], words[k + 1] = 0xFE80 | t >> 16, t & 0xFFFF
            fixed["jmpf"] += 1
        elif i.size == 2:
            imm = rd(a + 1)
            if w0 == 0x9309 and inside(imm | (start & 0xFF0000)):   # r1 = lo(data) ; r2 = seg
                assert rd(a + 2) == 0x9440 | (start >> 16), "%06x" % a
                words[k + 1] = (imm + delta) & 0xFFFF
                words[k + 2] = 0x9440 | seg
                fixed["data"] += 1
            elif (w0 == 0x990C and inside(imm | (start & 0xFF0000)) and rd(a + 2) & 0xFFC0 == 0xD800
                  and rd(a + 3) == 0x9840 | (start >> 16)):       # r4 = lo ; [bp+n] = r4 ; r4 = seg
                words[k + 1] = (imm + delta) & 0xFFFF
                words[k + 3] = 0x9840 | seg
                fixed["data"] += 1
            elif w0 == 0x970B and rd(a + 2) == 0x0908 and rd(a + 3) == 2:   # r3 = id ; r4 = sp + 2
                if imm in sprite_map:
                    words[k + 1] = sprite_map[imm]
                    sprites_seen.append(imm)
                    fixed["sprite"] += 1
                elif imm >= 0x100:
                    raise SystemExit("sprite %#x non mappato a %06x" % (imm, a))
            elif imm >= 0x1000 and (w0 & 0xF1F8) == 0x9108:      # rd = imm16 above RAM
                raise SystemExit("costante non gestita %04x a %06x" % (imm, a))
        a += i.size
    assert set(sprites_seen) == set(sprite_map), (sprites_seen, sprite_map)
    return words, fixed


# --------------------------------------------------------------------------- dispatcher hooks
def _by_line(a, name, lines, body, default):
    """r4 = pet id; for each (line, base, cnt): if base <= id < base+cnt -> body(line) ; else default."""
    a.label(name)
    a.ld(R4, imm=ID)
    a.ldi(R4, R4)
    for k, (line, base, cnt) in enumerate(lines):
        a.ld(R3, rs=R4)
        a.alu("sub", R3, imm=base)
        a.cmp(R3, cnt)
        a.j("jae", "%s_n%d" % (name, k))
        body(line)
        a.label("%s_n%d" % (name, k))
    default()


def helpers(a, lines, tables, speeds, rnd):
    """Assemble tr_init, tr_cnt, tr_tab for lines = [(name, base, cnt)] with a ported minigame.

    tables[name] = relocated handler table; speeds[name] = relocated V4 data table.
    Each helper is called from the V3 dispatcher and returns with retf (SR, hence ds, restored).
    """
    def ret_const(r3_val, r4_val=None):
        a.ld(R3, imm=r3_val)
        if r4_val is not None:
            a.ld(R4, imm=r4_val)
        a.retf()

    def flash_read(table):                        # r3 = word at table + [0x085C]
        a.ld(R4, imm=0x085C)
        a.ldi(R4, R4)
        a.alu("add", R4, imm=table & 0xFFFF)
        a.w(0xFE00 | table >> 16)                 # ds = seg
        a.w(0x96E4)                               # r3 = [ds:r4]
        a.w(0xFE00)                               # ds = 0

    def store(reg, addr):
        a.ld(R1 if reg != R1 else R3, imm=addr)
        a.sti(reg, R1 if reg != R1 else R3)

    # ---- tr_init: replaces `0x085D = V3_SPEED[0x085C]`
    def init_body(line):
        kind = INIT_OF[line]
        if kind == "V4":
            flash_read(speeds[line])
            a.ld(R4, imm=0x0B)
            a.w(0xD890)                           # push r3,r4 (same word as the V4 original)
            a.call(ROM_MOD)
            a.sp_add(2)
            store(R1, 0x085D)
        elif kind == "V1":
            a.ld(R4, imm=0x0D02)
            a.ldi(R4, R4)
            a.cmp(R4, 9)
            a.j("ja", "tri_rnd")
            a.add(R4, 1)
            a.cmp(R4, 9)
            a.j("jbe", "tri_st")
            a.ld(R4, imm=0)
            a.j("jmp", "tri_st")
            a.label("tri_rnd")
            a.ld(R3, imm=10)
            a.push(R3)
            a.call(rnd)
            a.sp_add(1)
            a.ld(R4, rs=R1)
            a.label("tri_st")
            store(R4, 0x0D02)
            a.ld(R4, imm=0)
            store(R4, 0x085C)
            store(R4, 0x085D)
        a.retf()

    def init_default():
        flash_read(V3_SPEED)
        store(R3, 0x085D)
        a.retf()
    _by_line(a, "tr_init", lines, init_body, init_default)

    _by_line(a, "tr_cnt", lines, lambda line: ret_const(COUNT_OF[line]), lambda: ret_const(V3_COUNT))
    _by_line(a, "tr_tab", lines, lambda line: ret_const(tables[line] & 0xFFFF, tables[line] >> 16),
             lambda: ret_const(V3_TAB & 0xFFFF, V3_TAB >> 16))


INIT_OF = {k: v["init"] for k, v in TRAINING.items()}
COUNT_OF = {k: v["count"] for k, v in TRAINING.items()}


# --------------------------------------------------------------------------- battle series
# Solo battle (screen 10): 8 areas x 5 (opponent id, power) pairs, 0xFFFF ends an area. The ids are
# local to each version. Each version's table is copied with the ids moved to its line's range; the
# three places that load the V3 table address call bt_roll / bt_tab, which pick a random version per area.
BATTLE = {"V1": 0x20612F, "V2": 0x206022, "V3": 0x206B27, "V4": 0x206A37, "V5": 0x206A4C}
BATTLE_WORDS = 80
V3_BATTLE_AT = (0x206CF3, 0x207225, 0x20724B)     # `r4 = 0x6b27 ; [bp+n] = r4 ; r4 = 0x20`


def battle_table(rd, name, base, count):
    t = BATTLE[name]
    words = [rd(t + k) for k in range(BATTLE_WORDS)]
    for k in range(0, BATTLE_WORDS, 2):
        if words[k] != 0xFFFF:
            assert words[k] < count, (name, k, words[k])
            words[k] += base
    return words


BT_VERSION = 0x0D15     # free RAM word: version rolled for the current area series (0..4, see VERSION_NO)
BT_ROLL_COUNT = 0x0861  # opponents counted so far by the area-confirm loop (0 on its first pass)


def battle_helper(a, lines, tables, rnd):
    """Random battle series: each time an area is entered, its opponents come from a random version.

    bt_roll (area-confirm loop, 0x206CF3): on the loop's first pass rolls random(5) into BT_VERSION.
    bt_tab (all three sites): r4 = lo, r3 = segment of the rolled version's table, whose ids are
    already moved to that version's line; area k stays area k, so the stages stay coherent.
    An invalid BT_VERSION falls back to the pet line's own table (V3 by default).
    """
    def ret(t):
        a.ld(R4, imm=t & 0xFFFF)
        a.ld(R3, imm=t >> 16)
        a.retf()
    every = dict(tables, V3=BATTLE["V3"])
    a.label("bt_roll")
    a.ld(R4, imm=BT_ROLL_COUNT)
    a.ldi(R4, R4)
    a.cmp(R4, 0)
    a.j("jne", "bt_tab")
    a.ld(R3, imm=len(VERSION_NO))
    a.push(R3)
    a.call(rnd)
    a.sp_add(1)
    a.ld(R3, imm=BT_VERSION)
    a.sti(R1, R3)
    a.label("bt_tab")
    a.ld(R4, imm=BT_VERSION)
    a.ldi(R4, R4)
    for name, no in sorted(VERSION_NO.items(), key=lambda kv: kv[1]):
        a.cmp(R4, no)
        a.j("jne", "bt_v%d" % no)
        ret(every[name])
        a.label("bt_v%d" % no)
    _by_line(a, "bt_line", lines, lambda line: ret(tables[line]), lambda: ret(BATTLE["V3"]))


# ---------------------------------------------------------------- link identity and jogress
# The link packet (0x20448A) carries the device version (0x0CF5, set to 2 at link start) and the pet id;
# the receiver (0x2043C9) stores them in 0x0CF9 / 0x0CFA. Ids in the packet are local to the version.
VERSION_NO = {"V1": 0, "V2": 1, "V3": 2, "V4": 3, "V5": 4}
LK_START = 0x2060C2     # r3 = 2 ; ds = 0 ; r4 = 0x0CF5 ; [ds:r4] = r3
LK_SEND = 0x2044D8      # ds = 0 ; r4 = 0x0826 ; r3 = [ds:r4]        (-> packet word 0x0CCE)
LK_RECV = 0x204433      # 0x0CFA = [0x0CBC]                           (partner id)
JG_CHECK = (0x2061DB, 0x20627A)    # decision part of the jogress check -> [bp+0] flag, [bp+1] partner bits
JG_RESULT = (0x201066, 0x20109E)   # screen 25: resulting id, style and two colours
# Jogress rules (V1/V2: 0x20607D in both dumps; V3/V4/V5: 0x2061D7 identical in the three dumps):
# (line, own local id, partner version, partner local id, bits put in the high byte of 0x0827)
JOGRESS = [("V1", 14, "V2", 15, 0), ("V2", 15, "V1", 14, 0),
           ("V3", 14, "V5", 14, 0x400), ("V3", 17, "V4", 14, 0x200),
           ("V4", 14, "V3", 17, 0x100), ("V4", 14, "V5", 14, 0x400),
           ("V5", 14, "V3", 14, 0x100), ("V5", 14, "V4", 14, 0x200)]
# Screen 25 result block in each dump: V1/V2 one result; V3/V4/V5 two, chosen by the high byte of 0x0827.
RESULT_V12 = (0x2007E1, 0x2007F7)
RESULT_V345 = 0x20106A


def result_blocks(rd, name, base):
    """[(hb or None, words)] from the dump's screen-25 code; the id load becomes base + id."""
    def fix(words):
        assert words[0] & 0xFFC0 == 0x9640, hex(words[0])          # r3 = imm6 (resulting id)
        return [0x970B, base + (words[0] & 0x3F)] + words[1:]
    if name in ("V1", "V2"):
        a, e = RESULT_V12
        return [(None, fix([rd(x) for x in range(a, e)]))]
    a = RESULT_V345
    head = [rd(a + k) for k in range(7)]
    assert head[:5] == [0xB90C, 0xFF00, 0xD800, 0x9800, 0x490C] and head[6] == 0x4E17, head
    blk_a = [rd(a + 7 + k) for k in range(0x17)]
    assert blk_a[-1] == 0xEE16
    blk_b = [rd(a + 7 + 0x17 + k) for k in range(0x16)]
    assert a + 7 + 0x17 + 0x16 == JG_RESULT[1]
    return [(head[5], fix(blk_a[:-1])), (None, fix(blk_b))]


def link_helpers(a, lines, rds):
    """lk_ver, lk_local, lk_rx, jg_check, jg_result. lines = all (name, base, count) incl. V3."""
    base = {n: b for n, b, _ in lines}

    def ld_mem(r, addr):
        a.ld(r, imm=addr)
        a.ldi(r, r)

    # lk_ver: 0x0CF5 = version of the pet's line
    def ver_body(line):
        a.ld(R3, imm=VERSION_NO[line])
        a.ld(R4, imm=0x0CF5)
        a.sti(R3, R4)
        a.retf()
    _by_line(a, "lk_ver", lines, ver_body, lambda: ver_body("V3"))

    # lk_local: r3 = pet id local to its version (after _by_line's test r3 = id - base already)
    def local_default():
        a.ld(R3, rs=R4)
        a.retf()
    _by_line(a, "lk_local", lines, lambda line: a.retf(), local_default)

    # lk_rx: 0x0CFA = base of the partner version + received id (raw if unknown)
    a.label("lk_rx")
    ld_mem(R3, 0x0CBC)
    ld_mem(R4, 0x0CBB)
    for k, (name, b, cnt) in enumerate(lines):
        a.cmp(R4, VERSION_NO[name])
        a.j("jne", "lk_rx_n%d" % k)
        a.cmp(R3, cnt)
        a.j("jae", "lk_rx_st")
        a.alu("add", R3, imm=b)
        a.j("jmp", "lk_rx_st")
        a.label("lk_rx_n%d" % k)
    a.label("lk_rx_st")
    a.ld(R4, imm=0x0CFA)
    a.sti(R3, R4)
    a.retf()

    # jg_check: r1 = 1 and r2 = partner bits when (pet id, partner version, partner id) is a jogress pair
    a.label("jg_check")
    a.ld(R1, imm=0)
    a.ld(R2, imm=0)
    ld_mem(R4, ID)
    for k, (line, own, pv, pid, bits) in enumerate(JOGRESS):
        nxt = "jg_n%d" % k
        a.cmp(R4, imm=base[line] + own)
        a.j("jne", nxt)
        ld_mem(R3, 0x0CF9)
        a.cmp(R3, imm=VERSION_NO[pv])
        a.j("jne", nxt)
        ld_mem(R3, 0x0CFA)
        a.cmp(R3, imm=base[pv] + pid)
        a.j("jne", nxt)
        a.ld(R1, imm=1)
        a.ld(R2, imm=bits)
        a.retf()
        a.label(nxt)
    a.retf()

    # jg_result: 0x0CD7 id (moved to the line), 0x0CD8 style, 0x0CD9/0x0CDA colours, from the line's dump
    def res_body(line):
        blocks = result_blocks(rds[line], line, base[line])
        if len(blocks) == 1:
            a.w(*blocks[0][1])
            a.retf()
            return
        (hb, wa), (_, wb) = blocks
        other = "jr_%s_b" % line
        ld_mem(R4, 0x0827)
        a.w(0xB90C, 0xFF00)                       # r4 = r4 & 0xFF00
        a.cmp(R4, imm=hb)
        a.jf("jne", other)
        a.w(*wa)
        a.retf()
        a.label(other)
        a.w(*wb)
        a.retf()
    _by_line(a, "jg_result", [ln for ln in lines if ln[0] != "V3"], res_body, lambda: res_body("V3"))
