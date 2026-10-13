"""Fork mode: continue a public decomp inside its own source tree, judged by objdiff.

projects/demo-fork is a small project in the shape community decomps use (objdiff.json, ninja build, partly
decompiled src/), for Demo Quest 64. The test clones it like a repository from decomp.dev, adds the game,
builds it and has an agent work through the guarded tools.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from decompnet import toolchains
from decompnet.app.engine import RomNetwork
from decompnet.forks import catalog, csource
from decompnet.network import NetworkError

ROOT = Path(__file__).resolve().parent.parent
FORK_SRC = ROOT / "projects" / "demo-fork"
ROM = ROOT / "src" / "decompnet" / "app" / "static" / "demo" / "demo-quest-64.z64"
REFERENCE = (FORK_SRC / "tools" / "reference" / "game.c").read_text()

needs_tools = pytest.mark.skipif(
    toolchains.installed("gcc-2.7.2-kmc") is None or toolchains.installed("objdiff-cli") is None
    or not shutil.which("git"), reason="needs gcc-2.7.2-kmc, objdiff-cli and git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(cwd)}).stdout


@pytest.fixture(scope="module")
def fork(tmp_path_factory):
    base = tmp_path_factory.mktemp("fork")
    upstream = base / "upstream"
    shutil.copytree(FORK_SRC, upstream)
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "initial")
    net = RomNetwork(base / "app.db", base / "work")
    (base / "work").mkdir()
    slug = net.forks.clone(str(upstream), name="Demo Quest decomp", platform="n64", log=lambda *_: None)
    need = net.forks.needs(slug)
    assert need["kind"] == "manifest" and need["path"] == "baserom.z64"
    with pytest.raises(NetworkError, match="sha1"):
        net.forks.setup(slug, b"\0" * 1024, "nope.z64", log=lambda *_: None)
    m = net.forks.setup(slug, ROM.read_bytes(), "demo.z64", log=lambda *_: None)
    assert m["total_functions"] == 13
    agent, _ = net.local_agent("claude-1")
    return net, slug, agent


def _ref(name: str) -> str:
    return csource.get(REFERENCE, name).strip()


@needs_tools
def test_fork_state_after_setup(fork):
    net, slug, agent = fork
    ov = net.overview(slug)
    assert ov["kind"] == "fork" and ov["ready"] and ov["functions"] == 13
    assert ov["matched"] == 4  # clamp_s32, abs_s32, rng_next, add_score
    rows = {r["name"]: r for r in net.function_map(slug)}
    assert rows["clamp_s32"]["status"] == "matched" and rows["lerp"]["status"] == "open"
    assert "fork of" in net.forks.info(slug) and "functions" in net.forks.info(slug)


@needs_tools
def test_agent_matches_a_new_function_and_commits(fork):
    net, slug, agent = fork
    brief = net.claim(agent, slug, "lerp")
    assert "Target assembly" in brief and "src/game.c" in brief and "Not in src/game.c yet" in brief
    res = net.attempt(agent, "lerp", _ref("lerp"), slug)
    assert res["matched"], res["text"]
    text = (Path(net.project_row(slug)["root"]) / "src" / "game.c").read_text()
    # placed where it was in the original: after add_score, before the later player_take_damage
    assert text.index("void add_score(") < text.index("lerp(") < text.index("void player_take_damage(")
    log = _git(Path(net.project_row(slug)["root"]), "log", "--oneline", "decomp-wtf")
    assert "Match lerp" in log
    assert net.overview(slug)["matched"] == 5


@needs_tools
def test_near_miss_gets_a_diff_and_release_restores_the_best(fork):
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    net.claim(agent, slug, "count_alive_entities")
    good = _ref("count_alive_entities")
    near, worse = good.replace("return n;", "return n + 1;"), good.replace("n++;", "n += 2;")
    assert near != good and worse != good
    first = net.attempt(agent, "count_alive_entities", near, slug)
    assert not first["matched"] and first["score"] and "TARGET" in first["text"]
    second = net.attempt(agent, "count_alive_entities", worse, slug)
    assert second["score"] is not None
    net.release(agent, "count_alive_entities", "testing release", slug)
    best = net.store.one("SELECT best_source FROM functions WHERE name='count_alive_entities'")["best_source"]
    assert best.strip() in (root / "src" / "game.c").read_text()


@needs_tools
def test_guards(fork):
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    net.claim(agent, slug, "checksum_save")
    before = (root / "src" / "game.c").read_text()
    with pytest.raises(NetworkError, match="assembly"):
        net.attempt(agent, "checksum_save", 'u32 checksum_save(u8 *p, s32 n) { __asm__("nop"); return 0; }', slug)
    with pytest.raises(NetworkError, match="only the one function"):
        net.attempt(agent, "checksum_save", "struct X { int a; };\n" + _ref("checksum_save"), slug)
    broken = net.attempt(agent, "checksum_save", "u32 checksum_save(void) { return undefined_thing; }", slug)
    assert broken["score"] is None and "BUILD ERROR" in broken["text"]
    assert (root / "src" / "game.c").read_text() == before
    for bad in ("configure.py", "build/expected/game.o", "../outside.c", "objdiff.json", "tools/reference/game.c"):
        with pytest.raises(NetworkError):
            net.forks.edit_file(agent, slug, bad, "", "int x;", "nope")
    with pytest.raises(NetworkError, match="ROM projects"):
        net.get_context(slug)
    # a header edit that would break matches is undone
    header = (root / "include" / "game.h").read_text()
    res = net.forks.edit_file(agent, slug, "include/game.h", "#define MAX_SCORE 999999", "#define MAX_SCORE 5000", "cap")
    assert res.startswith("❌") and "add_score" in res and (root / "include" / "game.h").read_text() == header
    # one that keeps every match is committed
    res = net.forks.edit_file(agent, slug, "include/game.h", "", "#define SAVE_MAGIC 0x5A5A", "save magic")
    assert res.startswith("✅") and "SAVE_MAGIC" in (root / "include" / "game.h").read_text()
    assert net.overview(slug)["matched"] >= 5
    net.release(agent, "checksum_save", "", slug)


@needs_tools
def test_read_and_search(fork):
    net, slug, agent = fork
    assert "clamp_s32" in net.forks.read_file(slug, "src/game.c")
    assert "game.h" in net.forks.search(slug, r"MAX_ENTITIES")
    with pytest.raises(NetworkError):
        net.forks.read_file(slug, ".git/config")
    with pytest.raises(NetworkError, match="source or text"):
        net.forks.read_file(slug, "baserom.z64")
    assert "../" not in net.forks.search(slug, "x", "../**/*.c")


@needs_tools
def test_function_detail(fork):
    net, slug, agent = fork
    d = net.function_detail(slug, "clamp_s32")
    assert d["status"] == "matched" and "slt" in d["asm"] and "clamp_s32" in d["source"]


def test_csource_finds_definitions_and_stubs():
    src = ('s32 f(s32 a) {\n    return a; /* } */\n}\nINCLUDE_ASM("asm/nonmatchings/x", g);\n'
           'uLong h(a)\n    uLong a;\n{\n    return a;\n}\nvoid C::m(int x) {\n}\n')
    assert csource.get(src, "f").startswith("s32 f(") and csource.get(src, "f").endswith("}\n")
    assert "INCLUDE_ASM" in csource.get(src, "g")
    assert csource.get(src, "h").startswith("uLong h(a)")
    assert csource.get(src, "C::m").startswith("void C::m")
    assert csource.get(src, "missing") is None
    assert "return 1" in csource.replace(src, "g", "int g(void) { return 1; }")


def test_catalog_normalizes_decomp_dev():
    raw = {"projects": [{"owner": "o", "repo": "r", "name": "Game", "platform": "gc", "default_version": "GALE01",
                         "commit": {"timestamp": "2026-10-01T00:00:00Z"},
                         "measures": {"matched_code_percent": 42.123, "total_functions": 10, "matched_functions": 4}}]}
    p = catalog.normalize(raw)[0]
    assert p["id"] == "o/r" and p["platform_name"] == "GameCube" and p["matched"] == 42.12
    assert p["repo_url"] == "https://github.com/o/r"


def test_clone_rejects_odd_sources(tmp_path):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    for bad in ("file:///etc", "https://evil.example/x/y", "--upload-pack=x", str(tmp_path)):
        with pytest.raises(NetworkError):
            net.forks.clone(bad, log=lambda *_: None)


@needs_tools
def test_find_similar(fork):
    net, slug, agent = fork
    out = net.find_similar(slug, "entity_spawn")
    assert "index covers" in out or "Functions that look like" in out


def test_similarity_ranks_copies_first():
    from decompnet import similarity

    a = ["addiu", "sw", "lw", "addu", "slt", "bnez", "nop", "jr", "addiu"]
    cands = {"copy": list(a), "tweaked": a[:4] + ["sltu"] + a[5:], "other": ["lui", "ori", "jal", "nop"] * 3}
    ranked = similarity.rank(a, cands, top=3)
    assert [n for n, _ in ranked][:2] == ["copy", "tweaked"] and ranked[0][1] == 1.0


@pytest.mark.skipif(toolchains.installed("m2c") is None, reason="needs m2c")
def test_drafts_from_project_assembly(tmp_path):
    from decompnet.forks import drafts

    (tmp_path / "asm" / "nonmatchings" / "game").mkdir(parents=True)
    (tmp_path / "asm" / "nonmatchings" / "game" / "func_80001234.s").write_text(
        "glabel func_80001234\n"
        "/* 1234 80001234 27BDFFE8 */  addiu      $sp, $sp, -0x18\n"
        "/* 1238 80001238 AFBF0010 */  sw         $ra, 0x10($sp)\n"
        "/* 123C 8000123C 0C000000 */  jal        func_80000000\n"
        "/* 1240 80001240 00000000 */   nop\n"
        "/* 1244 80001244 8FBF0010 */  lw         $ra, 0x10($sp)\n"
        "/* 1248 80001248 03E00008 */  jr         $ra\n"
        "/* 124C 8000124C 27BD0018 */   addiu     $sp, $sp, 0x18\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "game.c").write_text('INCLUDE_ASM("asm/nonmatchings/game", func_80001234);\n')
    out = drafts.draft(tmp_path, "n64", "game", "func_80001234", "src/game.c")
    assert out and "func_80000000();" in out
    assert csource.is_stub('INCLUDE_ASM("asm/nonmatchings/game", func_80001234);')
    assert drafts.draft(tmp_path, "win32", "game", "func_80001234", "src/game.c") is None


@needs_tools
def test_ghidra_drafts_for_a_unit(fork):
    from decompnet.forks import ghidra

    if not ghidra.available():
        pytest.skip("needs Ghidra and Java 21+")
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    out = ghidra.decompile_object(root / "build" / "expected" / "game.o", root / ".decomp-wtf" / "ghidra")
    assert "clamp_s32" in out and "return" in out["clamp_s32"]


@needs_tools
def test_rewriting_a_matched_function_must_keep_it_matching(fork):
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    before = (root / "src" / "game.c").read_text()
    res = net.attempt(agent, "clamp_s32", "s32 clamp_s32(s32 v, s32 lo, s32 hi) { return v; }", slug)
    assert not res["matched"] and "already matches" in res["text"]
    assert (root / "src" / "game.c").read_text() == before
    assert net.store.one("SELECT status FROM functions WHERE name='clamp_s32'")["status"] == "matched"
