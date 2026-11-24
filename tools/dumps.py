"""Check that the dumps given to the build scripts are the expected original firmwares.

Only the firmware part is hashed: the area from 0x7FD000 holds each device's save data.
"""
import hashlib

SAVE_START = 0x7FD000
SIZE = 0x800000
FIRMWARE_SHA1 = {
    "V1": "606e1c0fead64b080456d9af882d9e731168ed9f",
    "V2": "1416fa91b8b5f26b0f51d227cf07fa994134050e",
    "V3": "be618c6efa0f5a5f8196c660cdd8af4cb2c93b5d",
    "V4": "8a42d27c903b185803c10947d6f7b2030d11e009",
    "V5": "559bde8834eac9dcaa45c13b0c0748b58695515e",
}
SKIP_FLAG = "--skip-dump-check"


def check(name, path, data=None):
    data = open(path, "rb").read() if data is None else data
    if len(data) != SIZE:
        raise SystemExit("%s (%s): %d byte, attesi %d (dump completo della flash da 8 MB)"
                         % (name, path, len(data), SIZE))
    got = hashlib.sha1(data[:SAVE_START]).hexdigest()
    if got != FIRMWARE_SHA1[name]:
        raise SystemExit("%s (%s): firmware diverso dall'originale atteso (sha1 %s, atteso %s).\n"
                         "Serve il dump originale non modificato di Digital Monster Color %s.\n"
                         "Per provare comunque: %s" % (name, path, got, FIRMWARE_SHA1[name], name, SKIP_FLAG))
    return data
