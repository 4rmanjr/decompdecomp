"""Game Boy Advance ROM recognition and ROM-mode support."""

import hashlib
import json
import struct

from decompnet import consoles
from decompnet.rom import Picked


def _gba_rom() -> bytes:
    rom = bytearray(0xC0)
    struct.pack_into("<I", rom, 0, 0xEA00002E)
    rom[0xA0:0xAC] = b"DEMO QUEST\0\0"
    rom[0xAC:0xB0] = b"DQAE"
    rom[0xB2] = 0x96
    rom[0xBC] = 3
    rom[0xBD] = (-(sum(rom[0xA0:0xBD]) + 0x19)) & 0xFF
    return bytes(rom)


def test_gba_header_is_recognized_and_metadata_is_read():
    from decompnet.consoles.gba.rom import is_gba, read_info

    rom = _gba_rom()
    assert is_gba(rom)
    info = read_info(rom)
    assert info == {
        "title": "DEMO QUEST",
        "game_code": "DQAE",
        "region": "North America",
        "version": 3,
        "size": len(rom),
        "sha1": hashlib.sha1(rom).hexdigest(),
        "entrypoint": 0x08000000,
        "platform": "gba",
        "byte_order": "gba",
        "compiler_guess": "AGBCC",
        "gp": 0,
    }


def test_gba_header_rejects_invalid_size_branch_fixed_byte_and_checksum():
    from decompnet.consoles.gba.rom import is_gba

    valid = _gba_rom()
    assert not is_gba(valid[:0xBF])
    for offset, value in ((3, 0), (0xB2, 0), (0xBD, valid[0xBD] ^ 1)):
        changed = bytearray(valid)
        changed[offset] = value
        assert not is_gba(changed)


def test_frontend_gba_header_detection_and_invalid_checksum_diagnostic():
    import json
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = root / "src" / "decompnet" / "app" / "static" / "js" / "home.js"
    rom = list(_gba_rom())
    program = f"""
    global.$ = () => ({{ addEventListener: () => {{}}, hidden: false, classList: {{ toggle: () => {{}} }},
      querySelector: () => ({{ style: {{}} }}), firstElementChild: {{ style: {{}} }},
      addEventListener: () => {{}}, onclick: null }});
    global.document = {{ addEventListener: () => {{}} }};
    const fs = require('fs'), vm = require('vm');
    vm.runInThisContext(fs.readFileSync({json.dumps(str(script))}, 'utf8'));
    const bytes = Uint8Array.from({json.dumps(rom)});
    const file = {{ name: 'demo.gba', size: bytes.length }};
    const valid = identifyHead(bytes, file);
    const invalidBytes = bytes.slice(); invalidBytes[0xbd] ^= 1;
    console.log(JSON.stringify({{ valid, invalid: identifyHead(invalidBytes, file), message: notSupported(invalidBytes, file) }}));
    """
    result = subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)
    output = json.loads(result.stdout)
    assert output["valid"]["platform"] == "gba"
    assert output["valid"]["code"] == "DQAE"
    assert output["invalid"] is None
    assert output["message"] is None


def test_gba_console_can_start_roms_and_preserves_public_fork_facts():
    console = consoles.get("gba")
    assert console in consoles.startable()
    assert console.mode == consoles.ROM
    assert console.arch == "arm"
    assert console.m2c_target() == "gba"
    assert console.guides == ("arm",)
    picked = console.read_game(_gba_rom(), "demo.gba")
    assert isinstance(picked, Picked)
    assert (picked.platform, picked.filename, picked.big_endian) == ("gba", "baserom.gba", False)


def test_gba_function_target_uses_arm_and_thumb_metadata():
    from decompnet.consoles.gba.split import GbaFunction
    from decompnet.rom import match

    function = GbaFunction("sub_080000C0", 0x080000C0, 0, b"\x70\x47", "thumb_func_start sub_080000C0\n", 1)
    target = match.target_from_split(function, lambda _: None, False)
    assert (target.arch, target.thumb, target.code) == ("arm", True, b"\x70\x47")


def test_gba_toolchains_are_registered_with_pinned_upstream_sources():
    from decompnet import toolchains

    agbcc = toolchains.get("agbcc")
    luvdis = toolchains.get("luvdis")
    assert agbcc.parts[0]["sha256"] == "bcf0197e3e27d8e08506a142e25fa68ea7f6679a98347a6406804eb3f9ff4d29"
    assert agbcc.requires == ["arm-gnu"]
    assert luvdis.git == "https://github.com/aarant/luvdis"
    assert luvdis.commit == "ec3a4e3ee3afe3e9b4a9c7ae8fffc65e1ea2096c"


def test_gba_candidates_find_returned_leaf_functions():
    from decompnet.consoles.gba.candidates import candidates

    rom = bytearray(0x180)
    rom[0x100:0x104] = bytes.fromhex("70470000")  # bx lr; aligned padding
    rom[0x104:0x108] = bytes.fromhex("70470000")  # leaf function after return
    assert 0x08000104 in candidates(bytes(rom), [0x08000100])


