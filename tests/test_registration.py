"""Reputation, open registration, per-unit headers and upstream export."""

import json
import subprocess

import pytest

from decompnet import policy, toolchains
from decompnet.network import Network, NetworkError
from decompnet.project import import_project
from decompnet.registration import Registrar
from decompnet.upstream import export as upstream_export

from .solutions import SOLUTIONS, WRONG_CLAMP
from .test_network import DEMO, HOST_GCC, needs_host_gcc

PROJECTS = DEMO.parent


@pytest.fixture
def net(tmp_path):
    if not HOST_GCC:
        pytest.skip(needs_host_gcc.kwargs["reason"])
    n = Network(tmp_path / "t.db")
    import_project(n.store, DEMO, log=lambda *_: None)
    return n


def web_agent(net, name="newbie"):
    reg = Registrar(net, secret="s", hostnames={"decomp.online"},
                    verify=lambda t, ip: {"success": True, "action": "signup", "hostname": "decomp.online"})
    out = reg.register({"name": name, "accept_terms": True, "age_ok": True, "turnstile": "tok"}, "203.0.113.7")
    return net.authenticate(out["token"]), reg


def test_registration_guards(net):
    closed = Registrar(net, secret="", hostnames=set())
    with pytest.raises(NetworkError, match="closed"):
        closed.register({"name": "a", "accept_terms": True, "age_ok": True, "turnstile": "t"}, None)
    agent, reg = web_agent(net)
    assert not agent.trusted
    row = net.store.one("SELECT origin, terms_accepted_at FROM agents WHERE id=?", agent.id)
    assert row["origin"] == "web" and row["terms_accepted_at"]
    for payload, why in [({"accept_terms": False, "age_ok": True}, "terms"), ({"accept_terms": True, "age_ok": False}, "13")]:
        with pytest.raises(NetworkError, match=why):
            reg.register({"name": "b", "turnstile": "t", **payload}, "203.0.113.7")
    wrong_host = Registrar(net, secret="s", hostnames={"decomp.online"},
                           verify=lambda t, ip: {"success": True, "action": "signup", "hostname": "evil.example"})
    with pytest.raises(NetworkError, match="bot check"):
        wrong_host.register({"name": "c", "accept_terms": True, "age_ok": True, "turnstile": "t"}, None)
    for i in range(2):  # 3 per IP per day
        reg.register({"name": f"more{i}", "accept_terms": True, "age_ok": True, "turnstile": "t"}, "203.0.113.7")
    with pytest.raises(NetworkError, match="too many"):
        reg.register({"name": "toomany", "accept_terms": True, "age_ok": True, "turnstile": "t"}, "203.0.113.7")
    assert net.store.one("SELECT COUNT(*) AS n FROM registrations")["n"] == 3
    assert "203.0.113.7" not in json.dumps([dict(r) for r in net.store.all("SELECT * FROM registrations")])


def test_reputation_gates_header_edits_and_budget(net, monkeypatch):
    agent, _ = web_agent(net)
    assert policy.reputation(net.store, agent.id)["level"].name == "new"
    with pytest.raises(NetworkError, match="member"):
        net.edit_context(agent, "demo", "", "s32 x;", "add")
    assert "MATCH" in net.compile_and_diff(agent, "abs_s32", SOLUTIONS["abs_s32"])
    assert policy.reputation(net.store, agent.id)["level"].name == "member"
    assert "now v2" in net.edit_context(agent, "demo", "", "s32 entity_default_hp(s16 type);", "add proto")
    monkeypatch.setattr(policy, "LEVELS", (policy.Level("new", 0, 0, False), policy.Level("member", 1, 1, True)))
    with pytest.raises(NetworkError, match="compile budget"):
        net.compile_and_diff(agent, "clamp_s32", WRONG_CLAMP)


needs_n64 = pytest.mark.skipif(toolchains.installed("gcc-2.7.2-kmc") is None, reason="gcc-2.7.2-kmc not installed")


zlib_ready = (PROJECTS / "zlib-n64" / "original" / "zlib.h").exists()


@needs_n64
@pytest.mark.skipif(not zlib_ready, reason="run projects/zlib-n64/prepare.py first")
def test_zlib_uses_per_unit_headers(tmp_path):
    net = Network(tmp_path / "z.db")
    import_project(net.store, PROJECTS / "zlib-n64", log=lambda *_: None)
    assert net.store.one("SELECT COUNT(*) AS n FROM functions")["n"] == 71
    assert net._unit_contexts(net._project("zlib-n64")["id"]) >= {"deflate", "inflate", "trees"}
    with pytest.raises(NetworkError, match="pass `function`"):
        net.get_context("zlib-n64")
    assert "deflate_state" in net.get_context("zlib-n64", "fill_window")
    agent = net.authenticate(net.register_agent("z"))
    src = (PROJECTS / "zlib-n64" / "original" / "adler32.c").read_text()
    body = src[src.index("uLong ZEXPORT adler32("):]
    assert "MATCH" in net.compile_and_diff(agent, "adler32", body[: body.index("\n}\n") + 3], project="zlib-n64")


def test_upstream_export_commits_with_credit(net, tmp_path):
    a = net.authenticate(net.register_agent("alice", "Alice"))
    net.compile_and_diff(a, "abs_s32", SOLUTIONS["abs_s32"])
    repo = tmp_path / "up"
    assert upstream_export(net, "demo", repo, log=lambda *_: None) == 1
    assert upstream_export(net, "demo", repo, log=lambda *_: None) == 0  # idempotent
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%an|%s|%b"], capture_output=True, text=True).stdout
    assert "alice|Match abs_s32" in log and "operator: Alice" in log
    assert "reconstruction, not the original source" in (repo / "src" / "game" / "abs_s32.c").read_text()
