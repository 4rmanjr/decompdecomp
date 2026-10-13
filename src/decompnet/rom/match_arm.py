"""Link a compiled ARM/Thumb function at its address in the ROM and compare it with the original, byte for byte.

Like match.py for MIPS, but without relocations from a disassembler: for each relocation in the candidate, the
original's bytes at the same place say what it refers to (a Thumb `bl` encodes its destination, a literal-pool word
holds an address). The candidate must refer to the same thing: a function or variable by name (sub_08001234,
gUnknown_03001234 or a name given with rename_symbol), or data it defines itself, which is then checked against
the ROM's copy where that's in ROM. Everything else must be identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

from ..elfobj import FunctionObj, Reloc, _function_symbols
from .match import LinkResult
from .symbols import SymbolTable

R_ARM_PC24, R_ARM_ABS32, R_ARM_THM_CALL, R_ARM_CALL, R_ARM_JUMP24, R_ARM_V4BX = 1, 2, 10, 28, 29, 40


@dataclass
class ArmTarget:
    name: str
    vram: int
    code: bytes
    thumb: bool = True


def thumb_bl_dest(code: bytes, at: int, pc: int) -> int | None:
    """Destination of a Thumb `bl` pair at code[at:at+4] located at address pc."""
    h1 = int.from_bytes(code[at:at + 2], "little")
    h2 = int.from_bytes(code[at + 2:at + 4], "little")
    if (h1 & 0xF800) != 0xF000 or (h2 & 0xE800) != 0xE800:
        return None
    hi = h1 & 0x7FF
    if hi & 0x400:
        hi -= 0x800
    return (pc + 4 + (hi << 12) + ((h2 & 0x7FF) << 1)) & 0xFFFFFFFF


def arm_b_dest(code: bytes, at: int, pc: int) -> int | None:
    word = int.from_bytes(code[at:at + 4], "little")
    if (word >> 25) & 7 != 5:
        return None
    imm = word & 0xFFFFFF
    if imm & 0x800000:
        imm -= 0x1000000
    return (pc + 8 + (imm << 2)) & 0xFFFFFFFF


def _sym_name(elf: ELFFile, symtab, idx: int):
    sym = symtab.get_symbol(idx)
    if sym["st_info"]["type"] == "STT_SECTION":
        return elf.get_section(sym["st_shndx"]).name, sym, True
    return sym.name, sym, False


def link(obj: bytes, name: str, target: ArmTarget, symbols: SymbolTable, read) -> LinkResult:
    elf = ELFFile(BytesIO(obj))
    found = _function_symbols(elf).get(name)
    if found is None:
        return LinkResult(None, False, [f"the object has no function named `{name}`"])
    shndx, start, size = found
    start &= ~1  # Thumb functions have the low bit set
    section = elf.get_section(shndx)
    code = bytearray(section.data()[start:start + size])
    problems: list[str] = []
    placements: dict[str, dict[int, int]] = {}
    relocs: list[Reloc] = []
    tcode = target.code

    def problem(text: str) -> None:
        if text not in problems:
            problems.append(text)

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
            sname, sym, is_section = _sym_name(elf, symtab, rel["r_info_sym"])
            relocs.append(Reloc(at, rtype, sname, 0))
            pc = target.vram + at
            if at + 4 > len(tcode):
                problem("your function is longer than the original")
                continue
            if rtype == R_ARM_ABS32:
                addend = int.from_bytes(code[at:at + 4], "little")
                original = int.from_bytes(tcode[at:at + 4], "little")
                if sym["st_shndx"] == "SHN_UNDEF" or (not is_section and sym["st_shndx"] != shndx):
                    base = symbols.resolve(sname)
                    if base is None:
                        problem(f"`{sname}` is not a known symbol. Use the name the assembly shows (like sub_08001234 "
                                f"or gUnknown_03001234), or give that address this name first with rename_symbol.")
                        continue
                    if (base + addend) & 0xFFFFFFFF != original:
                        problem(f"a pointer to {sname}{f'+0x{addend:X}' if addend else ''} where the original has "
                                f"0x{original:08X} ({symbols.label(original)})")
                    code[at:at + 4] = tcode[at:at + 4]
                elif sym["st_shndx"] == shndx:  # a pointer into this function (a switch's jump table)
                    inner = (0 if is_section else (sym["st_value"] & ~1)) + addend - start
                    if (target.vram + inner) & 0xFFFFFFFF != original & ~1 and (target.vram + inner) != original:
                        problem(f"a pointer to your function +0x{inner:X} where the original points at 0x{original:08X}")
                    code[at:at + 4] = tcode[at:at + 4]
                else:  # data this function defines itself (a string, a table): the original's copy is at `original`
                    sec = elf.get_section(sym["st_shndx"])
                    offset = addend + (0 if is_section else sym["st_value"])
                    prev = placements.setdefault(sec.name, {}).setdefault(offset, original)
                    if prev != original:
                        problem(f"{sec.name}+0x{offset:X} is used for two different addresses in the original")
                    if sec["sh_type"] != "SHT_NOBITS" and 0x08000000 <= original < 0x0A000000:
                        mine = sec.data()[offset:offset + 64]
                        rom = read(original, len(mine)) if mine else b""
                        if rom is not None and mine and rom[:len(mine)] != mine:
                            problem(f"your {sec.name} data at +0x{offset:X} differs from the ROM's at 0x{original:08X}")
                    code[at:at + 4] = tcode[at:at + 4]
            elif rtype in (R_ARM_THM_CALL,):
                dest = thumb_bl_dest(tcode, at, pc)
                base = symbols.resolve(sname)
                if base is None:
                    problem(f"`{sname}` is not a known function. Call it by the name the assembly shows (like "
                            f"sub_08001234), or give that address this name first with rename_symbol.")
                    continue
                if dest is None or (dest & ~1) != (base & ~1):
                    problem(f"you call {sname} where the original calls "
                            f"{symbols.label(dest) if dest is not None else 'something else'}")
                    continue
                code[at:at + 4] = tcode[at:at + 4]
            elif rtype in (R_ARM_PC24, R_ARM_CALL, R_ARM_JUMP24):
                dest = arm_b_dest(tcode, at, pc)
                base = symbols.resolve(sname)
                if base is None or dest is None or (dest & ~1) != (base & ~1):
                    problem(f"you branch to {sname} where the original goes to "
                            f"{symbols.label(dest) if dest is not None else 'something else'}")
                    continue
                code[at:at + 4] = tcode[at:at + 4]
            else:
                problem(f"relocation type {rtype} isn't supported yet")

    linked = bytes(code)
    exact = not problems and linked == tcode[:len(linked)] and len(linked) == len(tcode)
    return LinkResult(FunctionObj(name, "thumb" if target.thumb else "arm", False, linked, []), exact, problems,
                      placements)
