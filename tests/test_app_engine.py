"""The local engine on Demo Quest 64: import, claim, split, match, rename, read data, export."""

import shutil

import pytest

from decompnet import toolchains
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError

from .test_rom import built  # noqa: F401  (fixture that builds the demo ROM)

pytestmark = pytest.mark.skipif(toolchains.installed("gcc-2.7.2-kmc") is None or not shutil.which("ld.lld"),
                                reason="needs gcc-2.7.2-kmc and ld.lld to build the demo ROM")

CLAMP = """s32 func_800007D0(s32 v, s32 lo, s32 hi) {
    if (v < lo) {
        return lo;
    }
    if (v > hi) {
        return hi;
    }
    return v;
}"""

DAMAGE = """typedef struct {
    u8 ent[0x1C];
    s32 hp;
    s16 invuln_timer;
    s16 state;
    s32 combo;
} Player;
extern Player D_8004AE34;

void func_80000808(s32 amount) {
    if (D_8004AE34.invuln_timer > 0) {
        return;
    }
    D_8004AE34.hp -= amount;
    if (D_8004AE34.hp <= 0) {
        D_8004AE34.hp = 0;
        D_8004AE34.state = 3;
    }
    D_8004AE34.invuln_timer = 60;
}"""


@pytest.fixture(scope="module")
def net(built, tmp_path_factory):  # noqa: F811
    ws = tmp_path_factory.mktemp("ws")
    net = RomNetwork(ws / "decomp.db", ws)
    slug = net.import_rom(built, confirmed=True, log=lambda *_: None)
    assert slug == "demo-quest-64"
    return net


def test_import_needs_the_ownership_confirmation(built, tmp_path):  # noqa: F811
    with pytest.raises(NetworkError, match="legally obtained"):
        RomNetwork(tmp_path / "d.db", tmp_path).import_rom(built, log=lambda *_: None)


def test_import_is_idempotent_and_described(net, built):  # noqa: F811
    assert net.import_rom(built, confirmed=True, log=lambda *_: None) == "demo-quest-64"
    ov = net.overview("demo-quest-64")
    assert ov["functions"] >= 95 and ov["matched"] == 0 and ov["compiler"]["toolchain"] == "gcc-2.7.2-kmc"
    assert ov["rom"]["game_code"] == "NDQE"


def test_split_match_and_rename(net):
    agent, _ = net.local_agent("tester")
    # exit() never returns, so the disassembler ran it into clamp_s32: split them, then match clamp_s32
    assert "split" in net.split_function(agent, "demo-quest-64", "func_800007C4", "0x800007D0")
    brief = net.claim(agent, "demo-quest-64", "func_800007D0")
    assert "0x800007D0" in brief and "slt " in brief and "m2c" in brief
    out = net.compile_and_diff(agent, "func_800007D0", CLAMP, "demo-quest-64")
    assert out.startswith("✅ MATCH"), out
    out = net.rename_symbol(agent, "demo-quest-64", "func_800007D0", "clamp_s32")
    assert out.startswith("✅"), out
    assert net._function("clamp_s32", "demo-quest-64")["status"] == "matched"
    assert "clamp_s32" in net._function("clamp_s32", "demo-quest-64")["matched_source"]


def test_struct_access_through_an_automatic_name(net):
    agent, _ = net.local_agent("tester2")
    brief = net.get_function("func_80000808", "demo-quest-64")
    assert "D_8004AE50" in brief  # g_player.hp, as the assembly names it
    out = net.compile_and_diff(agent, "func_80000808", DAMAGE, "demo-quest-64")
    assert out.startswith("✅ MATCH"), out
    # renaming the struct's address keeps the match (the source is updated with it)
    assert net.rename_symbol(agent, "demo-quest-64", "D_8004AE34", "g_player").startswith("✅")
    assert "g_player.hp" in net._function("func_80000808", "demo-quest-64")["matched_source"]


def test_wrong_code_gets_a_diff_and_link_notes(net):
    agent, _ = net.local_agent("tester3")
    out = net.compile_and_diff(agent, "func_80000A0C", "void func_80000A0C(s32 p) { mystery(p); }", "demo-quest-64")
    assert "score" in out and "mystery" in out
    net.release(agent, "func_80000A0C", "not this", "demo-quest-64")


def test_read_data_and_lists(net):
    text = net.read_data("demo-quest-64", "0x800082C0", 16)
    assert "DEMOQUEST-SAVE" in text
    assert "bss" in net.read_data("demo-quest-64", "g_player", 16)
    assert "clamp_s32" in net.list_functions("demo-quest-64", "matched")


def test_export_and_live(net):
    out = net.export_rom("demo-quest-64")
    assert "clamp_s32" in (out / "src" / "game.c").read_text()
    live = net.live("demo-quest-64")
    assert live["overview"]["matched"] >= 2 and any(e["kind"] == "match" for e in live["events"])
    fmap = net.function_map("demo-quest-64")
    assert sum(f["status"] == "matched" for f in fmap) >= 2
    detail = net.function_detail("demo-quest-64", "clamp_s32")
    assert detail["status"] == "matched" and detail["scores"]
