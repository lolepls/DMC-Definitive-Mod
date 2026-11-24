"""Tiny two-pass assembler for the subset of GeneralPlus u'nSP used by the DMC patches.

Every encoding here was derived from instructions found in the original firmware and is
checked against unsp_dis.decode() by self_test(). Registers: sp=0 r1..r4=1..4 bp=5 sr=6 pc=7.

    a = Asm(0x20F400)
    a.label("loop"); a.ld(3, imm=0x0826); a.ldi(3, 3); a.cmp(3, 20); a.j("jb", "loop"); a.retf()
    words = a.assemble()       # list of 16-bit words, placed at word address a.org
"""
from unsp_dis import decode

SP, R1, R2, R3, R4, BP = 0, 1, 2, 3, 4, 5
ALU = {"add": 0x0, "adc": 0x1, "sub": 0x2, "sbc": 0x3, "cmp": 0x4, "xor": 0x8,
       "ld": 0x9, "or": 0xA, "and": 0xB, "st": 0xD}
COND = {"jb": 0x0, "jcc": 0x0, "jae": 0x1, "jcs": 0x1, "jne": 0x4, "je": 0x5,
        "jbe": 0x8, "ja": 0x9, "jmp": 0xE}


class Asm:
    def __init__(self, org):
        self.org = org
        self.items = []          # (kind, payload)
        self.labels = {}

    # ---- bookkeeping -------------------------------------------------------
    def here(self):
        return self.org + sum(self._size(k, p) for k, p in self.items)

    @staticmethod
    def _size(kind, p):
        return {"w": 1, "j": 1, "far": 2, "lab": 0, "abs": 1}[kind]

    def label(self, name):
        self.items.append(("lab", name))

    def w(self, *words):
        for x in words:
            self.items.append(("w", x & 0xFFFF))

    # ---- instructions ------------------------------------------------------
    def _alu(self, op, rd, rs=None, imm=None, bp=None):
        o = ALU[op] << 12 | rd << 9
        if bp is not None:                               # rd op= [bp+imm6]
            self.w(o | bp)
        elif imm is not None and rs is None and 0 <= imm < 64:
            self.w(o | 0x40 | imm)                      # rd op= imm6
        elif imm is not None:                            # rd = rs op imm16
            self.w(o | 0x108 | (rd if rs is None else rs), imm)
        else:                                            # rd op= rs
            self.w(o | 0x100 | rs)

    def alu(self, op, rd, rs=None, imm=None, bp=None):
        """Generic ALU op: rd op= [bp+off] | imm6 | rs ; or rd = rs op imm16."""
        self._alu(op, rd, rs, imm, bp)

    def lea(self, rd, rs, imm16):                       # rd = rs + imm16 (e.g. r2 = sp + 1)
        self.w(rd << 9 | 0x108 | rs, imm16)

    def shift(self, rd, rs, kind, n):                   # rd = rs lsl/lsr n (n = 1..4)
        code = {"lsl": 0, "lsr": 4}[kind] + n - 1
        self.w(0x9000 | rd << 9 | 0x140 | code << 3 | rs)

    def pop_bp(self):
        self.w(0x9888)

    def jf(self, cond, target):
        """Conditional far jump: inverted short jump over a jmpf."""
        inv = {"jb": "jae", "jae": "jb", "je": "jne", "jne": "je", "jbe": "ja", "ja": "jbe"}[cond]
        skip = "_jf%d" % len(self.items)
        self.j(inv, skip)
        self.jmpf(target)
        self.label(skip)

    def ld(self, rd, rs=None, imm=None, bp=None):
        self._alu("ld", rd, rs, imm, bp)

    def add(self, rd, imm):
        self._alu("add", rd, imm=imm)

    def sub(self, rd, imm):
        self._alu("sub", rd, imm=imm)

    def xor(self, rd, imm):
        self._alu("xor", rd, imm=imm)

    def cmp(self, rd, imm=None, rs=None):
        self._alu("cmp", rd, rs=rs, imm=imm)

    def st_bp(self, rd, off):                           # [bp+off] = rd
        self.w(0xD000 | rd << 9 | off)

    def ldi(self, rd, rs):                              # rd = [rs]   (page 0)
        self.w(0x9000 | rd << 9 | 0xC0 | rs)

    def sti(self, rd, rs):                              # [rs] = rd   (page 0)
        self.w(0xD000 | rd << 9 | 0xC0 | rs)

    def push(self, r):
        self.w(0xD000 | r << 9 | 0x88)

    def sp_add(self, n):
        self.w(0x0040 | n)

    def call(self, target):
        self.items.append(("far", (0xF040, target)))

    def jmpf(self, target):
        self.items.append(("far", (0xFE80, target)))

    def retf(self):
        self.w(0x9A90)

    def j(self, cond, target):
        self.items.append(("j", (COND[cond], target)))

    def addr(self, target):                             # data word = low 16 bits of a label
        self.items.append(("abs", target))

    # ---- output ------------------------------------------------------------
    def _resolve(self, t):
        return self.labels[t] if isinstance(t, str) else t

    def assemble(self):
        pc = self.org
        for k, p in self.items:
            if k == "lab":
                if p in self.labels:
                    raise ValueError("label duplicata " + p)
                self.labels[p] = pc
            pc += self._size(k, p)
        out, pc = [], self.org
        for k, p in self.items:
            if k == "w":
                out.append(p)
            elif k == "abs":
                out.append(self._resolve(p) & 0xFFFF)
            elif k == "far":
                base, t = p
                t = self._resolve(t)
                out += [base | (t >> 16), t & 0xFFFF]
            elif k == "j":
                c, t = p
                t = self._resolve(t)
                d = t - (pc + 1)
                if not -63 <= d <= 63:
                    raise ValueError("salto corto fuori portata a %06x" % pc)
                out.append(c << 12 | 0x0E00 | (0x40 | -d if d < 0 else d))
            pc += self._size(k, p)
        return out


