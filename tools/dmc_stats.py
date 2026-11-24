#!/usr/bin/env python3
"""Dump the Digimon stat table and the evolution switch of a Digital Monster Color dump.

Usage: python3 dmc_stats.py DUMP.bin
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unsp_dis import Dump, decode  # noqa: E402

FIELDS = [
    "sprite", "stage", "dp?", "peso", "evo_min", "sonno_h", "sonno_m", "sveglia_h",
    "sveglia_m", "fame_min", "forza_min", "cacca_min", "f12?", "attributo", "potenza",
    "attacco1", "attacco2",
]
STAGES = ["BabyI", "BabyII", "Child", "Adult", "Perfect", "Ultimate"]
ATTR = {0: "Free", 1: "Virus", 2: "Data", 3: "Vaccine"}
RAM = {0x082F: "errori_cura", 0x0832: "allenamenti", 0x082A: "overfeed",
       0x0831: "sonno_disturbato", 0x0826: "id_digimon"}


def find_get_stat(d):
    for a in range(0x200000, 0x210000):
        if (d.read(a) == 0x9807 and (d.read(a + 1) & 0xFFC0) == 0x4840 and d.read(a + 2) == 0x8E02
                and d.read(a + 3) == 0x9840 and d.read(a + 4) == 0xD807 and d.read(a + 5) == 0x9808
                and (d.read(a + 6) & 0xFFC0) == 0x4840):
            b = a
            while d.read(b) != 0x990C:
                b += 1
            return {
                "func": a - 4,
                "max_id_at": a + 1,
                "max_id": d.read(a + 1) & 0x3F,
                "nfields": (d.read(a + 6) & 0x3F) + 1,
                "table": 0x200000 | d.read(b + 1),
            }
    return None


def find_evo_switch(d):
    """Pattern: r4=[0x0826] ... r3 = TABLE ; r4 = 0x20 ; test r2, r2 lsl 4 ... cmp r4, NCASES."""
    for a in range(0x200000, 0x210000):
        if d.read(a) == 0x970B and d.read(a + 2) == 0x9860 and d.read(a + 3) == 0xC55A:
            # must be preceded by a load of 0x0826
            if not any(d.read(b) == 0x990C and d.read(b + 1) == 0x0826 for b in range(a - 10, a)):
                continue
            ncases = None
            for b in range(a, a + 20):
                if (d.read(b) & 0xFFC0) == 0x4840 and d.read(b) & 0x3F:
                    ncases = d.read(b) & 0x3F
            return {"table": 0x200000 | d.read(a + 1), "ncases": ncases, "at": a}
    return None


def case_summary(d, start, stop):
    """Very small symbolic reading of one switch case: list conditions and result ids."""
    out, a = [], start
    while a < stop:
        i = decode(d.read, a)
        t = i.text
        if i.imm in RAM:
            t = t.replace("0x%04x" % i.imm, RAM[i.imm])
        out.append("%06x  %s" % (a, t))
        if i.kind == "jmp" and i.target is not None and not (start <= i.target < stop):
            break
        a += i.size
    return out


def main():
    d = Dump(sys.argv[1])
    gs = find_get_stat(d)
    print("get_stat()           @ word %06x" % gs["func"])
    print("limite id (cmp)      @ word %06x  = %d  -> %d Digimon" % (gs["max_id_at"], gs["max_id"], gs["max_id"] + 1))
    print("tabella stats        @ word %06x (byte 0x%05x), %d campi per record\n"
          % (gs["table"], (gs["table"] - 0x200000) * 2, gs["nfields"]))
    print("id " + " ".join("%9s" % f for f in FIELDS))
    for i in range(gs["max_id"] + 1):
        r = [d.read(gs["table"] + i * gs["nfields"] + k) for k in range(gs["nfields"])]
        cells = []
        for k, v in enumerate(r):
            if k == 1:
                cells.append("%9s" % STAGES[v] if v < len(STAGES) else "%9d" % v)
            elif k == 13:
                cells.append("%9s" % ATTR.get(v, v))
            elif v == 0xFFFF:
                cells.append("%9s" % "-")
            else:
                cells.append("%9d" % v)
        print("%2d " % i + " ".join(cells))

    ev = find_evo_switch(d)
    if ev:
        n = ev["ncases"]
        targets = [d.read(ev["table"] + 2 * k) | (d.read(ev["table"] + 2 * k + 1) << 16) for k in range(n)]
        print("\nswitch evoluzione    @ word %06x, jump table @ %06x, %d casi (id 0..%d)"
              % (ev["at"], ev["table"], n, n - 1))
        for k, t in enumerate(targets):
            print("  id %2d -> codice @ %06x" % (k, t))


if __name__ == "__main__":
    main()
