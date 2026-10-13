"""Work out what the user picked: an N64 ROM, a PlayStation executable, or a PlayStation disc image."""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import n64, psx
from .n64 import RomError

PLATFORMS = {"n64": "Nintendo 64", "psx": "PlayStation"}


@dataclass
class Picked:
    platform: str  # n64 | psx
    data: bytes  # what we split: a z64 ROM or a PS-X EXE
    filename: str  # name to store it under in the project folder
    info: dict
    big_endian: bool


def identify(data: bytes) -> Picked:
    if data[:4] in n64.MAGIC:
        rom, info = n64.read_info(data)
        return Picked("n64", rom, "rom.z64", info.to_dict() | {"platform": "n64", "gp": 0}, True)
    if (data[:8] == b"PS-X EXE" or psx.is_disc(data)) and not os.environ.get("DECOMP_EXPERIMENTAL"):
        raise RomError("PlayStation support is coming soon; for now decomp.wtf works with N64 ROMs")
    if data[:8] == b"PS-X EXE":
        exe, info = psx.read_exe(data)
        return Picked("psx", exe, "game.exe", info, False)
    if psx.is_disc(data):
        raw, boot = psx.from_disc(data)
        exe, info = psx.read_exe(raw, boot)
        return Picked("psx", exe, "game.exe", info, False)
    if data[:4] == b"\x7fELF":
        raise RomError("this is an ELF program (PlayStation 2?). PS2 support is coming; for now: N64 and PlayStation")
    raise RomError("this isn't a ROM or game program this app can read: pick an N64 ROM (.z64, .n64, .v64), "
                   "a PlayStation disc image (.bin, .iso) or its program file (e.g. SLUS_000.01)")
