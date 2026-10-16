# AGENTS.md

How to work in this repository. Read README.md for what decompdecomp does from a user's point of view.

## What this is

decompdecomp: AI agents decompile games the user owns into C that compiles, with the game's original compiler, to
the exact same bytes. Python 3.12+, managed with uv. A continuation of decomp.wtf (Sam Wolfs, i12bp8/decomp.wtf,
archived at 0.10.6): `origin` is n-delic/decompdecomp, `upstream` the archived original.

Naming: the project is **decompdecomp** in everything users read. Internal names stay as decomp.wtf left them, so
existing installs and projects keep working: the `decomp` command, the `decompnet` package and data folder,
`~/Decomp`, `.decomp.wtf.json` manifests, `.decomp-wtf/` state, the `decomp-wtf` git branch, WSL distribution and
services. Installs and updates come from GitHub (`REPO` in app/updates.py and the installers); the decomp.wtf domain
isn't ours, so nothing may point at it. `site/` is the website (decomp.ndelic.com); `site/public/` also holds the installers, which updates read from GitHub.

## Commands

```bash
uv sync
uv run decomp                 # the app (GUI in the browser); projects live in ~/Decomp or $DECOMP_HOME
uv run decomp mcp             # the MCP server agents connect to
uv run pytest -q              # the test suite; many tests skip when a compiler or tool isn't installed
uv run pytest tests/test_forks.py -q   # one file
```

Compilers and tools (splat, objdiff, dtk, wibo, Ghidra, period compilers) are pinned with checksums in
`src/decompnet/toolchains.toml` and installed by `src/decompnet/toolchains.py`, never taken from PATH. Proprietary
compilers are only installed after the user accepts that in Setup; don't add code that fetches them silently.

## Two ways a game is decompiled

1. **ROM mode** (N64, PS1, PS2, GBA): `src/decompnet/rom/` splits the game (splat/spimdisasm, luvdis), and
   `rom/match.py` (MIPS) / `rom/match_arm.py` (ARM) *link* each compiled candidate at the function's real address
   and compare bytes. Driven by `app/engine.py`. Exported as a rebuildable tree by `rom/export*.py`.
2. **Fork mode** (public decomps forked by repository address, and GameCube/Wii/DS/PC started from scratch): a community-shaped
   project on disk with an `objdiff.json`; objdiff judges each unit. `forks/project.py` detects and builds projects,
   `app/forkengine.py` drives them. From-scratch importers generate such a project:
   `forks/scratch_dtk.py` (GameCube/Wii, dtk-template), `forks/scratch_dsd.py` (DS, ds-decomp, template in
   `forks/ds_template/`) and `forks/scratch_pe.py` (Windows PC: Ghidra finds functions, decompdecomp delinks them
   into COFF objects itself, template in `forks/pc_template/`). A new from-scratch platform that has a
   delinker/objdiff story belongs here. Fork-mode helpers that read objects with pyelftools must tolerate COFF
   (PC, Xbox): treat an `ELFError` as "no verdict".

Shared pieces: `network.py` (claims, attempts, notes, shared header), `compiler.py` (sandboxed compiler runs),
`scoring.py` (capstone disassembly and diffs for every arch), `elfobj.py` (ELF objects), `app/tools.py` (the MCP
tools and the agent prompt), `app/guide/*.md` (know-how agents read through `guide(topic)`), `app/static/` (GUI).

## Conventions

- Match the surrounding code: plain-English docstrings and comments that say *why*, often citing the game or issue
  that motivated a rule ("#22: ..."). No new dependencies for a few lines; capstone, pyelftools, httpx are there.
- Heavy external tools run through `limits.capped(...)` (memory cap) with a timeout.
- Errors shown to the user or agent are `ForkError` / `NetworkError` with a sentence they can act on.
- Every behavior change gets a test in `tests/`. Tests that need a real game or a proprietary compiler skip when it
  isn't there; small homemade games/objects live in `projects/demo-*` and are built by their own scripts.
- Releases bump the version in five places together: `pyproject.toml`, `src/decompnet/__init__.py`,
  `site/public/install.sh`, `site/public/install.ps1`, `uv.lock`. Commit subjects read
  `decompdecomp X.Y.Z: <what users get, in one sentence>`, with a body explaining why.

## Never

- Commit, download or publish game data (ROMs, discs, executables, extracted code or assets). Generated projects
  keep them out of git (`orig/`, `extract/`, `build/`).
- Let agents edit a project's build setup: `forks/project.py` (`EDITABLE`, `FORBIDDEN_PARTS`) limits them to
  source and header files on purpose.
- Weaken the judge: a match means byte-identical after linking (ROM mode) or objdiff 100% (fork mode).
