"""Link ARM and Thumb candidates against ROM bytes."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

from ..elfobj import FunctionObj, Reloc, _function_symbols
from .match import LinkResult

R_ARM_ABS32, R_ARM_THM_CALL, R_ARM_CALL, R_ARM_JUMP24, R_ARM_V4BX = 2, 10, 28, 29, 40


@dataclass
class ArmTarget:
    name: str
    vram: int
    code: bytes
    thumb: bool = True


def thumb_bl_dest(code: bytes, at: int, pc: int) -> int | None:
    h1 = int.from_bytes(code[at:at + 2], "little")
    h2 = int.from_bytes(code[at + 2:at + 4], "little")
    if h1 & 0xF800 != 0xF000 or h2 & 0xE800 != 0xE800:
        return None
    high = h1 & 0x7FF
    if high & 0x400:
        high -= 0x800
    return (pc + 4 + (high << 12) + ((h2 & 0x7FF) << 1)) & 0xFFFFFFFF


def arm_b_dest(code: bytes, at: int, pc: int) -> int | None:
    word = int.from_bytes(code[at:at + 4], "little")
    if (word >> 25) & 7 != 5:
        return None
    offset = word & 0xFFFFFF
    if offset & 0x800000:
        offset -= 0x1000000
    return (pc + 8 + (offset << 2)) & 0xFFFFFFFF


def link(obj: bytes, name: str, target: ArmTarget, symbols, read) -> LinkResult:
    elf = ELFFile(BytesIO(obj))
    found = _function_symbols(elf).get(name)
    if found is None:
        return LinkResult(None, False, [f"the object has no function named `{name}`"])
    shndx, start, size = found
    # ARM ELF symbols use bit 0 to mark Thumb. Some assemblers also report a one-byte loose symbol size.
    symbol_start = start
    start &= ~1
    section = elf.get_section(shndx)
    if target.thumb and (symbol_start & 1):
        size = max(size, section["sh_size"] - start)
    code = bytearray(section.data()[start:start + size])
    original = target.code
    problems = []
    relocs = []
    for rsec in elf.iter_sections():
        if not isinstance(rsec, RelocationSection) or rsec["sh_info"] != shndx:
            continue
        symtab = elf.get_section(rsec["sh_link"])
        for rel in rsec.iter_relocations():
            at = rel["r_offset"] - start
            if not 0 <= at < len(code):
                continue
            rtype = rel["r_info_type"]
            if rtype == R_ARM_V4BX:
                continue
            symbol = symtab.get_symbol(rel["r_info_sym"])
            symbol_name = symbol.name
            relocs.append(Reloc(at, rtype, symbol_name, 0))
            if at + 4 > len(original):
                problems.append("your function is longer than the original")
                continue
            if rtype == R_ARM_THM_CALL:
                dest = thumb_bl_dest(original, at, target.vram + at)
                base = symbols.resolve(symbol_name)
                if base is None or dest is None or (dest & ~1) != (base & ~1):
                    problems.append(f"the original call does not target `{symbol_name}`")
                else:
                    code[at:at + 4] = original[at:at + 4]
            elif rtype in (R_ARM_CALL, R_ARM_JUMP24):
                dest = arm_b_dest(original, at, target.vram + at)
                base = symbols.resolve(symbol_name)
                if base is None or dest is None or (dest & ~1) != (base & ~1):
                    problems.append(f"the original branch does not target `{symbol_name}`")
                else:
                    code[at:at + 4] = original[at:at + 4]
            elif rtype == R_ARM_ABS32:
                base = symbols.resolve(symbol_name)
                value = int.from_bytes(original[at:at + 4], "little")
                if base is None or (base & ~1) != (value & ~1):
                    problems.append(f"the original pointer does not target `{symbol_name}`")
                else:
                    code[at:at + 4] = original[at:at + 4]
            else:
                problems.append(f"relocation type {rtype} isn't supported yet")
    linked = bytes(code)
    exact = not problems and linked == original and len(linked) == len(original)
    return LinkResult(FunctionObj(name, "arm", False, linked, relocs), exact, problems)
