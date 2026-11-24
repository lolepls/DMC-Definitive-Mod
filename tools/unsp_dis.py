#!/usr/bin/env python3
"""Minimal mu'nSP (GeneralPlus / Sunplus) disassembler for Digimon Color flash dumps.

The SPI flash is mapped at CPU word address 0x200000, so
    byte_offset = (word_addr - 0x200000) * 2

Usage:
    python3 unsp_dis.py DUMP.bin                 # recursive disassembly of all code -> stdout
    python3 unsp_dis.py DUMP.bin 0x200000 0x40   # linear disassembly of N words at word addr
"""
import struct
import sys

FLASH_BASE = 0x200000
REGS = ["sp", "r1", "r2", "r3", "r4", "bp", "sr", "pc"]
JUMPS = ["jb", "jae", "jge", "jl", "jne", "je", "jpl", "jmi",
         "jbe", "ja", "jle", "jg", "jvc", "jvs", "jmp"]
ALU = {0: "+=", 1: "+=c", 2: "-=", 3: "-=c", 4: "cmp", 6: "=-", 8: "^=",
       9: "=", 10: "|=", 11: "&=", 12: "test", 13: "store"}
ALU3 = {0: "+", 1: "+c", 2: "-", 3: "-c", 4: "cmp", 6: "-", 8: "^",
        9: "", 10: "|", 11: "&", 12: "test", 13: ""}
F_MISC = {0xF140: "int off", 0xF141: "int irq", 0xF142: "int fiq", 0xF143: "int fiq,irq",
          0xF148: "irq off", 0xF149: "irq on", 0xF14C: "fiq off", 0xF14E: "fiq on",
          0xF160: "break", 0xF165: "nop"}


class Insn:
    __slots__ = ("addr", "size", "text", "kind", "target", "ds_imm", "mem_ref", "imm")

    def __init__(self, addr):
        self.addr = addr
        self.size = 1
        self.text = ".dw ?"
        self.kind = "op"       # op | jcc | jmp | call | ret | bad
        self.target = None
        self.ds_imm = None     # value for "ds = imm6"
        self.mem_ref = None    # absolute [imm16] / [imm6] operand
        self.imm = None        # imm16 operand


def _indirect(opn, rb):
    d = "ds:" if opn & 4 else ""
    r = REGS[rb]
    return ["[%s%s]", "[%s%s--]", "[%s%s++]", "[%s++%s]"][opn & 3] % (d, r) if (opn & 3) != 3 \
        else "[%s++%s]" % (d, r)


