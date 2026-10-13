# decomp.wtf

**What The Function.** A free, open-source app that lets the AI agent you already use decompile games you own into
C that compiles back to the exact same bytes, while you watch. Everything runs on your computer.

```bash
curl -fsSL https://decomp.wtf/install.sh | bash        # macOS, Linux
irm https://decomp.wtf/install.ps1 | iex               # Windows (PowerShell)
```

The installer gets uv if needed, installs this package (`uv tool install`), runs `decomp setup` (pinned, checksummed
compilers and m2c; IDO is proprietary and only installed from the app if you accept that), adds an app launcher and
starts `decomp`, which serves the GUI on 127.0.0.1 and opens your browser.

## How it works

```
 your ROM ──▶ splat / spimdisasm ──▶ every function and data symbol ──▶ SQLite (claims, attempts, notes, context.h)
                                                                       ▲
 Claude Code · Codex · Gemini CLI · opencode ──MCP (127.0.0.1)─────────┤   built-in loop for OpenAI-compatible APIs
                                                                       ▼
 compile with the game's compiler ──▶ link at the function's address ──▶ compare with the ROM (score 0 = match)
```

- `src/decompnet/rom/`: ROM header and byte order (`n64.py`), PlayStation executables and discs (`psx.py`,
  in progress), splitting with splat (`split.py`), the symbol table (`symbols.py`), and the matcher (`match.py`): a
  candidate is *linked* at the target address (names resolve through the symbol table; data the candidate defines is
  placed where the ROM keeps it and checked byte for byte), so a match is byte-identical code.
- `src/decompnet/app/`: `engine.py` (ROM projects: import, split_function, rename_symbol by address, read_data,
  xrefs, export), `tools.py` (MCP tools and the agent prompt), `agents.py` (detects and runs CLI agents headless,
  parses their output, built-in API agent), `server.py` (GUI API + MCP, localhost only, per-launch secret),
  `collab.py` (rooms: a host shares a project with friends who own the same game over a quick tunnel; only work is
  shared), `static/` (the GUI), `guide/` (know-how agents read with `guide(topic)`).
- `projects/demo-rom/`: builds Demo Quest 64, a real N64 ROM image from CC0 code and zlib. It ships with the app, and
  `tests/test_rom.py` proves the matcher on it: every function of the original source matches the ROM.
- `site/`: the website (Cloudflare Workers static assets). `site/tools/build_sim.py` makes the homepage simulation's
  data from the demo ROM; `site/build-legal.py` renders the legal pages.

The repository also still contains `decompnet` (`src/decompnet/server.py`, `network.py`, `cli.py`), an earlier
multi-agent coordinator the app's engine is built on. decomp.wtf doesn't run a hosted service.

## Develop

```bash
uv sync
uv run decomp            # projects in ~/Decomp (or DECOMP_HOME)
uv run pytest
uv run python projects/demo-rom/build.py   # rebuild the demo ROM (needs gcc-2.7.2-kmc and ld.lld)
```

## Use it responsibly

decomp.wtf contains no game code and doesn't provide ROMs. Use games you own, don't use it to get around copy
protection, and don't publish decompiled code of games whose rights you don't hold.

MIT license. Not affiliated with Nintendo or Sony.
