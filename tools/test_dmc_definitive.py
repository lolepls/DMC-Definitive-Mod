#!/usr/bin/env python3
"""Run the patch code of a "DMC Definitive" image in the u'nSP simulator and check the results.

Usage: python3 test_dmc_definitive.py BUILD.bin V1 V2 V3 V4 V5
"""
import random
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unsp_sim import Sim, SR_C, SR_Z  # noqa: E402
from build_dmc_definitive import Version, ORDER, EXTRA, OFFTAB, LINEBUF, TRANSPARENT  # noqa: E402

RET = (0x20, 0x7000)          # fake return address (inside segment 0x20)
BPF, SPF = 0x0F00, 0x0EE0     # fake caller frame / stack (RAM)
ID = 0x0826


def labels_from_log(img):
    """Re-derive the entry points from the patched call sites."""
    rd = lambda w: struct.unpack_from("<H", img, (w - 0x200000) * 2)[0]  # noqa: E731
    tgt = lambda w: ((rd(w) & 0x3F) << 16) | rd(w + 1)  # noqa: E731
    return {"lim_bp5": tgt(0x2026C2), "lim_bp7": tgt(0x20921B), "lim_ds": tgt(0x203C40),
            "remap": tgt(0x207605), "evo": tgt(0x202078), "petinit": tgt(0x208E10),
            "blit_fwd": tgt(0x207F78), "blit_rev": tgt(0x207FC3),
            "subtab": 0x200000 | rd(0x20043E), "sizes": (rd(0x207659) & 0x3F) << 16 | rd(0x207658),
            "stats": (rd(0x20922C) & 0x3F) << 16 | rd(0x20922A)}


def new_sim(img):
    s = Sim(img)
    s.r[0], s.r[5], s.r[6] = SPF, BPF, 0x20

    def flash_read(sim):
        sp = sim.r[0]
        dst, n, lo, hi = (sim.ram[sp + 3 + k] for k in range(4))
        a = hi << 16 | lo
        for k in range(n):
            sim.ram[dst + k] = struct.unpack_from("<H", img, a + 2 * k)[0]
    s.rom[0x49CC] = flash_read

    def mul32(sim):                               # ROM 0x5F8B: r2:r1 = (a_hi:a_lo) * (b_hi:b_lo)
        m = [sim.ram[sim.r[0] + 3 + k] for k in range(4)]
        p = ((m[1] << 16 | m[0]) * (m[3] << 16 | m[2])) & 0xFFFFFFFF
        sim.r[1], sim.r[2] = p & 0xFFFF, p >> 16
    s.rom[0x5F8B] = mul32
    return s


def run(s, target):
    s.call_far(target, RET[1], RET[0])
    assert s.r[0] == SPF, "stack non bilanciato: %04x" % s.r[0]
    assert s.r[5] == BPF, "bp non ripristinato"


def check(cond, msg):
    if not cond:
        raise SystemExit("FALLITO: " + msg)


