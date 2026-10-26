"""Export a GBA ROM project as a rebuildable tree."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .rom import BASE


def export_gba(net, slug: str, out: Path, log=print) -> dict:
    proj = net._project(slug)
    cfg = net.cfg(proj)
    work = Path(proj["root"])
    image_name = cfg["rom"]["file"]
    image = (work / image_name).read_bytes()
    if out.exists():
        for child in out.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(work / image_name, out / "baserom.gba")
    (out / "include").mkdir()
    (out / "include" / "common.h").write_text(net._context(proj["id"])["header"], encoding="utf-8")
    rows = net.store.all("SELECT * FROM functions WHERE project_id=? ORDER BY vram", proj["id"])
    functions = []
    matched = 0
    for row in rows:
        record = {"name": row["name"], "addr": row["vram"], "size": row["size"],
                  "thumb": "arm_func_start" not in (row["asm"] or "")}
        if row["status"] == "matched" and row["matched_source"]:
            source = out / "src" / f"{row['name']}.c"
            source.parent.mkdir(exist_ok=True)
            source.write_text(f'#include "common.h"\n\n{row["matched_source"].strip()}\n', encoding="utf-8")
            record["c"] = source.relative_to(out).as_posix()
            matched += 1
        functions.append(record)
    build = {"title": proj["name"], "platform": "gba", "image": "baserom.gba", "output": "build/game.gba",
             "sha1": hashlib.sha1(image).hexdigest(), "base": BASE, "compiler": cfg["compiler"],
             "functions": functions}
    (out / "build.json").write_text(json.dumps(build, indent=1), encoding="utf-8")
    (out / "functions.cfg").write_text(
        "".join(f"{'thumb_func' if f['thumb'] else 'arm_func'} 0x{f['addr']:X} {f['name']}\n" for f in functions),
        encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("build.py"), out / "build.py")
    (out / ".gitignore").write_text("build/\nbaserom.gba\nasm/\n", encoding="utf-8")
    (out / "README.md").write_text(
        f"# {proj['name']} (decompilation)\n\n{matched} of {len(functions)} functions have matched C source.\n"
        "The original ROM is personal data and stays ignored. The exported metadata preserves each function's "
        "address and compiler source for the GBA build adapter.\n", encoding="utf-8")
    log("building the exported tree against the copied personal ROM")
    result = subprocess.run([sys.executable, "build.py"], cwd=out, capture_output=True, text=True, timeout=3600,
                            encoding="utf-8", errors="replace")
    identical = result.returncode == 0 and "identical:" in result.stdout
    details = (result.stdout + result.stderr)[-3000:]
    if not identical:
        log(details)
    return {"path": str(out), "identical": identical, "matched": matched, "functions": len(functions),
            "fallback": [], "log": details}
