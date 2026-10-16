"""PlayStation 2 ROM projects: split a homemade PS2 executable and prove the matcher on it.

Demo Quest PS1 (projects/demo-ps2) is the Demo Quest game built for the PlayStation with GCC 2.7.2 at -O2 -G8,
little-endian, small globals $gp-relative, like PSY-Q-era games. The executable carries no symbols, so the test
does what an agent does: give addresses names, declare the game's types, then write each function. Every one of
them must match byte for byte.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from decompnet import toolchains
from decompnet.app import tools
from decompnet.app.engine import RomNetwork
from decompnet.forks import csource
from decompnet.network import NetworkError

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "projects" / "demo-ps2"
EXE = DEMO / "build" / "SLUS_999.99"
ELF = DEMO / "build" / "demoq_ps2.elf"
GAME = ROOT / "projects" / "demo-n64" / "original"
FUNCTIONS = ["clamp_s32", "abs_s32", "player_take_damage", "rng_next", "count_alive_entities", "entity_update_physics",
             "entity_default_hp", "entity_spawn", "add_score", "lerp", "checksum_save", "entity_collides", "game_tick"]

pytestmark = pytest.mark.skipif(toolchains.installed("ee-gcc-2.96") is None or toolchains.installed("binutils-mips") is None,
                                reason="needs ee-gcc-2.96 and binutils-mips to build the PS2 demo")


@pytest.fixture(scope="module")
def psx(tmp_path_factory):
    if not EXE.exists():
        subprocess.run([sys.executable, str(DEMO / "build.py")], check=True)
    work = tmp_path_factory.mktemp("ps2")
    net = RomNetwork(work / "app.db", work)
    slug = net.import_rom(EXE, confirmed=True, log=lambda *_: None)
    return net, slug


def test_import(psx):
    net, slug = psx
    ov = net.overview(slug)
    assert ov["platform"] == "ps2" and ov["compiler"]["toolchain"] == "ee-gcc-2.96" and ov["functions"] > 60
    assert ov["compiler"]["flags"] == ["-O2", "-G8"]
    assert "WITHOUT" in net.get_context(slug)  # the gp-relative globals advice


def test_a_removed_game_can_be_imported_again(tmp_path):
    """Issue #18: removing a project left its symbols behind, and importing the game again (the new project gets
    the same id) failed on them. A database that has such leftovers loses them when decompdecomp opens it."""
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = net.import_rom(EXE, confirmed=True, log=lambda *_: None)
    assert net.store.one("SELECT COUNT(*) AS n FROM symbols")["n"] > 60
    net.remove_project(slug)
    assert net.store.one("SELECT COUNT(*) AS n FROM symbols")["n"] == 0
    slug = net.import_rom(EXE, confirmed=True, log=lambda *_: None)
    pid = net.project_row(slug)["id"]
    net.remove_project(slug)
    with net.store.tx() as db:  # what 0.10.2 left behind
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, "main", 0x100000, 0, "func"))
    again = RomNetwork(tmp_path / "app.db", tmp_path)
    assert again.store.one("SELECT COUNT(*) AS n FROM symbols")["n"] == 0
    again.import_rom(EXE, confirmed=True, log=lambda *_: None)


def test_every_game_function_matches(psx):
    net, slug = psx
    _match_every_function(net, slug, ELF)


def _match_every_function(net, slug, elf):
    agent, _ = net.local_agent("psx-agent")
    syms = {s.name: s["st_value"] for s in ELFFile(open(elf, "rb")).get_section_by_name(".symtab").iter_symbols()}
    header = (GAME / "game.h").read_text()
    header = "\n".join(l for l in header.splitlines()
                       if not l.startswith("#include") and not re.match(r"typedef \w+( \w+)? [su]\d+;|typedef \w+ f\d+;", l))
    header = re.sub(r"^extern (s32|u32) (g_\w+);", r"\1 \2;", header, flags=re.M)  # small globals: no extern
    assert net.edit_context(agent, slug, "", header, "the game's types").startswith("✅")
    table = net.symbols(net.project_row(slug)["id"])
    for name in FUNCTIONS + ["g_player", "g_entities", "g_score", "g_frame_count", "g_rng_state",
                             "__muldi3", "__udivdi3", "__umoddi3", "__divdi3", "__moddi3"]:
        if name in syms:
            current, offset = table.name_at(syms[name])
            if offset:  # inside a bigger data symbol (adjacent globals): name the address itself
                current = f"D_{syms[name]:08X}"
            if current != name:
                net.rename_symbol(agent, slug, current, name)
                table = net.symbols(net.project_row(slug)["id"])
    source = (GAME / "game.c").read_text()
    scores = {}
    for name in FUNCTIONS:
        res = net.attempt(agent, name, csource.get(source, name), slug)
        scores[name] = res["score"]
        if not res["matched"]:
            print(name, res["text"][:3000])
            net.release(agent, name, "", slug)
    assert all(score == 0 for score in scores.values()), scores


def test_wrong_code_does_not_match(psx):
    net, slug = psx
    agent, _ = net.local_agent("psx-agent-2")
    fn = net.store.one("SELECT name FROM functions WHERE status='open' AND n_insns > 8 ORDER BY n_insns LIMIT 1")
    res = net.attempt(agent, fn["name"], f"int {fn['name']}(int a) {{ return a * 3; }}", slug)
    assert not res["matched"]
    net.release(agent, fn["name"], "", slug)


OTHER_COMPILERS = ["ee-gcc-2.9-991111", "ee-gcc-2.95.2", "ee-gcc-2.95.3", "ee-gcc-3.2", "mwcps2-2.3.3", "mwcps2-3.0.1"]


@pytest.mark.parametrize("toolchain", OTHER_COMPILERS)
def test_games_built_with_other_compilers_match(toolchain, tmp_path):
    """PS2 games were built with many compilers: Sony's EE-GCC releases, SN Systems' (ProDG) and Metrowerks'
    CodeWarrior. The demo built with each must import with it and match function by function."""
    if toolchains.installed(toolchain) is None or toolchains.installed("wibo") is None:
        pytest.skip(f"needs {toolchain} (and wibo)")
    build = DEMO / "build" / toolchain
    if not (build / "SLUS_999.99").exists():
        subprocess.run([sys.executable, str(DEMO / "build.py"), toolchain], check=True)
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = net.import_rom(build / "SLUS_999.99", confirmed=True, log=lambda *_: None, toolchain=toolchain)
    ov = net.overview(slug)
    assert ov["compiler"]["toolchain"] == toolchain and ov["functions"] > 60
    if toolchain.startswith("mwcps2"):
        assert ov["compiler"]["flags"] == ["-O2", "-sdatathreshold", "8"]
        assert ov["rom"]["compiler_guess"].startswith("MWCPS2")  # CodeWarrior's .mwcats sections give it away
    _match_every_function(net, slug, build / "demoq_ps2.elf")


@pytest.mark.parametrize("toolchain", ["ee-gcc-2.96", *OTHER_COMPILERS])
def test_the_import_finds_the_compiler(toolchain, tmp_path):
    """Not told which compiler built it, the import works it out: CodeWarrior and its version, and SN's 2.95.2, from
    the code's style; the other GCCs by compiling drafts of small functions with each."""
    if toolchains.installed(toolchain) is None or toolchains.installed("m2c") is None:
        pytest.skip(f"needs {toolchain} and m2c")
    build = DEMO / "build" if toolchain == "ee-gcc-2.96" else DEMO / "build" / toolchain
    if not (build / "SLUS_999.99").exists():
        subprocess.run([sys.executable, str(DEMO / "build.py"), toolchain], check=True)
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = net.import_rom(build / "SLUS_999.99", confirmed=True, log=lambda *_: None)
    assert net.overview(slug)["compiler"]["toolchain"] == toolchain


