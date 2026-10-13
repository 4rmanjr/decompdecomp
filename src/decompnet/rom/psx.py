"""PlayStation executables (PS-X EXE) and the CD images they come on.

A PlayStation game's code is one executable on the disc (named in SYSTEM.CNF, e.g. SLUS_000.01). Discs are ISO 9660,
stored either as 2048-byte sectors (.iso) or raw 2352-byte sectors (.bin, Mode 1 or Mode 2 Form 1). We read only the
file system and the executable, from the user's own image; nothing here touches copy protection.
"""

from __future__ import annotations

import hashlib
import re
import struct

from .n64 import RomError

REGIONS = {"North America": "North America", "Japan": "Japan", "Europe": "Europe"}


def read_exe(data: bytes, boot_name: str = "") -> tuple[bytes, dict]:
    if data[:8] != b"PS-X EXE":
        raise RomError("this isn't a PlayStation executable")
    pc, gp, load, size = struct.unpack_from("<IIII", data, 0x10)
    if not (0x80000000 <= load < 0x80200000) or size == 0 or 0x800 + size > len(data):
        raise RomError("the executable's header doesn't make sense (load address or size)")
    marker = data[0x4C:0x800].split(b"\0", 1)[0].decode("ascii", "replace")
    region = next((v for k, v in REGIONS.items() if k in marker), "Unknown region")
    code = ""
    m = re.match(r"^([A-Z]{4})[_-](\d{3})\.(\d{2})", boot_name.upper())
    if m:
        code = f"{m.group(1)}-{m.group(2)}{m.group(3)}"
    exe = data[:0x800 + size]
    return exe, {
        "title": code or boot_name or "PlayStation game", "game_code": code, "region": region, "version": 0,
        "entrypoint": pc, "gp": gp, "load": load, "crc1": 0, "crc2": 0, "size": len(exe),
        "sha1": hashlib.sha1(exe).hexdigest(), "byte_order": "exe", "compiler_guess": "PSYQ", "platform": "psx",
        "boot": boot_name,
    }


class _Disc:
    def __init__(self, data: bytes):
        self.data = data
        for size, skip in ((2048, 0), (2352, 24), (2352, 16)):
            pvd = 16 * size + skip
            if data[pvd + 1:pvd + 6] == b"CD001":
                self.size, self.skip = size, skip
                return
        raise RomError("not a CD image this app can read (expected an ISO 9660 .bin or .iso)")

    def sector(self, lba: int) -> bytes:
        at = lba * self.size + self.skip
        return self.data[at:at + 2048]

    def read(self, lba: int, length: int) -> bytes:
        out = bytearray()
        while len(out) < length:
            out += self.sector(lba)
            lba += 1
        return bytes(out[:length])

    def entries(self, lba: int, length: int):
        raw = self.read(lba, length)
        i = 0
        while i < len(raw):
            n = raw[i]
            if n == 0:  # records don't cross sector boundaries; skip the padding
                i = (i // 2048 + 1) * 2048
                continue
            rec = raw[i:i + n]
            name = rec[33:33 + rec[32]].decode("ascii", "replace").split(";", 1)[0]
            if name not in ("\x00", "\x01"):
                yield name.upper(), struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0], bool(rec[25] & 2)
            i += n

    def find(self, path: str) -> bytes | None:
        pvd = self.sector(16)
        lba, length = struct.unpack_from("<I", pvd, 156 + 2)[0], struct.unpack_from("<I", pvd, 156 + 10)[0]
        parts = [p for p in re.split(r"[\\/]", path.upper()) if p]
        for k, part in enumerate(parts):
            hit = next(((l, s, d) for name, l, s, d in self.entries(lba, length) if name == part), None)
            if hit is None:
                return None
            lba, length, is_dir = hit
            if k == len(parts) - 1:
                return None if is_dir else self.read(lba, length)
        return None


def from_disc(data: bytes) -> tuple[bytes, str]:
    """(executable bytes, its file name) from a PlayStation CD image."""
    disc = _Disc(data)
    cnf = disc.find("SYSTEM.CNF")
    boot = "PSX.EXE"
    if cnf is not None:
        m = re.search(rb"BOOT\s*=\s*cdrom:?\\*([^\s;]+)", cnf, re.I)
        if m:
            boot = m.group(1).decode("ascii", "replace")
    exe = disc.find(boot)
    if exe is None:
        raise RomError(f"the disc names {boot} as its program, but it isn't on the disc")
    return exe, boot.replace("\\", "/").rsplit("/", 1)[-1]


def is_disc(data: bytes) -> bool:
    try:
        _Disc(data)
        return True
    except RomError:
        return False
