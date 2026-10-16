"""Build Demo Quest PS2, a PlayStation 2 executable made only from code we may share: the same Demo Quest game,
main loop and tiny libc as the N64 demo (CC0, ../demo-rom/src, ../demo-n64/original) and zlib 1.1.4 (Zlib
license), compiled with EE-GCC 2.96 at -O2 -G8 and linked at 0x00100000 like PS2 games.

    uv run python projects/demo-ps2/build.py    ->  build/SLUS_999.99 (stripped, like a game) + build/demoq_ps2.elf

With a toolchain id (ee-gcc-2.95.2, mwcps2-3.0.1, ...), the same game built with that compiler instead, as games
of its time were, into build/<toolchain>/. The start-up code and the 64-bit arithmetic helpers (ps2rt.c, what libgcc
is to a game: linked in prebuilt) are always EE-GCC 2.96's.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from decompnet import toolchains
from decompnet.compiler import run_in
from decompnet.project import compiler_config

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
DEMO = HERE.parent / "demo-rom" / "src"
GAME = HERE.parent / "demo-n64" / "original"
ZLIB = HERE.parent / "zlib-n64"
ZLIB_UNITS = ["adler32", "compress", "crc32", "deflate", "inflate", "infblock", "infcodes", "inffast", "inftrees",
              "infutil", "trees", "uncompr", "zutil"]


def run(argv, cwd=None):
    res = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    if res.returncode != 0:
        sys.exit(f"failed: {' '.join(map(str, argv))}\n{res.stdout}{res.stderr}")


def out_dir(toolchain: str) -> Path:
    return BUILD if toolchain == "ee-gcc-2.96" else BUILD / toolchain


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("toolchain", nargs="?", default="ee-gcc-2.96")
    toolchain = ap.parse_args().toolchain
    build = out_dir(toolchain)
    if build.exists():
        shutil.rmtree(build)
    (build / "src").mkdir(parents=True)
    # EE-GCC is a 32-bit Linux program: run it in decompdecomp's compiler sandbox, on copies of the sources
    for name, src in (("demo", DEMO), ("game", GAME), ("zlib", ZLIB / "original"), ("sysinclude", ZLIB / "sysinclude")):
        shutil.copytree(src, build / "src" / name)
    shutil.copy(HERE / "crt0.s", build / "src" / "crt0.s")
    shutil.copy(HERE / "ps2rt.c", build / "src" / "ps2rt.c")
    small = ["-sdatathreshold", "8"] if toolchain.startswith("mwcps2") else ["-G8"]  # globals up to 8 bytes: $gp
    jobs = [("src/crt0.s", "ee-gcc-2.96", ["-G8"]), ("src/demo/main.c", toolchain, ["-Isrc/game"]),
            ("src/demo/libc.c", toolchain, []), ("src/ps2rt.c", "ee-gcc-2.96", ["-G8"]),
            ("src/game/game.c", toolchain, ["-Isrc/game"])]
    jobs += [(f"src/zlib/{u}.c", toolchain, ["-Isrc/zlib"]) for u in ZLIB_UNITS]
    objs = []
    for src, tc, extra in jobs:
        obj = f"{Path(src).stem}.o"
        flags = ["-O2", *(small if tc == toolchain else []), *extra]
        cfg = compiler_config(build, {"compiler": {"toolchain": tc, "flags": flags}})
        # the toolchain's own compile command, on the copies in build/ (src/sysinclude: the demo's tiny C library)
        argv = [a.replace("{src}", src).replace("{obj}", obj).replace("{include}", "src/sysinclude") for a in cfg.cmd]
        res = run_in(cfg, build, argv, inputs=["src"], outputs=[obj])
        if res.returncode != 0:
            sys.exit(f"failed: {src}\n{res.stdout}{res.stderr}")
        objs.append(obj)
    elf = build / "demoq_ps2.elf"
    # the EE assembler writes .rel and .rela sections for the same section, which only GNU ld links
    ld = str(toolchains.binary("binutils-mips").parent / "mips-ps2-decompals-ld")
    run([ld, "-EL", "-T", str(HERE / "elf.ld"), "-o", str(elf), *objs], cwd=build)
    game = build / "SLUS_999.99"
    run([ld.replace("-ld", "-objcopy"), "--strip-all", str(elf), str(game)])
    print(f"built {game.relative_to(HERE)} ({game.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
