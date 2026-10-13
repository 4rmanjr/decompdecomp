"""Generate site/public/data/demo.json for the landing page.

Everything the page shows about the demo game is real: bytes and disassembly come from
projects/demo/target/game.o, and every score comes from DecompNet's own compiler + scorer,
compiling the plausible-but-wrong attempts below and the original source.

Run from the repo root:  uv run python site/tools/build_data.py [project-dir]   (default: projects/demo-n64)
"""

import json
import re
import sys
from pathlib import Path

from decompnet import scoring, toolchains
from decompnet.compiler import compile_source
from decompnet.elfobj import extract_function
from decompnet.project import compiler_config, load_config

ROOT = Path(__file__).resolve().parents[2]
DEMO = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "projects" / "demo-n64"
OUT = ROOT / "site" / "public" / "data" / "demo.json"
ALIGN = 16

# Wrong-but-plausible attempts an agent might make on the way to a match.
VARIANTS = {
    "clamp_s32": [
        "s32 clamp_s32(s32 v, s32 lo, s32 hi) {\n    if (v > hi) return hi;\n    if ((u32)v < (u32)lo) return lo;\n    return v;\n}",
        "s32 clamp_s32(s32 v, s32 lo, s32 hi) {\n    s32 r = v;\n    if (r > hi) r = hi;\n    if (r < lo) r = lo;\n    return r;\n}",
        "s32 clamp_s32(s32 v, s32 lo, s32 hi) {\n    if ((u32)v < (u32)lo) return lo;\n    if (v > hi) return hi;\n    return v;\n}",
    ],
    "abs_s32": [
        "s32 abs_s32(s32 v) {\n    return (v ^ (v >> 31)) - (v >> 31);\n}",
        "s32 abs_s32(s32 v) {\n    s32 m = v >> 31;\n    return (v + m) ^ m;\n}",
    ],
    "player_take_damage": [
        "void player_take_damage(s32 amount) {\n    if (g_player.invuln_timer > 0) return;\n    g_player.invuln_timer = 60;\n    g_player.hp -= amount;\n    if (g_player.hp <= 0) {\n        g_player.hp = 0;\n        g_player.state = PLAYER_STATE_DEAD;\n    }\n}",
        "void player_take_damage(s32 amount) {\n    s32 hp;\n    if (g_player.invuln_timer > 0) return;\n    hp = g_player.hp - amount;\n    if (hp <= 0) {\n        g_player.state = PLAYER_STATE_DEAD;\n        hp = 0;\n    }\n    g_player.hp = hp;\n    g_player.invuln_timer = 60;\n}",
        "void player_take_damage(s32 amount) {\n    if (g_player.invuln_timer != 0) return;\n    g_player.hp -= amount;\n    if (g_player.hp < 0) {\n        g_player.hp = 0;\n        g_player.state = PLAYER_STATE_DEAD;\n    }\n    g_player.invuln_timer = 60;\n}",
    ],
    "rng_next": [
        "u32 rng_next(void) {\n    g_rng_state = g_rng_state * 1103515245 + 12345;\n    return ((s32)g_rng_state >> 16) & 0x7FFF;\n}",
        "u32 rng_next(void) {\n    g_rng_state = g_rng_state * 1103515245 + 12345;\n    return (g_rng_state >> 16) & 0xFFFF;\n}",
    ],
    "count_alive_entities": [
        "s32 count_alive_entities(void) {\n    s32 i;\n    s32 n = 0;\n    for (i = MAX_ENTITIES - 1; i >= 0; i--) {\n        if (g_entities[i].flags & ENTITY_FLAG_ACTIVE) n++;\n    }\n    return n;\n}",
        "s32 count_alive_entities(void) {\n    s32 i;\n    s32 n = 0;\n    for (i = 0; i < MAX_ENTITIES; i++) {\n        n += g_entities[i].flags & ENTITY_FLAG_ACTIVE;\n    }\n    return n;\n}",
    ],
    "entity_update_physics": [
        "void entity_update_physics(Entity *e) {\n    e->x += e->vel_x;\n    e->vel_y += GRAVITY;\n    if (e->vel_y > MAX_FALL_SPEED) e->vel_y = MAX_FALL_SPEED;\n    e->y += e->vel_y;\n    if (e->y > FLOOR_Y) {\n        e->y = FLOOR_Y;\n        e->vel_y = 0;\n        e->flags |= ENTITY_FLAG_GROUNDED;\n    }\n}",
        "void entity_update_physics(Entity *e) {\n    s32 vy = e->vel_y + GRAVITY;\n    e->vel_y = vy > MAX_FALL_SPEED ? MAX_FALL_SPEED : vy;\n    e->x += e->vel_x;\n    e->y += e->vel_y;\n    if (e->y > FLOOR_Y) {\n        e->y = FLOOR_Y;\n        e->vel_y = 0;\n        e->flags = ENTITY_FLAG_GROUNDED;\n    }\n}",
    ],
    "entity_default_hp": [
        "s32 entity_default_hp(s16 type) {\n    static const s32 hp[] = { 0, 3, 2, 10, 50, 1 };\n    if (type < 0 || type > ENTITY_COIN) return 0;\n    return hp[type];\n}",
        "s32 entity_default_hp(s16 type) {\n    if (type == ENTITY_SLIME) return 3;\n    if (type == ENTITY_BAT) return 2;\n    if (type == ENTITY_KNIGHT) return 10;\n    if (type == ENTITY_DRAGON) return 50;\n    return type == ENTITY_COIN;\n}",
    ],
    "entity_spawn": [
        "Entity *entity_spawn(s16 type, s32 x, s32 y) {\n    Entity *e;\n    for (e = g_entities; e < &g_entities[MAX_ENTITIES]; e++) {\n        if (e->flags & ENTITY_FLAG_ACTIVE) continue;\n        e->type = type;\n        e->x = x;\n        e->y = y;\n        e->vel_x = 0;\n        e->vel_y = 0;\n        e->flags = ENTITY_FLAG_ACTIVE;\n        e->hp = entity_default_hp(type);\n        return e;\n    }\n    return NULL;\n}",
        "Entity *entity_spawn(s16 type, s32 x, s32 y) {\n    s32 i;\n    for (i = 0; i < MAX_ENTITIES; i++) {\n        Entity *e = &g_entities[i];\n        if (!(e->flags & ENTITY_FLAG_ACTIVE)) {\n            e->flags = ENTITY_FLAG_ACTIVE;\n            e->hp = entity_default_hp(type);\n            e->type = type;\n            e->x = x;\n            e->y = y;\n            e->vel_x = 0;\n            e->vel_y = 0;\n            return e;\n        }\n    }\n    return NULL;\n}",
    ],
    "add_score": [
        "void add_score(s32 points) {\n    g_score += points * g_player.combo + points;\n    if (g_score > MAX_SCORE) g_score = MAX_SCORE;\n}",
        "void add_score(s32 points) {\n    s32 s = g_score + points * (g_player.combo + 1);\n    if (s > MAX_SCORE) s = MAX_SCORE;\n    g_score = s;\n}",
    ],
    "lerp": [
        "f32 lerp(f32 a, f32 b, f32 t) {\n    return a * (1.0f - t) + b * t;\n}",
        "f32 lerp(f32 a, f32 b, f32 t) {\n    return a + (b - a) * (double)t;\n}",
    ],
    "checksum_save": [
        "u32 checksum_save(const u8 *data, s32 len) {\n    u32 sum = 0x1234;\n    while (len-- > 0) {\n        sum = (sum << 5) + sum + *data++;\n    }\n    return sum;\n}",
        "u32 checksum_save(const u8 *data, s32 len) {\n    u32 sum = 0x1234;\n    s32 i;\n    for (i = 0; i < len; i++) {\n        sum = sum * 31 + data[i];\n    }\n    return sum;\n}",
    ],
    "entity_collides": [
        "s32 entity_collides(const Entity *a, const Entity *b) {\n    s32 dx = a->x - b->x;\n    s32 dy = a->y - b->y;\n    if (dx < 0) dx = -dx;\n    if (dy < 0) dy = -dy;\n    return dx < (a->w + b->w) / 2 && dy < (a->h + b->h) / 2;\n}",
        "s32 entity_collides(const Entity *a, const Entity *b) {\n    return abs_s32(a->x - b->x) < (a->w + b->w) >> 1 && abs_s32(a->y - b->y) < (a->h + b->h) >> 1;\n}",
    ],
    "game_tick": [
        "void game_tick(void) {\n    s32 i;\n    if (g_player.invuln_timer > 0) g_player.invuln_timer--;\n    g_frame_count++;\n    for (i = 0; i < MAX_ENTITIES; i++) {\n        Entity *e = &g_entities[i];\n        if (e->flags & ENTITY_FLAG_ACTIVE) {\n            entity_update_physics(e);\n            if (entity_collides(e, &g_player.ent)) {\n                if (e->type == ENTITY_COIN) {\n                    add_score(10);\n                    e->flags = 0;\n                } else {\n                    player_take_damage(1);\n                }\n            }\n        }\n    }\n}",
        "void game_tick(void) {\n    s32 i;\n    g_frame_count++;\n    if (g_player.invuln_timer > 0) g_player.invuln_timer--;\n    for (i = 0; i < MAX_ENTITIES; i++) {\n        Entity *e = &g_entities[i];\n        if (!(e->flags & ENTITY_FLAG_ACTIVE)) continue;\n        entity_update_physics(e);\n        if (!entity_collides(e, &g_player.ent)) continue;\n        if (e->type == ENTITY_COIN) {\n            add_score(10);\n            e->flags &= ~ENTITY_FLAG_ACTIVE;\n        } else {\n            player_take_damage(1);\n        }\n    }\n}",
    ],
}

