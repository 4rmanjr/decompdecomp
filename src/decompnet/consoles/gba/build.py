"""Template copied into exported GBA projects to rebuild their ROM."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
START = re.compile(r"^\s*(?:thumb|arm|non_word_aligned_thumb)_func_start\s+(\S+)")


def tool(name: str) -> str:
    from decompnet import toolchains

    folder = toolchains.ensure("arm-gnu", log=lambda *_: None)
    return str(folder / "bin" / f"arm-none-eabi-{name}")


def object_text_size(path: Path) -> int:
    from elftools.elf.elffile import ELFFile

    with path.open("rb") as fp:
        return sum(section["sh_size"] for section in ELFFile(fp).iter_sections()
                   if section.name in (".text", ".data", ".rodata"))


def disassemble(data: dict) -> list[dict]:
    from decompnet.consoles.gba.split import _luvdis

    source = ROOT / "asm" / "rom.s"
    source.parent.mkdir(parents=True, exist_ok=True)
    work = ROOT / "build" / "luvdis"
    work.mkdir(parents=True, exist_ok=True)
    text, _ = _luvdis(ROOT / data["image"], work, ROOT / "functions.cfg")
    boundary = text.find("@ End embedded Luvdis macros")
    if boundary < 0:
        raise ValueError("luvdis output has no embedded macro boundary")
    boundary = text.find("\n", boundary) + 1
    macros = text[:boundary].replace(".syntax unified\n", "").replace(".text\n", "")
    (ROOT / "asm" / "macros.inc").write_text(macros, encoding="utf-8")
    function_by_name = {f["name"]: f for f in data["functions"]}
    pieces, current, lines = [], data["base"], []

    def flush() -> None:
        if not any(line.strip() for line in lines):
            return
        label = next((m.group(1) for line in lines if (m := START.match(line))), None)
        item = function_by_name.get(label or "")
        name = item["name"] if item and item["addr"] == current else "rom_header" if current == data["base"] else f"_{current:08X}"
        path = ROOT / "asm" / f"{name}.s"
        path.write_text('.syntax unified\n.include "macros.inc"\n.text\n' + "\n".join(lines) + "\n", encoding="utf-8")
        pieces.append({"addr": current, "src": path.relative_to(ROOT).as_posix(), "name": name})

    for line in text[boundary:].splitlines():
        found = START.match(line)
        item = function_by_name.get(found.group(1)) if found else None
        if item and item["addr"] % 4 == 0 and item["addr"] != current:
            flush()
            current, lines = item["addr"], []
        lines.append(line)
    flush()
    (ROOT / "asm" / "pieces.json").write_text(json.dumps(pieces, indent=1), encoding="utf-8")
    return pieces


def build() -> bool:
    from decompnet.compiler import run_in
    from decompnet.project import compiler_config

    data = json.loads((ROOT / "build.json").read_text(encoding="utf-8"))
    image = ROOT / data["image"]
    if not image.exists():
        raise ValueError(f"put your own copy of the game here as {data['image']}")
    if hashlib.sha1(image.read_bytes()).hexdigest() != data["sha1"]:
        raise ValueError("the supplied ROM does not have the expected SHA-1")
    pieces = json.loads((ROOT / "asm" / "pieces.json").read_text(encoding="utf-8")) if (ROOT / "asm" / "pieces.json").exists() else disassemble(data)
    arm_as, arm_ld, arm_objcopy = tool("as"), tool("ld"), tool("objcopy")
    asm_objects = []
    for piece in pieces:
        obj = f"build/{Path(piece['src']).with_suffix('.o').as_posix()}"
        (ROOT / obj).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([arm_as, "-mcpu=arm7tdmi", "-I", "asm", "-o", obj, piece["src"]], cwd=ROOT, check=True)
        piece["obj"] = obj
        asm_objects.append((piece["addr"], obj))
    compiler = compiler_config(ROOT, {"compiler": data["compiler"]})
    c_objects = {}
    for function in data["functions"]:
        source = function.get("c") or f"src/{function['name']}.c"
        if not (ROOT / source).exists() or function["addr"] % 4:
            continue
        obj = f"build/{Path(source).with_suffix('.o').as_posix()}"
        (ROOT / obj).parent.mkdir(parents=True, exist_ok=True)
        argv = [arg.replace("{src}", source).replace("{obj}", obj).replace("{include}", "include") for arg in compiler.cmd]
        result = run_in(compiler, ROOT, argv, inputs=[source, "include"], outputs=[obj])
        if result.returncode:
            raise RuntimeError(f"compiling {source} failed:\n{(result.stdout + result.stderr)[-2000:]}")
        c_objects[function["name"]] = (function["addr"], obj)
    replaced = {addr for addr, _ in c_objects.values()}
    items = [(addr, obj) for addr, obj in asm_objects if addr not in replaced]
    items.extend(c_objects.values())
    rom = image.read_bytes()
    pos, end = data["base"], data["base"] + len(rom)
    layout = []
    for addr, obj in sorted(items) + [(end, "")]:
        if addr > pos:
            gap = ROOT / f"build/gaps/{pos:08X}.s"
            gap.parent.mkdir(parents=True, exist_ok=True)
            raw = rom[pos - data["base"]:addr - data["base"]]
            gap.write_text(".data\n" + "".join(".byte " + ", ".join(map(str, raw[i:i + 16])) + "\n"
                                                   for i in range(0, len(raw), 16)), encoding="utf-8")
            gap_obj = f"build/gaps/{pos:08X}.o"
            subprocess.run([arm_as, "-mcpu=arm7tdmi", "-o", gap_obj, gap.relative_to(ROOT).as_posix()], cwd=ROOT, check=True)
            layout.append((pos, gap_obj))
        if obj:
            layout.append((addr, obj))
            pos = max(pos, addr + object_text_size(ROOT / obj))
    layout.sort()
    lines = ["SECTIONS", "{", f"  .rom 0x{data['base']:08X} :", "  {"]
    lines.extend(f"    . = 0x{addr - data['base']:X}; {obj}(.text .data .rodata)" for addr, obj in layout)
    lines.extend(["  }", "  /DISCARD/ : { *(.comment) *(.note*) *(.ARM.attributes) }", "}"])
    (ROOT / "build" / "game.ld").write_text("\n".join(lines) + "\n", encoding="utf-8")
    objects = sorted({obj for _, obj in layout})
    subprocess.run([arm_ld, "-T", "build/game.ld", "-o", "build/game.elf", *objects], cwd=ROOT, check=True)
    out = ROOT / data["output"]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([arm_objcopy, "-O", "binary", "build/game.elf", data["output"]], cwd=ROOT, check=True)
    identical = out.read_bytes() == image.read_bytes()
    print(f"{'identical' if identical else 'differs'}: {out.name} compared with {image.name} ({len(c_objects)} matched C functions)")
    return identical


def main() -> int:
    try:
        return 0 if build() else 1
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
