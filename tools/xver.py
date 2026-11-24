"""Cross-version helpers: load dumps, match functions between versions by instruction skeleton."""
import bisect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unsp_dis import Dump, full_analysis  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = {
    "V1": "DUMPS/Ver1/dmc_ver1_original_fw.bin",
    "V2": "DUMPS/Ver2/Dump/ver2_original_dump.bin",
    "V3": "DUMPS/Ver3/original_dump.bin",
    "V4": "DUMPS/Ver4/original_dump.bin",
    "V5": "DUMPS/Ver5/original_dump.bin",
}
_cache = {}


def load(v):
    if v not in _cache:
        d = Dump(os.path.join(ROOT, P[v]))
        ins, funcs, _ = full_analysis(d)
        _cache[v] = (d, ins, sorted(funcs))
    return _cache[v]


def func_of(v, a):
    d, ins, fs = load(v)
    return fs[bisect.bisect_right(fs, a) - 1]


def func_end(v, f):
    d, ins, fs = load(v)
    j = bisect.bisect_left(fs, f)
    return fs[j + 1] if j + 1 < len(fs) else f + 0x400


def skel(v, f):
    d, ins, fs = load(v)
    e = func_end(v, f)
    sk, a = [], f
    while a < e and a in ins:
        i = ins[a]
        sk.append(d.read(a) >> 6 if i.kind != "call" else 0xF04)
        a += i.size
    return tuple(sk)


def match(src_f, v, src="V1"):
    s = skel(src, src_f)
    d, ins, fs = load(v)
    return [f for f in fs if skel(v, f) == s]


def callers(v, t):
    d, ins, fs = load(v)
    return sorted(set(func_of(v, a) for a, i in ins.items() if i.kind == "call" and i.target == t))


def refs_imm(v, value):
    d, ins, fs = load(v)
    return sorted(a for a, i in ins.items() if i.imm == value or i.mem_ref == value)