def decode(read, addr):
    """read(word_addr) -> 16-bit word. Returns Insn."""
    w = read(addr)
    nxt = lambda: read(addr + 1)
    i = Insn(addr)
    op0, opa, op1, opn, opb = w >> 12, (w >> 9) & 7, (w >> 6) & 7, (w >> 3) & 7, w & 7
    ra, rb = REGS[opa], REGS[opb]

    if w == 0x9A98:
        i.text, i.kind = "retf", "ret"; return i
    if w == 0x9A90:
        i.text, i.kind = "reti", "ret"; return i

    # relative branches
    if op0 < 15 and opa == 7 and op1 < 2:
        off = w & 0x3F
        tgt = addr + 1 + (off if op1 == 0 else -off)
        i.target = tgt
        i.kind = "jmp" if op0 == 14 else "jcc"
        i.text = "%s 0x%06x" % (JUMPS[op0], tgt)
        return i

    if op0 == 15:
        if op1 == 1 and opa == 0:
            tgt = ((w & 0x3F) << 16) | nxt()
            i.size, i.kind, i.target = 2, "call", tgt
            i.text = "call 0x%06x" % tgt
            return i
        if op1 == 2 and opa == 7:
            tgt = ((w & 0x3F) << 16) | nxt()
            i.size, i.kind, i.target = 2, "jmp", tgt
            i.text = "jmpf 0x%06x" % tgt
            return i
        if (w & 0xFFC0) == 0xFE00:
            i.ds_imm = w & 0x3F
            i.text = "ds = 0x%02x" % (w & 0x3F)
            return i
        if w in F_MISC:
            i.text = F_MISC[w]; return i
        if op1 == 0 and opa == 0 and opn == 5:
            i.text = "ds = %s" % rb; return i
        if op1 == 0 and opa == 0 and opn == 4:
            i.text = "%s = ds   ;?" % rb; return i
        if op1 == 0 and opn == 1:
            i.text = "mr = %s*%s, us" % (ra, rb); return i
        if op1 == 4 and opn == 1:
            i.text = "mr = %s*%s" % (ra, rb); return i
        if op1 in (3, 7):
            i.text = "mr = [%s]*[%s], n=%d  ;muls" % (ra, rb, opn or 16); return i
        i.text, i.kind = ".dw 0x%04x  ;f-ext?" % w, "bad"
        return i

    if op0 == 14 and not (op1 == 2 or op1 == 3):
        # unSP 1.2 extended (shift by reg, bit ops...) - not fully decoded
        i.text, i.kind = ".dw 0x%04x  ;e-ext?" % w, "bad"
        return i

    if op0 not in ALU:
        i.text, i.kind = ".dw 0x%04x" % w, "bad"
        return i

    if op1 == 2:
        if op0 == 13:   # push
            regs = REGS[opa - opn + 1: opa + 1] if opn else []
            i.text = "push %s to [%s]" % (",".join(regs), rb)
            return i
        if op0 == 9:    # pop
            regs = REGS[opa + 1: opa + opn + 1]
            i.text = "pop %s from [%s]" % (",".join(regs), rb)
            if "pc" in regs:
                i.kind = "ret"
            return i
        i.text, i.kind = ".dw 0x%04x" % w, "bad"
        return i

    sym = ALU[op0]
    sym3 = ALU3[op0]

    def fmt_dst_src(src):
        if op0 == 13:
            return "%s = %s" % (src, ra)
        if op0 in (4, 12):
            return "%s %s, %s" % (sym, ra, src)
        return "%s %s %s" % (ra, sym, src)

    if op1 == 0:
        i.text = fmt_dst_src("[bp+0x%02x]" % (w & 0x3F))
    elif op1 == 1:
        i.text = fmt_dst_src("0x%02x" % (w & 0x3F))
    elif op1 == 3:
        i.text = fmt_dst_src(_indirect(opn, opb))
    elif op1 == 4:
        if opn == 0:
            i.text = fmt_dst_src(rb)
        elif opn == 1:
            imm = nxt(); i.size = 2; i.imm = imm
            if op0 == 9:
                i.text = "%s = 0x%04x" % (ra, imm)
            elif op0 in (4, 12):
                i.text = "%s %s, 0x%04x" % (sym, rb, imm)
            elif op0 == 13:
                i.text = ".dw store-imm?"; i.kind = "bad"
            else:
                i.text = "%s = %s %s 0x%04x" % (ra, rb, sym3, imm)
        elif opn == 2:
            a = nxt(); i.size = 2; i.mem_ref = a
            if op0 == 9:
                i.text = "%s = [0x%04x]" % (ra, a)
            elif op0 == 13:
                i.text = "[0x%04x] = %s" % (a, ra)
            elif op0 in (4, 12):
                i.text = "%s %s, [0x%04x]" % (sym, rb, a)
            else:
                i.text = "%s = %s %s [0x%04x]" % (ra, rb, sym3, a)
        elif opn == 3:
            a = nxt(); i.size = 2; i.mem_ref = a
            i.text = "[0x%04x] = %s %s %s" % (a, rb, sym3 or "", ra)
        else:
            i.text = fmt_dst_src("%s asr %d" % (rb, opn - 3))
    elif op1 == 5:
        i.text = fmt_dst_src("%s %s %d" % (rb, "lsl" if opn < 4 else "lsr", (opn & 3) + 1))
    elif op1 == 6:
        i.text = fmt_dst_src("%s %s %d" % (rb, "rol" if opn < 4 else "ror", (opn & 3) + 1))
    elif op1 == 7:
        i.mem_ref = w & 0x3F
        i.text = fmt_dst_src("[0x%02x]" % (w & 0x3F))

    # writes to pc = jumps
    if opa == 7 and op0 == 9:
        if op1 == 4 and opn == 1:
            i.kind = "jmp"; i.target = (addr & 0x3F0000) | i.imm
        else:
            i.kind = "jmp"   # computed jump
    return i


