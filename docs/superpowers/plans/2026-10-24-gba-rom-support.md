# GBA ROM Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore full GBA ROM support in decompdecomp, from ROM selection and function matching through byte-identical export/rebuild, while preserving public GBA forks.

**Architecture:** Add GBA as a ROM-mode console package and route its special ROM reader/splitter through the console connector. Reuse shared ROM matching and export infrastructure where possible, adding only the GBA-specific assembly export/build path necessary to preserve the prior rebuildable project behavior.

**Tech Stack:** Python 3.12+, pytest, Ruff, pinned toolchains, luvdis, ARM GNU binutils, plain JavaScript UI.

---

## File Map

- Create `src/decompnet/consoles/gba/__init__.py` for ROM recognition, metadata, compiler facts, custom split hook, and GBA console registration facts.
- Create `src/decompnet/consoles/gba/candidates.py` and any narrowly needed GBA-only helper modules by porting the verified prior implementation; these own function-start candidates, literal pools, references, and Thumb boundaries.
- Modify `src/decompnet/consoles/__init__.py` to register the startable GBA console without removing it from `public.PUBLIC_ONLY`; ensure a single `gba` Console instance supports both ROM config lookups and public fork metadata.
- Create/modify `src/decompnet/toolchains.toml` and GBA console guide files only if required by the prior toolchain definitions and export assembler. Reuse existing `agbcc`, `arm-gnu`, and `luvdis` registry entries if present; otherwise port their exact checksummed definitions from the archived project, not guessed URLs or hashes.
- Modify `src/decompnet/rom/export.py` only if its current generic exporter cannot support a GBA-specific path cleanly; otherwise add `src/decompnet/consoles/gba/export.py` and dispatch through a console method/feature rather than a platform conditional.
- Modify `src/decompnet/app/static/js/home.js` and its UI tests to identify/display GBA ROMs and remove the obsolete `.gba` rejection message.
- Modify `tests/test_consoles.py`, create `tests/test_gba.py`, and create a generated demo under `projects/demo-gba/` with a build script and source; generated ROM/object/build outputs must remain ignored.
- Update `ROADMAP.md` and relevant user-facing platform tables in `README.md` / `docs/GUIDE.md` to move GBA from public-only to beta once actual support and its limitations are accurate.

## Implementation Tasks

### Task 1: Restore GBA ROM parsing and console registration

**Files:**
- Create: `src/decompnet/consoles/gba/__init__.py`
- Modify: `src/decompnet/consoles/__init__.py`
- Test: `tests/test_consoles.py`
- Test: `tests/test_gba.py`

- [x] **Step 1: Add failing tests for GBA headers and metadata.** Synthetic header tests cover accepted metadata and invalid size/opcode/fixed byte/checksum.
- [x] **Step 2: Run `uv run pytest tests/test_gba.py -q` and confirm the new imports/functions fail.** The test-first run failed because the GBA package/module was absent; it passes after implementation.
- [x] **Step 3: Add the GBA ROM console with reader/compiler/export facts, sniffing, and ROM reading.** Public fork facts remain on the same `gba` console identity.
- [x] **Step 4: Register one GBA console and remove its duplicate public-only entry.**
- [x] **Step 5: Run parser/registration/console tests.** Focused tests pass; native-tool demo tests skip when unavailable.

### Task 2: Port GBA disassembly and candidate function recovery

**Files:**
- Create: `src/decompnet/consoles/gba/split.py`
- Create: `src/decompnet/consoles/gba/candidates.py`
- Create: `src/decompnet/consoles/gba/luvdis_run.py` only if that adapter is absent from the current dependency/package interface
- Modify: `src/decompnet/consoles/gba/__init__.py`
- Test: `tests/test_gba.py`

- [x] **Step 1: Add splitter tests for function candidates and executable integration.** Candidate recovery and local luvdis split tests pass.
- [x] **Step 2: Verify missing behavior with failing tests before implementation.**
- [x] **Step 3: Port candidate recovery, luvdis execution, ARM/Thumb blocks and references.**
- [x] **Step 4: Adapt the splitter to the shared split result and common importer.**
- [x] **Step 5: Run splitter tests against the generated homebrew demo.**

### Task 3: Connect GBA function data to common import and matching

**Files:**
- Modify: `src/decompnet/consoles/gba/split.py`
- Modify: `src/decompnet/app/engine/importing.py` only where the current common importer contract requires it
- Modify: `src/decompnet/arch/arm.py` or shared ROM matching only when a failing test demonstrates a generic missing ARM/Thumb case
- Test: `tests/test_gba.py`
- Preserve: existing local changes in `src/decompnet/app/engine/importing.py` and `src/decompnet/consoles/__init__.py`

