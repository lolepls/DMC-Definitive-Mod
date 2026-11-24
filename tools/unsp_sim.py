"""Minimal u'nSP interpreter used to unit-test firmware patches (not a full emulator).

Covers the instruction forms used by the compiled C code around the patches and by the
code we assemble: ALU ops with [bp+imm6] / imm6 / indirect (optionally ds:, ++/--) / reg /
imm16 / shifts / rotates, push/pop, call/jmpf/retf/reti, conditional jumps, ds=imm/ds=reg.
Flash is mapped at word 0x200000 (first 4 MB of the dump); RAM is a 64K-word array.
ROM routines (addresses < 0x8000) are Python callbacks: hook(sim) -> None.
"""
import struct

SR_N, SR_Z, SR_S, SR_C = 1 << 9, 1 << 8, 1 << 7, 1 << 6


class Halt(Exception):
    pass


class Sim:
    def __init__(self, image):
        self.img = image
        self.ram = [0] * 0x10000
        self.r = [0] * 8          # sp r1 r2 r3 r4 bp sr pc
        self.r[6] = 0x20          # SR: ds(15:10) N Z S C cs(5:0)
        self.sb = 0
        self.rom = {}
        self.steps = 0

    # ---- memory
    def rd(self, a):
        a &= 0x3FFFFF
        if a < 0x10000:
            return self.ram[a]
        if a >= 0x200000:
            return struct.unpack_from("<H", self.img, (a - 0x200000) * 2)[0]
        raise Halt("lettura fuori mappa %06x" % a)

    def wr(self, a, v):
        a &= 0x3FFFFF
        if a >= 0x10000:
            raise Halt("scrittura fuori RAM %06x" % a)
        self.ram[a] = v & 0xFFFF

    @property
    def cs(self):
        return self.r[6] & 0x3F

    @cs.setter
    def cs(self, v):
        self.r[6] = (self.r[6] & ~0x3F) | (v & 0x3F)

    @property
    def ds(self):
        return self.r[6] >> 10

    @ds.setter
    def ds(self, v):
        self.r[6] = (self.r[6] & 0x03FF) | ((v & 0x3F) << 10)

    def push(self, v):
        self.wr(self.r[0], v)
        self.r[0] = (self.r[0] - 1) & 0xFFFF

    def pop(self):
        self.r[0] = (self.r[0] + 1) & 0xFFFF
        return self.rd(self.r[0])

    def flag(self, m):
        return bool(self.r[6] & m)

    def setf(self, n, z, s=None, c=None):
        sr = self.r[6] & ~(SR_N | SR_Z)
        sr |= (SR_N if n else 0) | (SR_Z if z else 0)
        if s is not None:
            sr = (sr & ~SR_S) | (SR_S if s else 0)
        if c is not None:
            sr = (sr & ~SR_C) | (SR_C if c else 0)
        self.r[6] = sr

    # ---- execution
    def call_far(self, target, ret_pc, ret_cs):
        """Simulate a CALL from (ret_cs, ret_pc) to target; run until it returns there."""
        self.push(ret_pc)
        self.push((self.r[6] & ~0x3F) | ret_cs)
        self.pc, self.cs = target & 0xFFFF, target >> 16
        while not (self.cs == ret_cs and self.pc == ret_pc):
            self.step()
            if self.steps > 2_000_000:
                raise Halt("troppi passi")

    @property
    def addr(self):
        return self.cs << 16 | self.pc

    def fetch(self):
        w = self.rd(self.addr)
        self.pc = (self.pc + 1) & 0xFFFF
        return w

    def step(self):
        self.steps += 1
        here = self.addr
        if here in self.rom:                                # stubbed routine: emulate + return
            self.rom[here](self)
            self.r[6] = self.pop()
            self.pc = self.pop()
            self.cs = self.r[6] & 0x3F
            return
        w = self.fetch()
        op, rd, op1, opn, rs = w >> 12, (w >> 9) & 7, (w >> 6) & 7, (w >> 3) & 7, w & 7
        if (w & 0xFFC0) == 0xF040:                          # call
            lo = self.fetch()
            self.push(self.pc)
            self.push((self.r[6] & ~0x3F) | self.cs)
            self.pc, self.cs = lo, w & 0x3F
            return
        if w == 0xF161:                                     # call mr (r4:r3), screen dispatchers
            self.push(self.pc)
            self.push((self.r[6] & ~0x3F) | self.cs)
            self.pc, self.cs = self.r[3], self.r[4] & 0x3F
            return
        if (w & 0xFFC0) == 0xFE80:                          # jmpf
            lo = self.fetch()
            self.pc, self.cs = lo, w & 0x3F
            return
        if (w & 0xFFC0) == 0xFE00:                          # ds = imm6
            self.ds = w & 0x3F
            return
        if (w & 0xFFF8) == 0xF028:                          # ds = rs
            self.ds = self.r[rs]
            return
        if w in (0x9A90, 0x9A98):                           # retf / reti
            self.r[6] = self.pop()
            self.pc = self.pop()
            self.cs = self.r[6] & 0x3F
            return
        if here < 0x8000:
            raise Halt("routine ROM non simulata %04x" % here)
        if rd == 7 and op1 in (0, 1) and op not in (0xD, 0xF):  # conditional / short jump
            d = w & 0x3F
            tgt = (self.pc - d) if (w & 0x40) else (self.pc + d)
            if self.cond(op):
                self.pc = tgt & 0xFFFF
            return
        if op == 0xD and op1 == 2:                          # push rd..(rd-n+1)
            for k in range(opn):
                self.push(self.r[rd - k] if rd - k != 6 else self.r[6])
            return
        if op == 0x9 and op1 == 2:                          # pop (rd+1)..(rd+n)
            for k in range(1, opn + 1):
                v = self.pop()
                if rd + k == 7:
                    self.pc = v
                    self.cs = self.r[6] & 0x3F
                else:
                    self.r[rd + k] = v
            return
        self.alu(w, op, rd, op1, opn, rs)

    def cond(self, c):
        N, Z, S, C = (self.flag(m) for m in (SR_N, SR_Z, SR_S, SR_C))
        return {0x0: not C, 0x1: C, 0x2: not S, 0x3: S, 0x4: not Z, 0x5: Z, 0x6: not N, 0x7: N,
                0x8: (not C) or Z, 0x9: C and not Z, 0xA: Z or (S != N), 0xB: (not Z) and S == N,
                0xE: True}[c]

    def ea_indirect(self, opn, rs):
        d, mode = opn >> 2, opn & 3
        seg = self.ds if d else 0
        v = self.r[rs]
        if mode == 3:
            v = (v + 1) & 0xFFFF
            self.r[rs] = v
        a = seg << 16 | v
        if mode == 1:
            self.r[rs] = (v - 1) & 0xFFFF
        if mode == 2:
            self.r[rs] = (v + 1) & 0xFFFF
            if d and self.r[rs] == 0:
                self.ds = self.ds + 1
        return a

    def shifted(self, opn, rs, rotate):
        v = self.r[rs]
        n = (opn & 3) + 1
        if not rotate:
            if opn < 4:
                self.sb = (v >> (16 - n)) & 0xF
                return (v << n) & 0xFFFF
            self.sb = (v << (4 - n)) & 0xF
            return v >> n
        full = (self.sb << 16) | v if opn < 4 else (v << 4) | self.sb
        if opn < 4:
            full = ((full << n) | (full >> (20 - n))) & 0xFFFFF
            self.sb = full >> 16
            return full & 0xFFFF
        full = ((full >> n) | (full << (20 - n))) & 0xFFFFF
        self.sb = full & 0xF
        return full >> 4

    def alu(self, w, op, rd, op1, opn, rs):
        store_to = None
        if op1 == 0:                                        # [bp+imm6]
            a = (self.r[5] + (w & 0x3F)) & 0xFFFF
            src = self.rd(a)
            store_to = a
            a_val = self.r[rd]
        elif op1 == 1:                                      # imm6
            src, a_val = w & 0x3F, self.r[rd]
        elif op1 == 3:                                      # indirect
            a = self.ea_indirect(opn, rs)
            store_to = a
            src = self.rd(a) if op != 0xD else 0
            a_val = self.r[rd]
        elif op1 == 4:
            if opn == 0:
                src, a_val = self.r[rs], self.r[rd]
            elif opn == 1:                                  # rd = rs op imm16
                src, a_val = self.fetch(), self.r[rs]
            elif opn in (2, 3):                             # [A16]
                a = self.fetch()
                store_to = a
                src = self.rd(a) if opn == 2 else 0
                a_val = self.r[rs] if opn == 2 else self.r[rd]
                if opn == 3:
                    self.wr(a, self.r[rd] if op == 0xD else 0)
                    return
            else:
                raise Halt("forma op1=4 opn=%d non gestita %04x @%06x" % (opn, w, self.addr - 1))
        elif op1 in (5, 6):                                 # shift / rotate of rs
            src, a_val = self.shifted(opn, rs, op1 == 6), self.r[rd]
        else:
            raise Halt("forma non gestita %04x @%06x" % (w, self.addr - 1))

        if op == 0xD:                                       # store
            if op1 in (0, 3):
                self.wr(store_to, self.r[rd])
                return
            raise Halt("store non gestito %04x" % w)
        cin = 1 if self.flag(SR_C) else 0
        if op in (0, 1):
            res = a_val + src + (cin if op == 1 else 0)
            c = res > 0xFFFF
            s16 = (a_val ^ 0x8000) - 0x8000
            self._set(rd, op, res & 0xFFFF, c, s16 + ((src ^ 0x8000) - 0x8000) < 0)
        elif op in (2, 3, 4):
            res = a_val + ((~src) & 0xFFFF) + (1 if op != 3 else cin)
            c = res > 0xFFFF
            sgn = ((a_val ^ 0x8000) - 0x8000) - ((src ^ 0x8000) - 0x8000) < 0
            self._set(rd, op, res & 0xFFFF, c, sgn, write=(op != 4))
        elif op == 6:
            res = (-src) & 0xFFFF
            self._set(rd, op, res, None, None)
        elif op in (8, 9, 0xA, 0xB, 0xC):
            res = {8: a_val ^ src, 9: src, 0xA: a_val | src, 0xB: a_val & src, 0xC: a_val & src}[op]
            if op == 9 and rd == 6:
                self.r[6] = res
                return
            self._set(rd, op, res, None, None, write=(op != 0xC))
        else:
            raise Halt("op %x non gestito %04x" % (op, w))

    def _set(self, rd, op, res, c, s, write=True):
        if write:
            if rd == 7:
                self.pc = res
            else:
                self.r[rd] = res
        self.setf(res & 0x8000, res == 0, s, c)