def main():
    img = open(sys.argv[1], "rb").read()
    vers = {n: Version(n, p) for n, p in zip(["V1", "V2", "V3", "V4", "V5"], sys.argv[2:7])}
    L = labels_from_log(img)
    rdw = lambda w: struct.unpack_from("<H", img, (w - 0x200000) * 2)[0]  # noqa: E731
    lines, base = [("V3", 0, 20)], 20
    for n in ORDER:
        lines.append((n, base, vers[n].n))
        base += vers[n].n
    last = base - 1
    nspr = 0
    while struct.unpack_from("<I", img, OFFTAB + 4 * nspr)[0]:
        nspr += 1

    # ---------------- id-limit helpers
    for name, setup in (("lim_bp5", lambda s, v: s.wr(BPF + 5, v)), ("lim_bp7", lambda s, v: s.wr(BPF + 7, v)),
                        ("lim_ds", lambda s, v: (s.wr(0x0826, v), s.__setattr__("ds", 0),
                                                 s.r.__setitem__(4, 0x0826)))):
        for v in (0, 19, 20, last - 1, last, last + 1, 0xFFFF):
            s = new_sim(img)
            setup(s, v)
            run(s, L[name])
            jbe = (not s.flag(SR_C)) or s.flag(SR_Z)
            check(s.r[4] == v, "%s: r4" % name)
            check(jbe == (v <= last), "%s: id %d -> jbe %s" % (name, v, jbe))
    print("limiti id: ok (accettati 0..%d)" % last)

    # ---------------- sprite remap
    for name, b, cnt in lines:
        for idv in (b, b + cnt - 1):
            for spr in (0, 122, 133, 135, 159, 160, 182, 190, 201, 500, nspr - 1, nspr, 0x7FFF):
                s = new_sim(img)
                s.wr(ID, idv)
                s.wr(BPF + 0x2A, spr)
                run(s, L["remap"])
                got = s.ram[BPF + 0x2A]
                if spr >= nspr:
                    exp = 0
                elif name != "V3" and spr in EXTRA[name]:
                    exp = None            # must be a new sprite with the version's pixels
                else:
                    exp = spr
                if exp is None:
                    v = vers[name]
                    w, h, px = v.sprite(spr)
                    off = 0x80000 + struct.unpack_from("<I", img, OFFTAB + 4 * got)[0]
                    sw, sh = rdw(L["sizes"] + 2 * got), rdw(L["sizes"] + 2 * got + 1)
                    check(got >= 628 and (sw, sh) == (w, h) and img[off:off + len(px)] == px,
                          "remap %s id %d sprite %d -> %d" % (name, idv, spr, got))
                else:
                    check(got == exp, "remap %s id %d sprite %d -> %d (atteso %d)" % (name, idv, spr, got, exp))
    print("rimappaggio sprite: ok")

    # ---------------- stats table and Digimon sprites
    for name, b, cnt in lines:
        v = vers[name]
        for d in range(cnt):
            row = [rdw(L["stats"] + (b + d) * 17 + f) for f in range(17)]
            orig = v.row(d)
            check(row[1:] == orig[1:], "stats %s %d" % (name, d))
            for f in range(15):
                w, h, px = v.sprite(orig[0] + f)
                i = row[0] + f
                off = 0x80000 + struct.unpack_from("<I", img, OFFTAB + 4 * i)[0]
                check((rdw(L["sizes"] + 2 * i), rdw(L["sizes"] + 2 * i + 1)) == (w, h)
                      and img[off:off + len(px)] == px, "sprite %s id %d frame %d" % (name, d, f))
    print("tabella stats e frame dei Digimon: ok (%d Digimon, %d sprite)" % (last + 1, nspr))

    # ---------------- pet_init hook
    eggs = [b for _, b, _ in sorted(lines)]          # selection order V1, V2, V3, V4, V5
    for name, b, cnt in lines:
        for idv in (b, b + cnt // 2, b + cnt - 1):
            for ptr in (0x0826, 0x0838):
                s = new_sim(img)
                s.wr(BPF + 3, ptr)
                s.wr(ptr, idv)
                run(s, L["petinit"])
                exp = b if ptr == 0x0826 else 0
                check(s.r[2] == exp and s.r[3] == ptr, "pet_init %s id %d ptr %x -> %d" % (name, idv, ptr, s.r[2]))
    for bad in (last + 1, 0x7FFF):
        s = new_sim(img)
        s.wr(BPF + 3, 0x0826)
        s.wr(0x0826, bad)
        run(s, L["petinit"])
        check(s.r[2] == 0, "pet_init id invalido")
    print("pet_init: ok (uova %s)" % eggs)

    # ---------------- egg selection (sub-state 0 of screen 3) + line backgrounds
    egg = rdw(L["subtab"]) | rdw(L["subtab"] + 1) << 16
    line_bg = {0: 2, 20: 0, 38: 1, 56: 3, 75: 4}
    calls, drawn, bgs = [], [], []

    def egg_sim(unlocked, cur_bg, idv):
        s = new_sim(img)
        for a, tag in ((0x200069, "beep"), (0x2072F5, "clear"), (0x202724, "flush"),
                       (0x208E0D, "petinit"), (0x20912A, "timers"), (0x203B88, "save")):
            s.rom[a] = (lambda t: (lambda sim: calls.append(t)))(tag)
        s.rom[0x2075FC] = lambda sim: drawn.append([sim.ram[sim.r[0] + 3 + k] for k in range(5)])
        s.rom[0x20267C] = lambda sim: bgs.append(sim.ram[sim.r[0] + 3])
        s.wr(0x084C, unlocked)
        s.wr(0x0824, cur_bg)
        s.wr(0x0C8A, 0)
        s.wr(0x0C8B, 0xFFFF)
        s.wr(ID, idv)
        run(s, egg)
        return s

    # first egg after reset (only background 5 unlocked): preview follows the egg's line
    for start in (77, 0, 56):                     # after reset the choice always starts from the V1 egg
        s = egg_sim(0x0020, 2, start)
        check(s.ram[0x0C8B] == 0 and s.ram[ID] == 20 and drawn[-1][0] == 9 and drawn[-1][2:] == [0x18, 0x18, 1],
              "uovo: prima schermata da id %d -> %d %s" % (start, s.ram[ID], drawn[-1]))
        check(bgs[-1] == 0, "uovo: anteprima sfondo V1")
    for unlocked, start, exp in ((0x0028, 56, 56), (0x0028, 0, 0), (0x0028, 77, 20)):   # after a death
        s2 = egg_sim(unlocked, 3, start)
        check(s2.ram[ID] == exp, "uovo dopo la morte: id %d -> %d" % (start, s2.ram[ID]))
    s = egg_sim(0x0020, 2, 77)
    seq = []
    for k in range(len(eggs) + 1):
        seq.append(s.ram[ID])
        check(bgs[-1] == line_bg[s.ram[ID]], "uovo: anteprima sfondo id %d -> %d" % (s.ram[ID], bgs[-1]))
        s.wr(0x081B, 0x08)
        run(s, egg)
    check(seq == eggs + [eggs[0]] and eggs == [20, 38, 0, 56, 75], "uovo: ciclo %s" % seq)
    s.wr(0x081B, 0)
    run(s, egg)
    check(s.ram[0x0C8A] == 0, "uovo: senza tasti resta")
    s.wr(ID, 56)
    s.wr(0x081B, 0x10)
    calls.clear()
    run(s, egg)
    check(s.ram[0x0C8A] == 1 and calls == ["beep", "petinit", "timers", "save"], "uovo: conferma %s" % calls)
    check(s.ram[0x0824] == 3 and s.ram[0x084C] == 0x0028, "primo uovo V4: sfondo %d bitset %04x"
          % (s.ram[0x0824], s.ram[0x084C]))
    # next egg (after a death): unlock the line background, keep the current one
    for unlocked, cur, idv, exp_bits in ((0x0028, 3, 0, 0x002C), (0x0024, 7, 75, 0x0034), (0x1FFF, 9, 20, 0x1FFF)):
        s = egg_sim(unlocked, cur, idv)
        check(bgs[-1] == cur, "uovo successivo: anteprima con lo sfondo attuale")
        s.wr(0x081B, 0x10)
        run(s, egg)
        check(s.ram[0x0824] == cur and s.ram[0x084C] == exp_bits,
              "uovo successivo id %d: sfondo %d bitset %04x" % (idv, s.ram[0x0824], s.ram[0x084C]))
    check(rdw(0x203A3D) == 0xF060, "reset: chiamata di sblocco sostituita")
    print("scelta dell'uovo e sfondi: ok (ciclo %s)" % seq)

    # ---------------- evolution wrapper vs each version's original switch
    rnd = random.Random(1)
    nt = 0
    for name, b, cnt in lines:
        v = vers[name]
        start, table, end = v.evo_switch()
        for d in range(cnt):
            for _ in range(12):
                vars_ = {a: rnd.choice([0, 1, 2, 3, 4, 5, 6, 8, 9, 15, 16, 31, 32, 47, 48, 99])
                         for a in (0x082A, 0x082F, 0x0831, 0x0832)}
                ref = Sim(v.raw[:0x800000])
                ref.r[0], ref.r[5] = SPF, BPF
                for a, x in vars_.items():
                    ref.wr(a, x)
                ref.wr(ID, d)
                ref.call_far(start, RET[1], RET[0])
                s = new_sim(img)
                for a, x in vars_.items():
                    s.wr(a, x)
                s.wr(ID, b + d)
                run(s, L["evo"])
                exp = b + ref.ram[ID]
                check(s.ram[ID] == exp, "evoluzione %s id %d %s -> %d (atteso %d)" % (name, d, vars_, s.ram[ID], exp))
                nt += 1
    print("evoluzioni: ok (%d casi confrontati con gli switch originali)" % nt)

    # ---------------- blitter hook: rows above 4 MB, forward and mirrored
    high = [i for i in range(628, nspr) if struct.unpack_from("<I", img, OFFTAB + 4 * i)[0] + 0x80000 >= 0x400000]
    low = [i for i in range(628, nspr) if i not in set(high)]
    check(high and low, "servono sprite alti e bassi")
    rnd = random.Random(2)
    nrow = 0
    for i in rnd.sample(high, 25) + [high[0], high[-1]]:
        off = 0x80000 + struct.unpack_from("<I", img, OFFTAB + 4 * i)[0]
        w, h = rdw(L["sizes"] + 2 * i), rdw(L["sizes"] + 2 * i + 1)
        word0 = 0x200000 + off // 2
        for _ in range(4):
            y = rnd.randrange(h)
            skip = rnd.randrange(w)
            n = rnd.randint(1, w - skip)
            pos = rnd.randrange(0, 96 - n + 1)
            row = [struct.unpack_from("<H", img, off + 2 * (y * w + x))[0] for x in range(w)]
            for fwd in (True, False):
                s = new_sim(img)
                for k in range(96):
                    s.ram[LINEBUF + k] = 0x1234
                ptr = word0 + y * w + (skip if fwd else w - 1 - skip)
                s.wr(BPF + 2, n)
                s.wr(BPF + 4, pos)
                s.wr(BPF + 7, ptr & 0xFFFF)
                s.wr(BPF + 8, ptr >> 16)
                run(s, L["blit_fwd" if fwd else "blit_rev"])
                check(s.ram[BPF + 6] == n, "blitter: contatore")
                exp = [0x1234] * 96
                for k in range(n):
                    p = row[skip + k] if fwd else row[w - 1 - skip - k]
                    if p != TRANSPARENT:
                        exp[pos + k] = p
                check(s.ram[LINEBUF:LINEBUF + 96] == exp,
                      "blitter sprite %d riga %d skip %d n %d %s" % (i, y, skip, n, "avanti" if fwd else "specchio"))
                check(s.ram[BPF + 9] == 0, "blitter: frame toccato")
                nrow += 1
    for i in low[:5]:
        s = new_sim(img)
        off = 0x80000 + struct.unpack_from("<I", img, OFFTAB + 4 * i)[0]
        ptr = 0x200000 + off // 2
        s.wr(BPF + 2, 10)
        s.wr(BPF + 6, 0x5555)
        s.wr(BPF + 7, ptr & 0xFFFF)
        s.wr(BPF + 8, ptr >> 16)
        run(s, L["blit_fwd"])
        check(s.ram[BPF + 6] == 0 and s.ram[LINEBUF] == 0, "blitter: sprite basso deve usare il ciclo originale")
    print("disegno oltre 4 MB: ok (%d righe, %d sprite alti)" % (nrow, len(high)))

    # ---------------- training (screen 8): each line plays its own version's minigame
    import training as TR
    V3_TRAIN = 0x20558F
    tgt = lambda w: (rdw(w) & 0x3F) << 16 | rdw(w + 1)  # noqa: E731
    tr_init, tr_cnt, tr_tab = tgt(TR.V3_INIT_AT), tgt(TR.V3_CNT_AT), tgt(TR.V3_TAB_AT)
    LINES = {"V3": (0, 20), "V1": (20, 18), "V2": (38, 18), "V4": (56, 19), "V5": (75, 19)}
    line_of = lambda idv: next(n for n, (b, c) in LINES.items() if b <= idv < b + c)  # noqa: E731
    tabs = {}
    for idv in range(94):
        line = line_of(idv)
        s = new_sim(img)
        s.wr(ID, idv)
        run(s, tr_tab)
        t = s.r[4] << 16 | s.r[3]
        check(tabs.setdefault(line, t) == t, "allenamento: tabella id %d" % idv)
        run(s, tr_cnt)
        cnt = TR.V3_COUNT if line == "V3" else TR.TRAINING[line]["count"]
        check(s.r[3] == cnt, "allenamento: numero sotto-stati id %d = %d" % (idv, s.r[3]))
        cnt_at = {"V3": 0x205632, "V4": t + 14}.get(line, t + 2 * cnt)
        check(rdw(cnt_at) == cnt, "allenamento: conteggio nella copia %s" % line)
    check(tabs["V3"] == TR.V3_TAB and len(set(tabs.values())) == 5, "allenamento: tabelle %s" % tabs)

    # init per line (stage in 0x085C already written by the V3 dispatcher)
    def init_sim(idv, stage, d02=0):
        s = new_sim(img)
        mod = []

        def rom_mod(sim):
            mod.append((sim.ram[sim.r[0] + 3], sim.ram[sim.r[0] + 4]))
            sim.r[1] = 0x77
        s.rom[TR.ROM_MOD] = rom_mod
        s.rom[TR.RANDOM] = lambda sim: (mod.append(("rnd", sim.ram[sim.r[0] + 3])), sim.r.__setitem__(1, 4))
        s.wr(ID, idv)
        s.wr(0x085C, stage)
        s.wr(0x085D, 0x5555)
        s.wr(0x0D02, d02)
        run(s, tr_init)
        return s, mod
    for st in range(6):
        s, mod = init_sim(5, st)
        check(s.ram[0x085D] == rdw(TR.V3_SPEED + st) and not mod, "allenamento init V3 stadio %d" % st)
        s, mod = init_sim(60, st)
        v4 = vers["V4"].rd(0x204BEB + st)
        check(mod == [(v4, 0x0B)] and s.ram[0x085D] == 0x77, "allenamento init V4 stadio %d: %s" % (st, mod))
        for idv in (40, 80):
            s, mod = init_sim(idv, st)
            check(s.ram[0x085D] == 0x5555 and s.ram[0x085C] == st and not mod, "allenamento init V2/V5")
    for d02, exp, rnd in ((0, 1, False), (8, 9, False), (9, 0, False), (10, 4, True), (0xFFFF, 4, True)):
        s, mod = init_sim(25, 3, d02)
        check(s.ram[0x0D02] == exp and s.ram[0x085C] == 0 and s.ram[0x085D] == 0
              and (mod == [("rnd", 10)]) == rnd, "allenamento init V1 0x0D02 %d -> %d %s" % (d02, s.ram[0x0D02], mod))

    # full sessions: real dispatcher, real handlers and animations, drawing stubbed
    def session(idv, seed, p=0.03, skilled=None):
        rng = random.Random(seed)
        s = new_sim(img)
        log = []
        for a, tag in ((0x200069, "beep"), (0x2072F5, "clear"), (0x202724, "flush"), (0x20267C, "bg"),
                       (0x207CC6, "show"), (0x2092B2, "x"), (0x2026BE, "pet"), (0x20284F, "pet2"),
                       (0x20788B, "spr2"), (0x2079CF, "spr3"), (0x20799E, "spr4"), (0x202811, "num"),
                       (0x2093DB, "strength"), (0x209354, "weight"), (0x209527, "count")):
            s.rom[a] = (lambda t: (lambda sim: log.append((t, sim.ram[sim.r[0] + 3]))))(tag)
        s.rom[0x2075FC] = lambda sim: log.append(("spr", sim.ram[sim.r[0] + 4]))
        s.rom[0x200084] = lambda sim: sim.r.__setitem__(1, 0)
        s.rom[TR.RANDOM] = lambda sim: sim.r.__setitem__(1, rng.randrange(max(1, sim.ram[sim.r[0] + 3])))
        s.rom[0x5D4D] = lambda sim: sim.r.__setitem__(1, sim.ram[sim.r[0] + 3] // max(1, sim.ram[sim.r[0] + 4]))
        s.rom[0x5DAA] = lambda sim: sim.r.__setitem__(1, sim.ram[sim.r[0] + 3] % max(1, sim.ram[sim.r[0] + 4]))
        s.wr(ID, idv)
        s.wr(0x0C86, 1)
        s.wr(0x0C88, 8)
        s.wr(0x0C89, 0)
        s.wr(0x0D02, 3)
        for frame in range(20000):
            press = skilled(s) if skilled else rng.random() < p
            s.wr(0x081B, rng.choice((0x08, 0x10)) if press else 0)
            s.wr(0x0C86, 1)
            run(s, V3_TRAIN)
            log.append(("anim_id", s.ram[0x0CD2]))
            check(s.ram[0x0C88] == 8, "allenamento id %d: uscita inattesa dallo schermo (%d)" % (idv, s.ram[0x0C88]))
            if any(t == "count" for t, _ in log):
                break
        prize = [x for x in log if x[0] in ("strength", "weight", "count")]
        return log, prize, frame
    results = {}
    for line, idv in (("V3", 5), ("V1", 25), ("V2", 40), ("V4", 60), ("V5", 80)):
        wins = 0
        for seed in range(4):
            log, prize, frame = session(idv, seed)
            ok = prize and prize[-1] == ("count", 1) and ("weight", 0xFFFF) in prize
            check(ok, "allenamento %s seed %d: premio %s dopo %d frame" % (line, seed, prize, frame))
            won = ("strength", 1) in prize
            wins += won
            spr = set(v for t, v in log if t == "spr")
            check(all(v < 1780 for v in spr), "allenamento %s: sprite fuori tabella %s" % (line, spr))
            new = spr & set(range(1774, 1780))
            check(bool(new) == (line != "V3"), "allenamento %s: sprite nuovi disegnati %s" % (line, new))
        gauge = lambda sim: sim.ram[0x0C8A] == 0 and sim.ram[0x0C85] and sim.ram[0x0881] == 14  # noqa: E731
        good = {"V2": gauge, "V5": gauge,
                "V4": lambda sim: sim.ram[0x0C8A] == 0 and sim.ram[0x0C85] and sim.ram[0x085E] > 0x63}
        for seed in range(16):                    # skilled player, or random presses for V1/V3
            log, prize, frame = session(idv, seed, 0.3, good.get(line))
            if ("strength", 1) in prize:
                break
        check(("strength", 1) in prize and prize[-1] == ("count", 1),
              "allenamento %s: nessuna partita vincente %s" % (line, prize))
        if line == "V1":
            ran = {v for t, v in log if t == "anim_id"}
            check({23, 24, 25} <= ran, "allenamento V1: animazioni 23-27 non eseguite %s" % ran)
        results[line] = (wins, sorted(spr))
    print("allenamento: ok (tabelle/init/conteggi per linea; sessioni complete %s)"
          % ", ".join("%s %d/4 casuali vinte" % (k, v[0]) for k, v in results.items()))

    # ---------------- solo battle series per line
    bt_tab = tgt(TR.V3_BATTLE_AT[0])
    check(all(tgt(w) == bt_tab for w in TR.V3_BATTLE_AT), "battaglia: hook")
    base_of = {n: b for n, (b, c) in LINES.items()}
    nfight = 0
    for idv in range(94):
        line = line_of(idv)
        s = new_sim(img)
        s.wr(ID, idv)
        run(s, bt_tab)
        t = s.r[3] << 16 | s.r[4]
        orig = [vers[line].rd(TR.BATTLE[line] + k) for k in range(TR.BATTLE_WORDS)]
        got = [rdw(t + k) for k in range(TR.BATTLE_WORDS)]
        exp = [w + base_of[line] if k % 2 == 0 and w != 0xFFFF else w for k, w in enumerate(orig)]
        check(got == exp, "battaglia: tabella id %d (%s)" % (idv, line))
    for line, idv in (("V3", 5), ("V1", 25), ("V2", 40), ("V4", 60), ("V5", 80)):
        for area in range(8):
            for slot in range(5):
                k = TR.BATTLE[line] + area * 10 + slot * 2
                opp, power = vers[line].rd(k), vers[line].rd(k + 1)
                if opp == 0xFFFF:
                    continue
                s = new_sim(img)
                for f in (0x200069, 0x2072F5, 0x202724, 0x20267C, 0x207CC6, 0x2075FC, 0x20B78E, 0x20B7C9,
                          0x2026BE, 0x20284F, 0x2092B2, 0x203B88):
                    s.rom[f] = lambda sim: sim.r.__setitem__(1, 0)
                s.wr(ID, idv)
                s.wr(0x085C, area)
                s.wr(0x085D, slot)
                s.wr(0x0C8A, 1)
                s.wr(0x0C8B, 0)
                run(s, 0x2071F8)
                check(s.ram[0x0CFA] == opp + base_of[line] and s.ram[0x0CFB] == power,
                      "battaglia %s area %d avversario %d: %d/%d" % (line, area, slot, s.ram[0x0CFA], s.ram[0x0CFB]))
                nfight += 1
    print("battaglie: ok (serie per linea, %d avversari verificati con 0x2071F8)" % nfight)
    # ---------------- link identity and jogress
    import training as TR
    tgt = lambda w: ((rdw(w) & 0x3F) << 16) | rdw(w + 1)  # noqa: E731
    vno = TR.VERSION_NO
    cnt_of = {n: c for n, _, c in lines}

    def until(s, start, end, limit=20000):
        s.cs, s.pc = start >> 16, start & 0xFFFF
        while s.addr != end:
            s.step()
            limit -= 1
            check(limit > 0, "troppi passi da %06x" % start)

    def call_arg(s, f, arg):
        s.r[0] = SPF - 1
        s.ram[SPF] = arg
        s.call_far(f, RET[1], RET[0])
        check(s.r[0] == SPF - 1, "stack %06x" % f)

    for name, b, c in lines:
        for idv in range(b, b + c):
            s = new_sim(img)
            s.wr(ID, idv)
            run(s, 0x2060BF)                       # link start: own version
            check(s.ram[0x0CF5] == vno[name], "versione %s id %d: %d" % (name, idv, s.ram[0x0CF5]))
            call_arg(s, 0x20448A, 0)               # outgoing packet
            check(s.ram[0x0CCD] == vno[name] and s.ram[0x0CCE] == idv - b,
                  "pacchetto %s id %d: %d/%d" % (name, idv, s.ram[0x0CCD], s.ram[0x0CCE]))
    nrx = 0
    for ver in range(6):
        for pid in range(0, 24):
            s = new_sim(img)
            pk = [0x4744, 0x4C43, 0, ver, pid, 33, 2]
            for k, x in enumerate(pk):
                s.ram[0x0CB8 + k] = x
            s.ram[0x0CBF] = sum(pk) & 0xFFFF
            call_arg(s, 0x2043C9, 0)
            name = [n for n, v in vno.items() if v == ver]
            exp = pid + dict((n, b) for n, b, _ in lines)[name[0]] if name and pid < cnt_of[name[0]] else pid
            check(s.r[1] == 0 and s.ram[0x0CF9] == ver and s.ram[0x0CFA] == exp and s.ram[0x0CFB] == 33,
                  "ricezione versione %d id %d: %d" % (ver, pid, s.ram[0x0CFA]))
            nrx += 1
    print("collegamento: ok (versione e id locale inviati per tutti i %d id, %d pacchetti ricevuti)" % (last + 1, nrx))

    # jogress check: original function of each version vs the build, every own id x partner version x id
    import xver
    xver.P.update({n: os.path.abspath(p) for n, p in zip(["V1", "V2", "V3", "V4", "V5"], sys.argv[2:7])})
    func_end = xver.func_end
    base_of2 = {n: b for n, b, _ in lines}
    orig_check = {"V1": 0x20607D, "V2": 0x205F70, "V3": 0x2061D7, "V4": 0x2060E7, "V5": 0x2060FC}
    pairs, ntest = set(), 0
    for name, b, c in lines:
        ref = Sim(vers[name].raw[:0x800000])
        f = orig_check[name]
        saves = [((ref.rd(x) & 0x3F) << 16) | ref.rd(x + 1) for x in range(f, func_end(name, f))
                 if ref.rd(x) & 0xFFC0 == 0xF040]
        for own in range(c):
            for pn, pb, pc in lines:
                for pid in range(pc):
                    res = []
                    for s, own_id, part_id in ((Sim(vers[name].raw[:0x800000]), own, pid),
                                               (new_sim(img), b + own, pb + pid)):
                        s.r[0], s.r[5], s.r[6] = SPF, BPF, 0x20
                        for t in saves + [0x203B88]:
                            s.rom[t] = lambda sim: None
                        s.ram[0x0CF5], s.ram[ID], s.ram[0x0CF9], s.ram[0x0CFA] = vno[name], own_id, vno[pn], part_id
                        run(s, f if len(res) == 0 else 0x2061D7)
                        res.append((s.r[1], s.ram[0x0827], s.ram[0x084F]))
                    check(res[0] == res[1], "jogress %s %d + %s %d: originale %s, build %s"
                          % (name, own, pn, pid, res[0], res[1]))
                    if res[0][0]:
                        pairs.add((name, own, pn, pid, res[0][1] & 0xFF00))
                    ntest += 1
    check(pairs == set(TR.JOGRESS), "coppie jogress %s" % sorted(pairs))
    print("jogress (controllo): ok, %d combinazioni uguali all'originale; coppie %s"
          % (ntest, ", ".join("%s %d+%s %d" % p[:4] for p in sorted(pairs))))

    # jogress result: screen-25 block of each version's dump vs the build
    for name, b, c in lines:
        for hb in (0, 0x100, 0x200, 0x400):
            out = []
            for s, start, end, add in (
                    (Sim(vers[name].raw[:0x800000]),) + ((0x2007E1, 0x2007F7) if name in ("V1", "V2")
                                                         else (0x201066, 0x20109E)) + (b,),
                    (new_sim(img), 0x201066, 0x20109E, 0)):
                s.r[0], s.r[5], s.r[6] = SPF, BPF, 0x20
                s.ram[0x0827] = hb | 0x04
                s.ram[ID] = b + 14
                until(s, start, end)
                check(s.r[0] == SPF and s.r[5] == BPF, "stack risultato")
                out.append((s.ram[0x0CD7] + add, s.ram[0x0CD8], s.ram[0x0CD9], s.ram[0x0CDA]))
            check(out[0] == out[1], "risultato jogress %s %03x: originale %s, build %s" % (name, hb, out[0], out[1]))
    print("jogress (risultato): ok, Digimon e colori della versione per ogni linea")
    print("TUTTI I TEST SUPERATI")


if __name__ == "__main__":
    main()
