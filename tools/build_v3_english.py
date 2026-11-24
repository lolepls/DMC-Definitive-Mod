#!/usr/bin/env python3
"""Build an English Digital Monster Color Ver.3 firmware from the original V3 dump.

The English text sprites are the PNG files in english/ (see english.py): every V3 sprite with
the same Japanese pixels and size as a translated one is replaced. Nothing else is touched.

Optionally applies a one-word code patch used as a checksum/integrity test:
meat gives +5 weight instead of +1 (V3 word 0x202db0, `r3 = 0x01` -> `r3 = 0x05`).

Usage:
  python3 build_v3_english.py V3_ORIG OUT.bin [--weight-test] [--skip-dump-check]
"""
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dumps  # noqa: E402
import english  # noqa: E402

# word 0x202db0 in V3 -> byte offset
WEIGHT_PATCH_OFF = (0x202DB0 - 0x200000) * 2
WEIGHT_PATCH_OLD = 0x9641   # r3 = 0x01
WEIGHT_PATCH_NEW = 0x9645   # r3 = 0x05


def build(v3_orig, weight_test, check=True):
    v3 = bytearray(dumps.check("V3", v3_orig) if check else open(v3_orig, "rb").read())
    log = english.apply(v3)

    if weight_test:
        cur = struct.unpack_from("<H", v3, WEIGHT_PATCH_OFF)[0]
        if cur != WEIGHT_PATCH_OLD:
            raise SystemExit("word inattesa a 0x%05x: 0x%04x" % (WEIGHT_PATCH_OFF, cur))
        struct.pack_into("<H", v3, WEIGHT_PATCH_OFF, WEIGHT_PATCH_NEW)
        log.append("codice: carne +5 peso (byte 0x%05x: 0x%04x -> 0x%04x)"
                   % (WEIGHT_PATCH_OFF, WEIGHT_PATCH_OLD, WEIGHT_PATCH_NEW))
    return bytes(v3), log


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    weight_test = "--weight-test" in sys.argv
    if len(args) != 2:
        raise SystemExit(__doc__)
    data, log = build(args[0], weight_test, dumps.SKIP_FLAG not in sys.argv)
    open(args[1], "wb").write(data)
    print("\n".join(log))
    print("scritto %s (%d byte), sha1 %s" % (args[1], len(data), hashlib.sha1(data).hexdigest()))


if __name__ == "__main__":
    main()