- [x] **Step 1: Add a fixture importing a synthetic GBA project and verifying platform, compiler, function addresses, and function count.**
- [x] **Step 2: Run `uv run pytest tests/test_gba.py -q` and confirm project import expectations fail.** The initial test-first run was red; the completed import test passes with pinned tools.
- [x] **Step 3: Adapt GBA functions to common ROM storage and architecture-dispatched ARM matching.**
- [x] **Step 4: Add exact/mismatching Thumb object tests.**
- [x] **Step 5: Run GBA and MIPS ROM regression tests.**

### Task 4: Restore compiler and disassembler setup facts

**Files:**
- Modify: `src/decompnet/toolchains.toml` only if the registry lacks required exact entries
- Modify: `src/decompnet/consoles/gba/__init__.py`
- Modify: `src/decompnet/seeds.py` only if the existing GBA seed mapping is not already retained
- Test: `tests/test_toolchains.py`
- Test: `tests/test_consoles.py`
- Test: `tests/test_gba.py`

- [x] **Step 1: Assert GBA toolchains are registered and pinned.**
- [x] **Step 2: Verify exact GitHub release checksum and luvdis commit via GitHub API.**
- [x] **Step 3: Register agbcc, arm-gnu, and luvdis; ensure import installs only free pinned tools.**
- [x] **Step 4: Run toolchain and GBA console tests.**

### Task 5: Restore GBA rebuildable export

**Files:**
- Create: `src/decompnet/consoles/gba/export.py` and the GBA export build helper/template files identified by inspecting the archived `export_gba.py` and `export_gba_build.py`
- Modify: `src/decompnet/consoles/gba/__init__.py`
- Modify: `src/decompnet/rom/export.py` and/or `src/decompnet/app/engine/gui.py` only to add a console-dispatched export hook, with no `platform == "gba"` branching
- Test: `tests/test_gba.py`

- [x] **Step 1: Test GBA export dispatch and build metadata.**
- [x] **Step 2: Verify export integration behavior with synthetic project.**
- [x] **Step 3: Add console-dispatched export and a pinned-toolchain build adapter with assembly/object placement.**
- [x] **Step 4: Run export and whole-ROM byte identity on the generated homebrew ROM.**
- [x] **Step 5: Run GBA export tests.**

### Task 6: Enable GBA selection in the UI

**Files:**
- Modify: `src/decompnet/app/static/js/home.js`
- Modify: `tests/test_gui.py` and/or relevant UI tests

- [x] **Step 1: Test frontend recognition of valid GBA headers and invalid-header handling.**
- [x] **Step 2: Reproduce the signed JavaScript opcode-mask comparison bug.**
- [x] **Step 3: Add GBA header recognition returning platform `gba`, ROM format/title/code/region/entry facts and remove obsolete unsupported text.**
- [x] **Step 4: Run GBA UI/header and console tests; verify the active dev server serves the corrected frontend.** Other full-suite errors were unrelated missing zlib N64 fixture assets.

### Task 7: Add a homemade demo and end-to-end regression coverage

**Files:**
- Create: `projects/demo-gba/build.py`, minimal source/header/linker files
- Modify: `tests/test_consoles.py`
- Create: `tests/test_gba.py`
- Modify: `.gitignore` or project ignore rules only if generated ROM/build artifacts are not already ignored

- [x] **Step 1: Add demo tests that skip clearly when agbcc, ARM GNU, or luvdis are unavailable.**
- [x] **Step 2: Verify the demo build/import path using installed pinned tools.**
- [x] **Step 3: Create a synthetic homebrew GBA ROM from local source; outputs remain under ignored `projects/demo-gba/build/`.**
- [x] **Step 4: Exercise import, discovery, exact/mismatch ARM matching, export, and byte-identical rebuild.** Tool-dependent cases skip cleanly when tools are missing.
- [x] **Step 5: Run GBA and console tests.**

### Task 8: Update support status and run full verification

**Files:**
- Modify: `ROADMAP.md`
- Modify: `README.md`
- Modify: `docs/GUIDE.md`
- Test: full test suite and Ruff

- [x] **Step 1: Update support tables and roadmap for beta GBA ROM support and export.**
- [x] **Step 2: Run focused tests:** GBA, console, ROM data and reimport tests pass.
- [x] **Step 3: Run lint:** `uv run ruff check src tests projects` passes.
- [x] **Step 4: Run full suite:** 605 passed, 105 skipped; 60 setup errors are due to missing ignored zlib-N64 source (`projects/zlib-n64/original/adler32.c`).
- [x] **Step 5: Review worktree.** Generated ROM/build files are ignored; the provided personal ROM was not accessed.

## Self-Review Against Spec

- ROM recognition and metadata: Task 1 and Task 6.
- Function splitting, uncalled-code recovery, Thumb references and errors: Task 2.
- Common import, ARM matching, compiler facts and fork compatibility: Tasks 3 and 4.
- Rebuildable export and byte-identical validation: Task 5 and Task 7.
- No game data in repository, roadmap/docs, broad checks and preservation of local edits: Tasks 7 and 8.
- Tool definitions must be ported from verifiable pinned definitions, not guessed; task 4 explicitly enforces this constraint.