# The walkthrough: one agent's real attempt sequence on clamp_s32 (in the order it tried them).
WALK_FN = "clamp_s32"
WALK = [
    VARIANTS["clamp_s32"][1],
    VARIANTS["clamp_s32"][0],
    "s32 clamp_s32(s32 v, s32 lo, s32 hi) {\n    if (v <= hi) {\n        if (v < lo) return lo;\n        return v;\n    }\n    return hi;\n}",
    VARIANTS["clamp_s32"][2],
    None,  # the original source
]


def source_functions(src: str) -> dict[str, str]:
    return {m.group(1): m.group(0).strip()
            for m in re.finditer(r"^[A-Za-z][\w \*]*?\b(\w+)\([^)]*\)\s*\{\n.*?^\}\n", src, re.S | re.M)}


def main() -> None:
    cfg = load_config(DEMO)
    cc = compiler_config(DEMO, cfg)
    header = (DEMO / "original" / "game.h").read_text()
    target = (DEMO / "target" / "game.o").read_bytes()
    originals = source_functions((DEMO / "original" / "game.c").read_text())

    def attempt(name, src):
        res = compile_source(cc, header, src)
        if not res.ok:
            raise SystemExit(f"{name}: attempt does not compile:\n{res.log}\n{src}")
        return scoring.diff(extract_function(target, name), extract_function(res.obj, name))

    def listing(fn):
        insns = scoring.disassemble(fn)
        ends = [i.offset for i in insns[1:]] + [len(fn.code)]
        return [[i.offset, fn.code[i.offset:end].hex(), i.text] for i, end in zip(insns, ends)]

    functions, offset = [], 0
    for name, src in originals.items():
        fn = extract_function(target, name)
        final = attempt(name, src)
        assert final.bytes_match, f"original {name} does not match?"
        scores = sorted({d.score for d in (attempt(name, v) for v in VARIANTS.get(name, [])) if not d.bytes_match},
                        reverse=True)
        asm = listing(fn)
        functions.append({"name": name, "offset": offset, "size": len(fn.code), "insns": len(asm),
                          "hex": fn.code.hex(), "asm": asm, "src": src, "attempts": scores})
        print(f"{name:24} {len(fn.code):4} bytes  attempts {scores}")
        offset += -(-len(fn.code) // ALIGN) * ALIGN

    walk = []
    for n, src in enumerate(WALK, 1):
        src = src or originals[WALK_FN]
        d = attempt(WALK_FN, src)
        walk.append({"n": n, "src": src, "score": d.score, "pct": round(d.percent, 1),
                     "match": d.bytes_match, "rows": [list(r) for r in d.rows]})
    print("walkthrough scores:", [w["score"] for w in walk])

    total_bytes = sum(f["size"] for f in functions)
    tc_id = cfg["compiler"].get("toolchain")
    tc = toolchains.get(tc_id) if tc_id else None
    arch = extract_function(target, functions[0]["name"]).arch
    data = {
        "game": {"name": cfg["name"], "slug": cfg["slug"], "arch": arch, "bytes": total_bytes,
                 "platform": tc.platform if tc else "PC", "compiler": tc.name.split(" (")[0] if tc else "GCC (modern)",
                 "functions": len(functions), "align": ALIGN, "total": offset},
        "functions": functions,
        "walkthrough": {"function": WALK_FN, "max": next(f for f in functions if f["name"] == WALK_FN)["insns"] * 100,
                        "asm": next(f for f in functions if f["name"] == WALK_FN)["asm"], "attempts": walk},
    }
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes): {len(functions)} functions, "
          f"{total_bytes} bytes, laid out in {offset}")


if __name__ == "__main__":
    main()