class Dump:
    def __init__(self, path):
        self.data = open(path, "rb").read()

    def read(self, waddr):
        off = (waddr - FLASH_BASE) * 2
        if 0 <= off < len(self.data) - 1:
            return struct.unpack_from("<H", self.data, off)[0]
        return 0xFFFF

    def in_flash(self, waddr):
        return 0 <= (waddr - FLASH_BASE) * 2 < len(self.data)


def code_regions(dump):
    """Word ranges that contain code (from the entropy map)."""
    return [(0x200000, 0x210000), (0x230000, 0x230400)]


def recursive(dump, extra_entries=()):
    """Recursive descent from every call target found by a linear scan. Returns (insns, funcs)."""
    regions = code_regions(dump)
    inreg = lambda a: any(s <= a < e for s, e in regions)
    entries = set(extra_entries)
    for s, e in regions:
        for a in range(s, e):
            w = dump.read(a)
            if (w & 0xFFC0) == 0xF040:
                t = ((w & 0x3F) << 16) | dump.read(a + 1)
                if inreg(t):
                    entries.add(t)
    # filter entries: keep those that decode to a plausible first insn
    funcs = set()
    insns = {}
    work = list(sorted(entries))
    funcs.update(entries)
    while work:
        a = work.pop()
        while inreg(a) and a not in insns:
            ins = decode(dump.read, a)
            if ins.kind == "bad":
                break
            insns[a] = ins
            if ins.kind == "call" and inreg(ins.target):
                if ins.target not in funcs:
                    funcs.add(ins.target); work.append(ins.target)
            if ins.kind in ("jcc", "jmp") and ins.target is not None and inreg(ins.target):
                work.append(ins.target)
            if ins.kind in ("jmp", "ret"):
                break
            a += ins.size
    return insns, funcs


def code_end(dump):
    """Last non-zero word of the main code region."""
    a = 0x210000
    while a > 0x200000 and dump.read(a - 1) == 0:
        a -= 1
    return a


def full_analysis(dump):
    """Recursive descent seeded by call targets, 'push bp' prologues and 32-bit function
    pointers (lo, 0x0020) found in the data gaps; iterated to a fixed point."""
    end = code_end(dump)
    seeds = set(a for a in range(0x200000, end)
                if dump.read(a) == 0xDA88 and dump.read(a + 1) == 0x0B08)
    while True:
        insns, funcs = recursive(dump, seeds)
        covered = set()
        for a, i in insns.items():
            covered.update(range(a, a + i.size))
        new = set()
        a = 0x200000
        while a < end:
            if a not in covered and a + 1 not in covered and dump.read(a + 1) == 0x0020:
                t = 0x200000 | dump.read(a)
                if t < end and t not in seeds and t not in covered:
                    new.add(t)
            a += 1
        if not new:
            return insns, funcs, covered
        seeds |= new


def gaps(insns, start, end):
    out, last = [], start
    for a in sorted(insns):
        if a > last:
            out.append((last, a))
        last = max(last, a + insns[a].size)
    if last < end:
        out.append((last, end))
    return out


def main():
    d = Dump(sys.argv[1])
    if len(sys.argv) >= 3:
        a = int(sys.argv[2], 16); n = int(sys.argv[3], 16) if len(sys.argv) > 3 else 0x40
        end = a + n
        while a < end:
            ins = decode(d.read, a)
            words = " ".join("%04x" % d.read(a + k) for k in range(ins.size))
            print("%06x  %-10s %s" % (a, words, ins.text))
            a += ins.size
        return
    insns, funcs = recursive(d)
    last = None
    for a in sorted(insns):
        ins = insns[a]
        if last is not None and a != last:
            print("        ; ---- gap 0x%06x-0x%06x (%d words, data?) ----" % (last, a, a - last))
        if a in funcs:
            print("\nsub_%06x:" % a)
        words = " ".join("%04x" % d.read(a + k) for k in range(ins.size))
        print("%06x  %-10s %s" % (a, words, ins.text))
        last = a + ins.size


if __name__ == "__main__":
    main()
