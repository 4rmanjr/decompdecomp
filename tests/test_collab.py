"""Rooms: a host and a guest, each with their own copy of Demo Quest 64, working together."""

import json
import re
import shutil

import pytest
from starlette.testclient import TestClient

from decompnet import toolchains
from decompnet.app import collab
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError

from .test_app_engine import CLAMP, DAMAGE
from .test_rom import built  # noqa: F401

pytestmark = pytest.mark.skipif(toolchains.installed("gcc-2.7.2-kmc") is None or not shutil.which("ld.lld"),
                                reason="needs gcc-2.7.2-kmc")


@pytest.fixture(scope="module")
def room(built, tmp_path_factory):  # noqa: F811
    h = tmp_path_factory.mktemp("host")
    host = RomNetwork(h / "d.db", h)
    slug = host.import_rom(built, confirmed=True, log=lambda *_: None)
    host.rooms[slug] = collab.Room(host, slug)
    code = host.rooms[slug].open_invite()
    client = TestClient(collab.host_app(host, host.rooms), base_url="https://abc.trycloudflare.com")
    g = tmp_path_factory.mktemp("guest")
    guest = RomNetwork(g / "d.db", g)
    link = f"https://abc.trycloudflare.com/join/{code}#room={slug}"
    gslug = collab.join_room(guest, link, "alex", built, log=lambda *_: None, client=client, start=False)
    return host, slug, guest, gslug, client, code, link


def test_parse_invite():
    assert collab.parse_invite("https://x-y.trycloudflare.com/join/abcdefghij#room=demo") == ("https://x-y.trycloudflare.com", "abcdefghij#demo")
    with pytest.raises(NetworkError):
        collab.parse_invite("https://example.com/whatever")


def test_joining_needs_the_same_rom_and_a_valid_invite(room, built, tmp_path):  # noqa: F811
    host, slug, guest, gslug, client, code, link = room
    other = RomNetwork(tmp_path / "o.db", tmp_path)
    changed = bytearray(built)
    changed[0x2000] ^= 0xFF
    with pytest.raises(NetworkError, match="same as the host"):
        collab.join_room(other, link, "eve", bytes(changed), log=lambda *_: None, client=client, start=False)
    with pytest.raises(NetworkError, match="no longer valid"):
        collab.join_room(other, link.replace(code, "x" * 22), "eve", built, log=lambda *_: None, client=client, start=False)
    assert [m["name"] for m in host.rooms[slug].members()] == ["alex"]


def test_state_carries_work_never_rom_data(room):
    host, slug, guest, gslug, client, code, link = room
    tok = guest.links[gslug].token
    st = client.get(f"/collab/state?room={slug}", headers={"authorization": f"Bearer {tok}"}).json()
    text = json.dumps(st)
    assert "glabel" not in text and "target" not in st
    assert not re.search(r"/\* [0-9A-F]+ [0-9A-F]{8} [0-9A-F]{8} \*/", text)  # no disassembly lines
    assert st["functions"] and st["members"][0]["name"] == "alex"
    assert client.get(f"/collab/state?room={slug}", headers={"authorization": "Bearer dn_nope"}).status_code == 400


def test_split_and_match_from_the_guest_reach_the_host(room):
    host, slug, guest, gslug, client, code, link = room
    agent, _ = guest.local_agent("claude-1")
    assert guest.split_function(agent, gslug, "func_800007C4", "0x800007D0").startswith("✅")
    assert host._function("func_800007D0", slug)  # split on the host...
    assert guest._function("func_800007D0", gslug)  # ...and mirrored locally
    brief = guest.claim(agent, gslug, "func_800007D0")
    assert "glabel func_800007D0" in brief  # the guest's own copy provides the assembly
    claims = host.live(slug)["claims"]
    assert claims and claims[0]["agent"] == "alex.claude-1"
    out = guest.compile_and_diff(agent, "func_800007D0", CLAMP, gslug)
    assert out.startswith("✅ MATCH") and "host confirmed" in out
    assert host._function("func_800007D0", slug)["status"] == "matched"