def self_test():
    """Assemble one of each form and compare with the disassembler's reading."""
    a = Asm(0x200000)
    a.ld(R4, bp=0x2A); a.ld(R3, imm=0x00); a.ld(R3, imm=0x0826); a.ld(R4, imm=0x0273)
    a.ld(R4, rs=R1); a.ldi(R3, R4); a.sti(R3, R4); a.ldi(R2, R3)
    a.cmp(R4, 0x13); a.cmp(R4, 0x0273); a.cmp(R3, rs=R4); a.add(R4, 1); a.sub(R3, 20)
    a.xor(R3, 0x14); a.st_bp(R4, 0x2A); a.push(R3); a.push(BP); a.sp_add(5)
    a.call(0x208E0D); a.jmpf(0x202111); a.retf()
    a.alu("sub", R3, rs=R2); a.alu("add", R4, rs=R3); a.alu("or", R4, rs=R2)
    a.alu("sbc", R4, imm=0); a.alu("adc", R4, imm=0); a.ld(R3, rs=6); a.ld(R4, rs=BP)
    a.shift(R4, R4, "lsl", 1); a.shift(R2, R3, "lsr", 4); a.shift(R2, R2, "lsr", 3)
    a.lea(R2, SP, 1); a.lea(BP, SP, 1); a.lea(R3, R3, 0x07BA); a.pop_bp()
    a.alu("sub", SP, imm=16); a.cmp(R3, 0x07E0)
    a.label("x"); a.j("jne", "x"); a.j("jbe", "y"); a.j("jmp", "y"); a.label("y")
    want = ["r4 = [bp+0x2a]", "r3 = 0x00", "r3 = 0x0826", "r4 = 0x0273", "r4 = r1",
            "r3 = [r4]", "[r4] = r3", "r2 = [r3]", "cmp r4, 0x13", "cmp r4, 0x0273",
            "cmp r3, r4", "r4 += 0x01", "r3 -= 0x14", "r3 ^= 0x14", "[bp+0x2a] = r4",
            "push r3 to [sp]", "push bp to [sp]", "sp += 0x05", "call 0x208e0d",
            "jmpf 0x202111", None,
            "r3 -= r2", "r4 += r3", "r4 |= r2", "r4 -=c 0x00", "r4 +=c 0x00", "r3 = sr", "r4 = bp",
            "r4 = r4 lsl 1", "r2 = r3 lsr 4", "r2 = r2 lsr 3", "r2 = sp + 0x0001",
            "bp = sp + 0x0001", "r3 = r3 + 0x07ba", "pop bp from [sp]", "sp -= 0x10",
            "cmp r3, 0x07e0", None, None, None]
    words = a.assemble()
    mem = dict((0x200000 + i, w) for i, w in enumerate(words))
    pc, k, got = 0x200000, 0, []
    while pc < 0x200000 + len(words):
        i = decode(lambda x: mem.get(x, 0), pc)
        got.append((pc, i.text, i.target))
        pc += i.size
        k += 1
    for (pc, text, tgt), exp in zip(got, want):
        if exp is not None and text != exp:
            raise AssertionError("%06x: atteso %r, ottenuto %r" % (pc, exp, text))
    jumps = got[-3:]
    assert jumps[0][2] == a.labels["x"], jumps
    assert jumps[1][2] == a.labels["y"] and jumps[2][2] == a.labels["y"], jumps
    return got


if __name__ == "__main__":
    for pc, t, _ in self_test():
        print("%06x  %s" % (pc, t))
    print("self-test OK")
