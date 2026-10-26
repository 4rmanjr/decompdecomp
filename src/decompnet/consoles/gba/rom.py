"""Game Boy Advance cartridge header parsing."""

from __future__ import annotations

import hashlib
import struct

from ...rom import RomError

BASE = 0x08000000


def is_gba(data: bytes) -> bool:
    return (len(data) >= 0xC0 and data[0xB2] == 0x96 and
            struct.unpack_from("<I", data, 0)[0] & 0xFF000000 == 0xEA000000 and
            (-(sum(data[0xA0:0xBD]) + 0x19)) & 0xFF == data[0xBD])


def read_info(data: bytes) -> dict:
    if not is_gba(data):
        raise RomError("this isn't a Game Boy Advance ROM")
    title = data[0xA0:0xAC].split(b"\0", 1)[0].decode("ascii", "replace").strip()
    code = data[0xAC:0xB0].decode("ascii", "replace")
    region = {"E": "North America", "J": "Japan", "P": "Europe", "D": "Germany", "F": "France",
              "I": "Italy", "S": "Spain"}.get(code[3:4], "Unknown region")
    return {"title": title or code, "game_code": code, "region": region, "version": data[0xBC], "size": len(data),
            "sha1": hashlib.sha1(data).hexdigest(), "entrypoint": BASE, "platform": "gba", "byte_order": "gba",
            "compiler_guess": "AGBCC", "gp": 0}
