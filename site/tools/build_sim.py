"""Data for the homepage simulation: every function of the bundled demo ROM (Demo Quest 64) with its real
assembly, as decomp.wtf shows it, and C that compiles back to exactly those bytes (the demo's own CC0 source and
zlib 1.1.4, proven by tests/test_rom.py).

    uv run python site/tools/build_sim.py        ->  site/public/data/sim.json
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from elftools.elf.elffile import ELFFile

from decompnet.rom import n64, split

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "projects" / "demo-rom"
SOURCES = [DEMO / "src" / "main.c", DEMO / "src" / "libc.c", ROOT / "projects" / "demo-n64" / "original" / "game.c",
           *sorted((ROOT / "projects" / "zlib-n64" / "original").glob("*.c"))]
OUT = ROOT / "site" / "public" / "data" / "sim.json"


def elf_functions() -> dict[int, str]:
    elf = ELFFile(BytesIO((DEMO / "build" / "demo-quest-64.elf").read_bytes()))
    return {s["st_value"]: s.name for s in elf.get_section_by_name(".symtab").iter_symbols()
            if s["st_info"]["type"] == "STT_FUNC" and s.name and not s.name.endswith(".")}


def source_of(name: str) -> str | None:
    """The definition of `name` from the demo's sources (ANSI or K&R style)."""
    for path in SOURCES:
        lines = path.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            if not re.match(rf"^(?:[\w\s\*]+\s\*?)?{re.escape(name)}\s*\(", line) or line.rstrip().endswith(";"):
                continue
            start = i - 1 if i and re.match(r"^[A-Za-z_][\w\s\*]*$", lines[i - 1]) and not lines[i - 1].endswith(";") else i
            depth, seen, out = 0, False, []
            for j in range(start, len(lines)):
                out.append(lines[j])
                depth += lines[j].count("{") - lines[j].count("}")
                seen |= "{" in lines[j]
                if seen and depth == 0:
                    text = "\n".join(out).strip()
                    return re.sub(r"\bZEXPORT\s+|\bZEXPORTVA\s+", "", text)
    return None


def asm_lines(fn: split.RomFunction) -> str:
    out = []
    for ln in fn.lines:
        if ln.vram in fn.labels and ln.vram != fn.vram:
            out.append(f".L{ln.vram:08X}:")
        out.append(f"{ln.vram:08X}  {ln.mnemonic:<7} {ln.operands}".rstrip())
    return "\n".join(out)


def main() -> int:
    rom, info = n64.read_info((DEMO / "build" / "demo-quest-64.z64").read_bytes())
    real = elf_functions()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "rom.z64").write_bytes(rom)
        res = split.split_rom(Path(tmp), log=lambda *_: None)
    starts = sorted(real)
    functions = []
    for fn in res.functions:  # split what the disassembler merged, like an agent would
        for at in [a for a in starts if fn.vram < a < fn.vram + fn.size]:
            fn, tail = split.split_function(fn, at, f"func_{at:08X}")
            functions.append(fn)
            fn = tail
        functions.append(fn)
    out = []
    for fn in functions:
        name = real.get(fn.vram)
        c = source_of(name) if name else None
        out.append({"auto": f"func_{fn.vram:08X}", "name": name or f"func_{fn.vram:08X}", "vram": fn.vram,
                    "insns": len(fn.lines), "asm": asm_lines(fn), "c": c})
    data = {"title": "Demo Quest 64", "game_code": info.game_code, "compiler": "KMC GCC 2.7.2",
            "functions": [f for f in out if f["c"]]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    missing = [f["name"] for f in out if not f["c"]]
    print(f"wrote {OUT.relative_to(ROOT)}: {len(data['functions'])} functions, {OUT.stat().st_size // 1024} KiB"
          + (f"; no source found for {missing}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