def test_a_librarys_note_does_not_name_the_compiler(monkeypatch):
    """GTA San Andreas keeps one "MW MIPS C Compiler" note, from a library: its own code is EE-GCC 2.96's, and
    agents could match nothing until the compiler was switched. The code's style outweighs the note."""
    from decompnet.rom import ps2

    if not EXE.exists():
        pytest.skip("needs the PS2 demo built")
    monkeypatch.setattr(ps2, "_sections", lambda data: {".comment": b"MW MIPS C Compiler (2.4.1.01)\0PlayStation2\0"})
    assert ps2.guess_compiler(EXE.read_bytes()) == "EEGCC"


def test_agents_suggest_a_compiler_and_the_user_applies_it(tmp_path):
    """GTA San Andreas: agents worked out the compiler was wrong (one said EE-GCC 2.96, another 2.95.3). They suggest
    it; each suggestion naming a function is checked with that function's C before the user sees it, agreement is
    counted, and applying it switches the project."""
    tc = "ee-gcc-2.95.3"
    if any(toolchains.installed(t) is None for t in (tc, "ee-gcc-2.96", "wibo")):
        pytest.skip(f"needs {tc}, ee-gcc-2.96 and wibo")
    build = DEMO / "build" / tc
    if not (build / "SLUS_999.99").exists():
        subprocess.run([sys.executable, str(DEMO / "build.py"), tc], check=True)
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = net.import_rom(build / "SLUS_999.99", confirmed=True, log=lambda *_: None, toolchain="ee-gcc-3.2")
    addr = {s.name: s["st_value"] for s in ELFFile(open(build / "demoq_ps2.elf", "rb")).get_section_by_name(".symtab")
            .iter_symbols()}["entity_default_hp"]
    name = net.symbols(net.project_row(slug)["id"]).name_at(addr)[0]
    header = (GAME / "game.h").read_text()
    enums = "\n".join(l for l in header.splitlines() if l.startswith(("enum ", "#define ENTITY")))
    source = enums + "\n" + csource.get((GAME / "game.c").read_text(), "entity_default_hp").replace(
        "entity_default_hp(", f"{name}(", 1)
    a, _ = net.local_agent("agent-a")
    assert not net.attempt(a, name, source, slug)["matched"]  # right C, wrong compiler
    net.release(a, name, "", slug)

    assert "does not match" in net.suggest(a, slug, "daddu moves: some EE-GCC", "ee-gcc-2.96", function=name)
    b, _ = net.local_agent("agent-b")
    assert "matches the game" in net.suggest(b, slug, "sd $ra, filled delay slots: SN's 2.95.3", tc, function=name)
    c, _ = net.local_agent("agent-c")
    net.suggest(c, slug, "agreed", tc)
    with pytest.raises(NetworkError):
        net.suggest(c, slug, "?", "gcc-9000")
    shown = net.overview(slug)["suggestions"]
    right = next(s for s in shown if s["toolchain"] == tc)
    assert right["agents"] == ["agent-b", "agent-c"] and "matches the game" in right["check"] and len(shown) == 2

    net.resolve_suggestion(slug, right["id"], apply=True)
    ov = net.overview(slug)
    assert ov["compiler"]["toolchain"] == tc and ov["compiler"]["flags"] == ["-O2", "-G8"]
    assert [s["toolchain"] for s in ov["suggestions"]] == ["ee-gcc-2.96"]
    assert net.attempt(a, name, source, slug)["matched"]