def test_host_work_reaches_the_guest(room):
    host, slug, guest, gslug, client, code, link = room
    hagent, _ = host.local_agent("codex-1")
    assert host.compile_and_diff(hagent, "func_80000808", DAMAGE, slug).startswith("✅ MATCH")
    assert host.rename_symbol(hagent, slug, "func_800007D0", "clamp_s32").startswith("✅")
    assert host.edit_context(hagent, slug, "", "s32 clamp_s32(s32 v, s32 lo, s32 hi);", "prototype").startswith("✅")
    guest.links[gslug].sync()
    assert guest._function("func_80000808", gslug)["status"] == "matched"
    assert guest._function("clamp_s32", gslug)["status"] == "matched"
    assert "clamp_s32(s32 v" in guest.get_context(gslug)
    feed = [e["kind"] for e in guest.live(gslug)["events"]]
    assert "match" in feed
    assert guest.overview(gslug)["room"]["role"] == "guest"


def test_guest_edits_go_through_the_host(room):
    host, slug, guest, gslug, client, code, link = room
    agent, _ = guest.local_agent("claude-2")
    out = guest.rename_symbol(agent, gslug, "func_80000808", "player_take_damage")
    assert out.startswith("✅")
    assert host._function("player_take_damage", slug)["status"] == "matched"
    assert guest._function("player_take_damage", gslug)
    guest.add_note(agent, gslug, "player struct is at 0x8004AE34")
    assert "0x8004AE34" in host.search_notes(slug, "player struct")


ABS = "s32 func_800007F4(s32 v) { return v < 0 ? -v : v; }"


def test_two_friends_work_together(room, built, tmp_path):  # noqa: F811
    host, slug, guest, gslug, client, code, link = room
    bea = RomNetwork(tmp_path / "bea.db", tmp_path)
    bslug = collab.join_room(bea, link, "bea", built, log=lambda *_: None, client=client, start=False)
    assert sorted(m["name"] for m in host.rooms[slug].members()) == ["alex", "bea"]
    a_agent, _ = guest.local_agent("codex-1")
    b_agent, _ = bea.local_agent("claude-1")
    guest.claim(a_agent, gslug)
    bea.claim(b_agent, bslug)
    claims = {c["agent"]: c["name"] for c in host.live(slug)["claims"]}
    assert claims["alex.codex-1"] != claims["bea.claude-1"]  # the host never hands out the same function twice
    guest.release(a_agent, claims["alex.codex-1"], "", gslug)
    out = guest.compile_and_diff(a_agent, "func_800007F4", ABS, gslug)
    assert out.startswith("✅ MATCH"), out
    bea.links[bslug].sync()
    assert bea._function("func_800007F4", bslug)["status"] == "matched"  # bea sees alex's match


def test_reconnect_after_the_host_restarts(room, built):  # noqa: F811
    host, slug, guest, gslug, client, code, link = room
    host.rooms[slug].close()  # the host quits: the tunnel and its address are gone
    new_code = host.rooms[slug].open_invite()  # ...and shares again from a new address
    new_client = TestClient(collab.host_app(host, host.rooms), base_url="https://new-address.trycloudflare.com")
    new_link = f"https://new-address.trycloudflare.com/join/{new_code}#room={slug}"
    before = sorted(m["name"] for m in host.rooms[slug].members())
    again = collab.join_room(guest, new_link, "alex", b"", log=lambda *_: None, client=new_client, start=False)
    assert again == gslug
    assert sorted(m["name"] for m in host.rooms[slug].members()) == before  # same member, not "alex2"
    link_obj = guest.links[gslug]
    assert link_obj.url == "https://new-address.trycloudflare.com" and link_obj.member == "alex"
    link_obj.sync()
    agent, _ = guest.local_agent("claude-9")
    assert "CLAIMED" in guest.claim(agent, gslug)


def test_closing_the_room_locks_guests_out(room):
    host, slug, guest, gslug, client, code, link = room
    host.rooms[slug].close()
    with pytest.raises(NetworkError):
        collab.join_room(RomNetwork(":memory:", __import__("pathlib").Path("/tmp")), link, "late", b"", client=client, start=False)
