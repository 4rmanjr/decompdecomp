"""Game Boy Advance: ARM7TDMI ROMs, agbcc and luvdis, ROM mode.

The GBA pipeline is separate from splat because luvdis understands the mixed ARM/Thumb startup code and emits the
assembly syntax used by pret-style projects. The public-fork facts for GBA live on this same console instance.
"""

from __future__ import annotations

from pathlib import Path

from ...rom import Picked
from .. import BETA, EXPORT, ROM, Compiler, Console


class GameBoyAdvance(Console):
    id = "gba"
    name = "Game Boy Advance"
    short = "GBA"
    status = BETA
    arch = "arm"
    big_endian = False
    mode = ROM
    layout = "luvdis"
    compilers = (Compiler("agbcc", "agbcc", "most Game Boy Advance games", guess="AGBCC"),)
    guides = ("arm",)
    m2c = "gba"
    ram = (0x02000000, 0x0A000000)
    own_features = frozenset({EXPORT})
    as_flags = ("-mcpu=arm7tdmi",)
    image_ext = ".gba"
    asm_preamble = '.syntax unified\n.include "macros.inc"\n.text\n'

    def sniff(self, head: bytes, file) -> bool:
        from .rom import is_gba

        return is_gba(head)

    def read_game(self, file: bytes | Path, name: str = "") -> Picked:
        from .rom import read_info

        data = Path(file).read_bytes() if isinstance(file, (str, Path)) else bytes(file)
        return Picked(self.id, data, "baserom.gba", read_info(data), False)

    def split_rom(self, workdir: Path, rom_name: str = "baserom.gba", picked=None, log=print, symbols=None):
        from ... import toolchains
        from .split import split

        for tool in ("luvdis", "arm-gnu"):
            toolchains.ensure(tool, log=log)
        return split(workdir / rom_name, workdir, log=log)

    def default_flags(self, toolchain: str, gp: int) -> list[str]:
        from ... import toolchains

        return list(toolchains.get(toolchain).flags)

    def export_rom(self, net, slug: str, out: Path, log=print) -> dict:
        from .export import export_gba

        return export_gba(net, slug, out, log=log)


GBA = GameBoyAdvance()
