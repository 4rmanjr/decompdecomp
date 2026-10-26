"""Build the small open-source GBA demo ROM when agbcc and arm-gnu are installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from decompnet import toolchains

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"


def run(args: list[str], cwd: Path) -> None:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(result.stdout + result.stderr)


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir()
    agbcc = toolchains.ensure("agbcc")
    arm = toolchains.ensure("arm-gnu") / "bin"
    src, obj, asm = ROOT / "game.c", BUILD / "game.o", BUILD / "game.s"
    crt = BUILD / "crt0.o"
    preprocessed = BUILD / "game.i"
    run([str(arm / "arm-none-eabi-cpp"), "-nostdinc", "-undef", "-I", str(agbcc / "include"),
         str(src), "-o", str(preprocessed)], BUILD)
    run([str(agbcc / "bin" / "agbcc"), "-mthumb-interwork", "-O2", "-fhex-asm", str(preprocessed), "-o", str(asm)], BUILD)
    run([str(arm / "arm-none-eabi-as"), "-mcpu=arm7tdmi", "-o", str(obj), str(asm)], BUILD)
    run([str(arm / "arm-none-eabi-as"), "-mcpu=arm7tdmi", "-o", str(crt), str(ROOT / "crt0.s")], BUILD)
    run([str(arm / "arm-none-eabi-ld"), "-T", str(ROOT / "rom.ld"), "-o", str(BUILD / "game.elf"), str(crt), str(obj)], BUILD)
    run([str(arm / "arm-none-eabi-objcopy"), "-O", "binary", str(BUILD / "game.elf"), str(BUILD / "demo.gba")], BUILD)
    image = bytearray((BUILD / "demo.gba").read_bytes())
    image[0xA0:0xAC] = b"DEMO QUEST  "
    image[0xAC:0xB0] = b"DQAE"
    image[0xB2] = 0x96
    image[0xBC] = 0
    image[0xBD] = (-(sum(image[0xA0:0xBD]) + 0x19)) & 0xFF
    (BUILD / "demo.gba").write_bytes(image)


if __name__ == "__main__":
    main()