def test_auto_adjust_applies_a_proven_compiler(tmp_path):
    """Settings: auto-adjust the compiler. A suggestion proven by a function's C applies at once; others wait for
    agreement (three agents) or the user."""
    tc = "ee-gcc-2.95.3"
    if any(toolchains.installed(t) is None for t in (tc, "ee-gcc-3.2", "wibo")):
        pytest.skip(f"needs {tc}, ee-gcc-3.2 and wibo")
    build = DEMO / "build" / tc
    if not (build / "SLUS_999.99").exists():
        subprocess.run([sys.executable, str(DEMO / "build.py"), tc], check=True)
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    net.auto_compiler = True
    slug = net.import_rom(build / "SLUS_999.99", confirmed=True, log=lambda *_: None, toolchain="ee-gcc-3.2")
    agents_ = [net.local_agent(f"agent-{k}")[0] for k in range(3)]
    for k, a in enumerate(agents_[:2]):  # agreement without proof: two isn't enough
        assert net.suggest(a, slug, "looks like 2.9", "ee-gcc-2.9-991111").startswith("Suggested")
    assert net.overview(slug)["compiler"]["toolchain"] == "ee-gcc-3.2"
    assert "Open suggestions" in tools.project_info(net, slug)
    assert net.suggest(agents_[2], slug, "me too", "ee-gcc-2.9-991111").startswith("Applied")
    assert net.overview(slug)["compiler"]["toolchain"] == "ee-gcc-2.9-991111"
    with pytest.raises(NetworkError):
        net.suggest(agents_[0], slug, "again", "ee-gcc-2.9-991111")  # already the project's
