"""Split GBA ROM code with luvdis and recover uncalled Thumb functions."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ... import toolchains
from ...limits import capped
from ...rom import RomError
from ...rom.split import Segment, SplitResult
from . import candidates
from .rom import BASE


@dataclass
class GbaFunction:
    name: str
    vram: int
    rom: int
    code: bytes
    asm: str
    n_insns: int
    thumb: bool = True
    refs: list[tuple[int, int, int]] | None = None
    asm_only: str | None = None

    @property
    def size(self) -> int:
        return len(self.code)

    @property
    def lines(self) -> list:
        return []


def _luvdis(rom: Path, workdir: Path, cfg_in: Path | None) -> tuple[str, list[int]]:
    python = toolchains.venv_bin(toolchains.ensure("luvdis", log=lambda *_: None) / "venv", "python")
    rom = rom.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    out, cfg = workdir / "rom.s", workdir / "functions.cfg"
    args = [str(python), "-I", str(Path(__file__).with_name("luvdis_run.py")), "disasm", str(rom), "--min-calls", "1",
            "--min-length", "2", "-o", str(out), "-co", str(cfg)]
    if cfg_in is not None:
        args[5:5] = ["-c", str(cfg_in)]
    try:
            result = subprocess.run(capped(args), cwd=workdir, capture_output=True, text=True, timeout=3600,
                                encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise RomError("luvdis (the GBA disassembler) took more than an hour on this ROM and was stopped") from None
    if result.returncode != 0 or not out.exists():
        raise RomError("luvdis couldn't disassemble the ROM: " + (result.stderr or result.stdout)[-400:])
    found = sorted({int(m.group(1), 16) for m in re.finditer(r"^(?:[a-z_]+[ \t]+)?(0x[0-9A-Fa-f]+)",
                                                               cfg.read_text(encoding="utf-8"), re.M)})
    return out.read_text(encoding="utf-8"), found


def find_functions(rom_path: Path, workdir: Path, rounds: int = 4, log=lambda *_: None,
                   budget: float = 1800) -> tuple[str, list[int]]:
    rom = rom_path.read_bytes()
    started = time.time()
    asm, found = _luvdis(rom_path, workdir, None)
    log(f"luvdis followed the code from the start: {len(found)} functions ({time.time() - started:.0f}s)")
    for round_no in range(rounds):
        if time.time() - started > budget:
            log("stopping the search for more here: it has run half an hour")
            break
        extra = [addr for addr in candidates.candidates(rom, found) if addr not in found]
        if not extra:
            break
        log(f"round {round_no + 2}: {len(extra)} more function starts in code nothing calls directly")
        cfg = workdir / "candidates.cfg"
        cfg.write_text("".join(f"thumb_func 0x{addr:X}\n" for addr in sorted(set(found) | set(extra))), encoding="utf-8")
        asm, found_again = _luvdis(rom_path, workdir, cfg)
        if set(found_again) == set(found):
            break
        found = found_again
    return asm, found


def call_via(code: bytes) -> str | None:
    if len(code) in (2, 4) and code[1] == 0x47 and code[0] & 0x87 == 0 and code[2:] in (b"", b"\xc0\x46", b"\0\0"):
        reg = (code[0] >> 3) & 0xF
        if reg < 13:
            return f"_call_via_r{reg}"
    return None


def _asm_blocks(asm: str) -> dict[int, tuple[str, bool]]:
    blocks, current, lines, thumb = {}, None, [], True
    for line in asm.splitlines():
        match = re.match(r"\s*(thumb|arm|non_word_aligned_thumb)_func_start\s+\w*?([0-9A-F]{7,8})\b", line)
        if match:
            if current is not None:
                blocks[current] = ("\n".join(lines).rstrip() + "\n", thumb)
            current, lines, thumb = int(match.group(2), 16), [line], match.group(1) != "arm"
        elif current is not None:
            lines.append(line)
    if current is not None:
        blocks[current] = ("\n".join(lines).rstrip() + "\n", thumb)
    return blocks


def _thumb_bl_dest(code: bytes, at: int, pc: int) -> int | None:
    h1 = int.from_bytes(code[at:at + 2], "little")
    h2 = int.from_bytes(code[at + 2:at + 4], "little")
    if h1 & 0xF800 != 0xF000 or h2 & 0xE800 != 0xE800:
        return None
    high = h1 & 0x7FF
    if high & 0x400:
        high -= 0x800
    return (pc + 4 + (high << 12) + ((h2 & 0x7FF) << 1)) & 0xFFFFFFFF


def _refs(code: bytes, addr: int, literals: set[int], off: int) -> list[tuple[int, int, int]]:
    refs = []
    at = 0
    while at + 4 <= len(code):
        if off + at in literals and at % 4 == 0:
            value = int.from_bytes(code[at:at + 4], "little")
            if 0x02000000 <= value < 0x0A000000:
                refs.append((at, 2, value & ~1 if value & 1 and value >= BASE else value))
            at += 4
            continue
        if (dest := _thumb_bl_dest(code, at, addr + at)) is not None:
            refs.append((at, 10, dest & ~1))
            at += 4
            continue
        at += 2
    return refs


def split(rom_path: Path, workdir: Path, log=print) -> SplitResult:
    rom = rom_path.read_bytes()
    log("finding the functions (luvdis, then a pass for code nothing calls)" +
        (f": a {len(rom) >> 20} MiB ROM takes several minutes" if len(rom) > 8 << 20 else ""))
    asm, found = find_functions(rom_path, workdir, log=log)
    blocks = _asm_blocks(asm)
    starts = sorted({addr & ~1 for addr in found if BASE <= (addr & ~1) < BASE + len(rom)})
    literals = candidates.literal_words(rom, starts)
    ends = {start + BASE: end for start, end, _ in candidates.bodies(rom, starts, literals)}
    functions = []
    for index, addr in enumerate(starts):
        off = addr - BASE
        limit = starts[index + 1] - BASE if index + 1 < len(starts) else len(rom)
        end = min(ends.get(addr, limit), limit)
        while end + 4 <= limit and (end in literals or (end % 4 == 2 and end + 2 in literals and
                                                         rom[end:end + 2] in (b"\0\0", b"\xc0\x46"))):
            end += 2 if end % 4 == 2 and end not in literals else 4
        text, thumb = blocks.get(addr, (f"\tthumb_func_start sub_{addr:08X}\n", True))
        code = rom[off:end]
        functions.append(GbaFunction(f"sub_{addr:08X}", addr, off, code, text, max(1, len(code) // (2 if thumb else 4)),
                                     thumb, _refs(code, addr, literals, off), call_via(code)))
    log(f"found {len(functions)} functions")
    return SplitResult(functions, [], [Segment("rom", 0, len(rom), BASE)], {}, workdir / "functions.cfg", "AGBCC",
                       "", "gba", 0, rom_path.name)