def test_export_build_script_rejects_rom_with_wrong_hash(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    image = tmp_path / "baserom.gba"
    image.write_bytes(_gba_rom())
    (tmp_path / "build.json").write_text(json.dumps({"image": "baserom.gba", "sha1": hashlib.sha1(image.read_bytes()).hexdigest()}))
    script = Path(__file__).resolve().parents[1] / "src" / "decompnet" / "consoles" / "gba" / "build.py"
    copied = tmp_path / "build.py"
    copied.write_text(script.read_text())
    config = json.loads((tmp_path / "build.json").read_text())
    config["sha1"] = "0" * 40
    (tmp_path / "build.json").write_text(json.dumps(config))
    result = subprocess.run([sys.executable, str(copied)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1 and "SHA-1" in result.stderr


def test_demo_build_has_clear_skip_when_gba_tools_are_missing(monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    from decompnet import toolchains

    missing = [tid for tid in ("agbcc", "arm-gnu") if toolchains.installed(tid) is None]
    if missing:
        pytest.skip(f"GBA demo build needs pinned tools: {', '.join(missing)}")
    script = Path(__file__).resolve().parents[1] / "projects" / "demo-gba" / "build.py"
    subprocess.run([sys.executable, str(script)], check=True, timeout=120)
    assert (script.parent / "build" / "demo.gba").is_file()


def test_demo_rom_splits_with_pinned_luvdis_when_available(tmp_path):
    import pytest

    from decompnet import toolchains
    from decompnet.consoles.gba.split import split

    if toolchains.installed("luvdis") is None:
        pytest.skip("GBA split test needs pinned luvdis")
    image = __import__("pathlib").Path(__file__).resolve().parents[1] / "projects" / "demo-gba" / "build" / "demo.gba"
    if not image.exists():
        pytest.skip("GBA demo ROM has not been built (needs agbcc and arm-gnu)")
    result = split(image, tmp_path, log=lambda *_: None)
    assert len(result.functions) >= 2
    assert all(function.vram >= 0x08000000 and function.size > 0 for function in result.functions)


def test_demo_gba_imports_as_rom_project_when_tools_are_available(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    from decompnet import toolchains
    from decompnet.app.engine import RomNetwork

    missing = [tid for tid in ("agbcc", "arm-gnu", "luvdis") if toolchains.installed(tid) is None]
    if missing:
        pytest.skip(f"GBA import demo needs pinned tools: {', '.join(missing)}")
    demo = Path(__file__).resolve().parents[1] / "projects" / "demo-gba"
    subprocess.run([sys.executable, str(demo / "build.py")], check=True, timeout=120)
    rom = demo / "build" / "demo.gba"
    net = RomNetwork(tmp_path / "decomp.db", tmp_path)
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    overview = net.overview(slug)
    assert overview["platform"] == "gba" and overview["compiler"]["toolchain"] == "agbcc"
    assert overview["functions"] >= 2
    assert all(row["vram"] >= 0x08000000 for row in net.store.all("SELECT vram FROM functions"))
    exported = net.export_rom(slug, log=lambda *_: None)
    assert exported["identical"], exported.get("log")


def test_gba_matcher_accepts_exact_thumb_object_and_rejects_changed_bytes(tmp_path):
    import subprocess

    from decompnet import toolchains
    from decompnet.consoles.gba.split import GbaFunction
    from decompnet.rom import match, match_arm
    from decompnet.rom.symbols import Symbol, SymbolTable

    if toolchains.installed("arm-gnu") is None:
        import pytest

        pytest.skip("ARM GNU assembler is required for Thumb matcher fixture")
    assembler = toolchains.ensure("arm-gnu") / "bin" / "arm-none-eabi-as"
    source = tmp_path / "one.s"
    obj = tmp_path / "one.o"
    source.write_text(".syntax unified\n.thumb\n.text\n.global test_fn\n.thumb_func\ntest_fn:\n bx lr\n", encoding="utf-8")
    subprocess.run([str(assembler), "-mcpu=arm7tdmi", "-o", str(obj), str(source)], check=True)
    code = bytes.fromhex("7047")
    fn = GbaFunction("test_fn", 0x08000100, 0x100, code, "thumb_func_start test_fn\n", 1)
    target = match.target_from_split(fn, lambda _: None, False)
    table = SymbolTable([Symbol("test_fn", fn.vram, len(code), "func")])
    read = lambda addr, length: code[addr - fn.vram:addr - fn.vram + length] if fn.vram <= addr < fn.vram + len(code) else None
    linked = match.link(obj.read_bytes(), "test_fn", target, table, read)
    assert linked.exact
    wrong = match_arm.ArmTarget("test_fn", fn.vram, bytes.fromhex("00df"), True)
    assert not match_arm.link(obj.read_bytes(), "test_fn", wrong, table, read).exact
