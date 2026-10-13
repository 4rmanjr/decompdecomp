"""Game Boy Advance from scratch: a homemade ROM built like GBA games (agbcc, pret-style), split by luvdis plus
decomp.wtf's pass for code nothing calls, and judged byte for byte by the ARM/Thumb matcher.

Demo Quest GBA (projects/demo-gba) is stripped of symbols in the ROM, so the test does what an agent does: split
off a function the finder merged, add one it didn't find, name things, write the C. Every function must match.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from decompnet import toolchains
from decompnet.app.engine import RomNetwork
from decompnet.forks import csource

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "projects" / "demo-gba"
ROM, ELF = DEMO / "build" / "demoquest.gba", DEMO / "build" / "demoq_gba.elf"
GAME = ROOT / "projects" / "demo-n64" / "original"
FUNCTIONS = ["abs_s32", "player_take_damage", "count_alive_entities", "entity_update_physics", "entity_default_hp",
             "entity_spawn", "add_score", "checksum_save", "entity_collides", "game_tick", "clamp_s32", "lerp", "rng_next"]

pytestmark = pytest.mark.skipif(any(toolchains.installed(t) is None for t in ("agbcc", "arm-gnu", "luvdis")),
                                reason="needs agbcc, the Arm GNU toolchain and luvdis")


@pytest.fixture(scope="module")
def gba(tmp_path_factory):
    if not ROM.exists():
        subprocess.run([sys.executable, str(DEMO / "build.py")], check=True)
    work = tmp_path_factory.mktemp("gba")
    net = RomNetwork(work / "app.db", work)
    return net, net.import_rom(ROM, confirmed=True, log=lambda *_: None)


def test_import_finds_the_functions(gba):
    net, slug = gba
    ov = net.overview(slug)
    assert ov["platform"] == "gba" and ov["compiler"]["toolchain"] == "agbcc"
    real = {s["st_value"] & ~1 for s in ELFFile(open(ELF, "rb")).get_section_by_name(".symtab").iter_symbols()
            if s["st_info"]["type"] == "STT_FUNC"}
    found = {r["vram"] for r in net.store.all("SELECT vram FROM functions")}
    assert len(found & real) >= 110 and not found - real  # most functions, and no false starts


def test_every_game_function_matches(gba):
    net, slug = gba
    agent, _ = net.local_agent("gba-agent")
    syms = {s.name: s["st_value"] & ~1 for s in ELFFile(open(ELF, "rb")).get_section_by_name(".symtab").iter_symbols() if s.name}
    header = "\n".join(l for l in (GAME / "game.h").read_text().splitlines()
                       if not l.startswith("#include") and not re.match(r"typedef \w+( \w+)? [su]\d+;|typedef \w+ f\d+;", l))
    assert net.edit_context(agent, slug, "", header, "the game's types").startswith("✅")
    pid = net.project_row(slug)["id"]
    for name in FUNCTIONS:  # merged into the function before, or never found: split it off / add it
        row = net.store.one("SELECT name, vram FROM functions WHERE project_id=? AND vram<=? ORDER BY vram DESC LIMIT 1",
                            pid, syms[name])
        if row["vram"] != syms[name]:
            assert net.split_function(agent, slug, row["name"], f"0x{syms[name]:08X}", name).startswith("✅")
    table = net.symbols(pid)
    for name in FUNCTIONS + ["g_player", "g_entities", "g_score", "g_frame_count", "g_rng_state", "__mulsf3", "__addsf3",
                             "__subsf3", "__divsi3", "__modsi3", "__floatsisf", "__fixsfsi", "__ltsf2", "__gtsf2"]:
        if name in syms:
            current, offset = table.name_at(syms[name])
            if offset:
                current = f"D_{syms[name]:08X}"
            if current != name:
                net.rename_symbol(agent, slug, current, name)
                table = net.symbols(pid)
    source = (GAME / "game.c").read_text()
    scores = {name: net.attempt(agent, name, csource.get(source, name), slug)["score"] for name in FUNCTIONS}
    assert all(score == 0 for score in scores.values()), scores


def test_drafts_and_look_alikes(gba):
    net, slug = gba
    row = net.store.one("SELECT name FROM functions WHERE status='open' AND n_insns > 20 LIMIT 1")
    brief = net.get_function(row["name"], slug)
    assert "thumb_func_start" in brief or "push" in brief
    assert net.find_similar(slug, row["name"]).startswith(("Functions that look like", "No look-alikes"))
