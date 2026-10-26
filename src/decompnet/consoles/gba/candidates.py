"""Recover GBA function starts and literal pools that luvdis cannot reach."""

from __future__ import annotations

import bisect
import re

import capstone

BASE = 0x08000000


def _md() -> capstone.Cs:
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    md.skipdata = True
    return md


def _return(ins) -> bool:
    return (ins.mnemonic == "pop" and "pc" in ins.op_str) or ins.mnemonic == "bx"


def _branch_target(ins) -> int | None:
    if ins.mnemonic.startswith("b") and ins.mnemonic not in ("bl", "blx", "bx", "bic", "bics"):
        match = re.match(r"#(0x[0-9a-f]+)", ins.op_str)
        return int(match.group(1), 16) if match else None
    return None


def plausible_function(rom: bytes, off: int, limit: int = 0x800) -> bool:
    md = _md()
    pos, end = off, min(len(rom), off + limit)
    while pos < end:
        ins = next(md.disasm(rom[pos:pos + 4], BASE + pos, 1), None)
        if ins is None:
            return False
        if _return(ins):
            return True
        pos += ins.size
    return False


def literal_words(rom: bytes, known: list[int]) -> set[int]:
    del known
    out = set()
    for ins in _md().disasm(rom[0xC0:], BASE + 0xC0):
        if ins.mnemonic.startswith("ldr") and "[pc" in ins.op_str:
            match = re.search(r"#(0x[0-9a-f]+|\d+)", ins.op_str)
            at = ((ins.address + 4) & ~3) + (int(match.group(1), 0) if match else 0) - BASE
            out.update((at, at + 2))
    return out


def bodies(rom: bytes, known: list[int], literals: set[int] | None = None):
    md = _md()
    literals = literal_words(rom, known) if literals is None else literals
    starts = sorted({(a & ~1) - BASE for a in known})
    for k, start in enumerate(starts):
        limit = min(len(rom), starts[k + 1] if k + 1 < len(starts) else start + 0x2000)
        body, furthest, pos = [], start, start
        while pos < limit:
            if pos in literals:
                pos += 2
                continue
            ins = next(md.disasm(rom[pos:pos + 4], BASE + pos, 1), None)
            if ins is None:
                break
            body.append(ins)
            pos += ins.size
            if (target := _branch_target(ins)) is not None:
                furthest = max(furthest, target - BASE)
            if _return(ins) and furthest <= ins.address - BASE:
                break
        end = body[-1].address + body[-1].size - BASE if body else start
        yield start, max(end, furthest), body


def candidates(rom: bytes, known: list[int]) -> list[int]:
    known_set = {a & ~1 for a in known}
    literals = literal_words(rom, known)
    reach = {start: end for start, end, _ in bodies(rom, known, literals)}
    starts = sorted(reach)

    def inside_known(off: int) -> bool:
        k = bisect.bisect_right(starts, off) - 1
        return k >= 0 and reach[starts[k]] > off

    out = set()
    for off in range(0xC0, len(rom) - 2, 2):
        if BASE + off in known_set or off in literals or inside_known(off):
            continue
        half = int.from_bytes(rom[off:off + 2], "little")
        if half in (0, 0x46C0):
            continue
        prev = int.from_bytes(rom[off - 2:off], "little")
        after_return = (prev & 0xFF87) == 0x4700 or (prev & 0xFF00) == 0xBD00
        after_padding = prev in (0, 0x46C0) and off >= 4 and (
            (int.from_bytes(rom[off - 4:off - 2], "little") & 0xFF87) == 0x4700 or rom[off - 1] == 0xBD)
        prologue = (half & 0xFF00) == 0xB500
        if (prologue and (after_return or after_padding or prev == 0)) or after_return or after_padding:
            if plausible_function(rom, off):
                out.add(BASE + off)
    for _, _, body in bodies(rom, known, literals):
        for ins in body:
            if ins.mnemonic == "bl":
                match = re.match(r"#(0x[0-9a-f]+)", ins.op_str)
                target = int(match.group(1), 16) - BASE if match else -1
                if (0xC0 <= target < len(rom) and BASE + target not in known_set and target not in literals
                        and plausible_function(rom, target)):
                    out.add(BASE + target)
    for off in range(0, len(rom) - 4, 4):
        word = int.from_bytes(rom[off:off + 4], "little")
        target = (word & ~1) - BASE
        if (word & 1 and 0xC0 <= target < len(rom) and BASE + target not in known_set and target not in literals
                and not inside_known(target) and plausible_function(rom, target)):
            out.add(BASE + target)
    return sorted(out)
