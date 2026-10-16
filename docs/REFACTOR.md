# The tidy-codebase refactor: what's done, and the two steps left

This file is the handoff for the `tidy-codebase` branch. It's written for the next person or agent who picks the
work up, so it says everything: why each step exists, what exactly to do, in which order, the commands, the numbers
measured when this was written (2026-10-16, on commit `24b12d5`), what must not change, how to check the work, and
how to commit it. Read [AGENTS.md](../AGENTS.md) first: everything there applies here too, above all the section
"Never disturb the user's own decompdecomp".

When both steps are done and merged, delete this file (its content lives on in `docs/CONVENTIONS.md` and in git).

## Why this refactor

decompdecomp was written fast, mostly by agents, and it shows in a few places that make it hard for a *human* to
find their way: one function of 1,039 lines, one class of 150 methods in one 2,400-line file, two products in one
repository, no linter, and a 2,779-line JavaScript file. The code itself is in good shape (1,299 functions averaging
16 lines, docstrings that explain *why*, about 260 tests). The goal is that a newcomer can open the repository, find
the part they care about in a minute, and know the house rules without reverse-engineering them.

The rules for every step:

- **No behaviour changes.** A refactor commit moves, splits or renames; it never changes what the app does. If you
  find a bug while moving code, write it down (an issue, or a separate commit after the move), don't fix it inside
  the move: a reviewer must be able to check a move by seeing that the code is the same.
- **Move code verbatim.** Keep comments, docstrings and blank lines. The scripts used for steps 1 and 2 cut the
  source by line ranges (from the abstract syntax tree) for exactly this reason.
- **Tests stay green after every commit**, run the way AGENTS.md says (a scratch environment, a throwaway
  `DECOMP_HOME`), and the full suite, not a subset: `UV_PROJECT_ENVIRONMENT=<scratch venv> uv run pytest -q`.
  At `24b12d5`: 259 passed, 47 skipped (skips are tests whose compiler or tool isn't installed).
- **One commit per step**, with a subject in the house style (what users or contributors get, in one sentence) and
  a body explaining why.

## Done

1. **The server's routes** (`c1be5a0`). `app/server.py` was one `build()` of 1,039 lines with every API route as a
   closure inside it. Now `build()` (164 lines) sets up what routes share and hands it as an `App` object
   (`app/routes/__init__.py`) to one module per part of the app: `routes/pages.py`, `state.py`, `projects.py`,
   `forks.py`, `setup.py`, `life.py` (updates, the window, quitting), `rooms.py`. Shared helpers (jobs, settings,
   `_static`, ...) moved to `app/support.py`. Each route module starts with a line of local aliases
   (`net, mcp, guard = app.net, app.mcp, app.guard`) so the moved code didn't change.
2. **The engine** (`ca3de72`). `app/engine.py` (2,393 lines, the class `RomNetwork` with 150 methods) is now the
   package `app/engine/`, a mixin per topic: `core`, `importing`, `judging`, `briefs`, `compilers`, `editing`,
   `rooms`, `gui`, `autopilot`, plus `common.py` (constants and helpers). Methods were grouped by what they do, not
   by the old section headers (which had stopped matching their contents). `from .engine import ...` works as before.
3. **The public coordinator** (`24b12d5`). decomp.wtf's original internet coordinator (`decompnet` command, its
   server, public API, volunteer workers, hash-only projects, moderation commands) is removed; about 1,100 lines.

---

## Step 4: a linter, and the conventions written down

### What and why

There is no linter or formatter: style is kept by hand, by humans and agents copying the code around them. That
works until it doesn't, and it can't be checked in CI. This step adds **ruff** as a development tool with a small,
deliberately chosen rule set, fixes what it finds, runs it in CI, and writes the house rules into
`docs/CONVENTIONS.md`, so a newcomer reads them instead of guessing them.

Two things this step is **not**:

- **Not a reformat.** `ruff format` would rewrite 132 of the 144 Python files (measured:
  `uvx ruff@0.14.0 format --line-length 120 --check src tests` → "132 files would be reformatted"). The code has a
  hand-kept style (aligned continuation lines, long one-line dict literals, comments placed after code) that the
  formatter would destroy, and the diff would bury every real change in git blame for years. Do not run
  `ruff format`. Do not add it to CI.
- **Not a style rewrite.** Only rules whose findings are real problems or real inconsistencies go in. Everything
  below was measured on `24b12d5` with ruff 0.14.0.

### 4.1 Measure (so you know you're starting from the same place)

```bash
uvx ruff@0.14.0 check --line-length 120 --target-version py312 --select F --statistics src tests
uvx ruff@0.14.0 check --line-length 120 --target-version py312 --select I --statistics src tests
uvx ruff@0.14.0 check --line-length 120 --target-version py312 --select B --statistics src tests
uvx ruff@0.14.0 check --line-length 120 --target-version py312 --select E,W --statistics src tests
```

What it said at `24b12d5`:

| Rules | Findings | What they are | Verdict |
| :-- | --: | :-- | :-- |
| `F` (pyflakes) | 12 | 7 unused imports (F401), 3 f-strings without placeholders (F541), 2 unused variables (F841) | **adopt**: all real |
| `I` (import order) | 12 | files whose imports aren't in isort order | **adopt**: the new route and engine modules are already sorted this way |
| `E9`, `F63`, `F7`, `F82` | 0 | syntax errors, undefined names | **adopt**: free safety |
| `B` (bugbear) | 43 | 20 `zip()` without `strict=` (B905), 11 closures using a loop variable (B023), 10 unused loop variables (B007), 2 mutable default arguments (B006) | **adopt B006, B008, B023 only**, see below |
| `E501` (line length 120) | 519 | lines over 120 characters | **don't adopt**: see below |
| `E741`, `E731` | 13, 12 | variables named `l`/`I`/`O`, lambdas assigned to names | **don't adopt**: house style uses `s = lambda ...` for tiny local helpers on purpose |
| `UP` (pyupgrade) | 130 | modernisations | **don't adopt** now: churn without a bug behind it |

About line length: the code's width is "about 120", but 535 lines are over 120, 101 over 130, 15 over 140 and 4
over 150. Setting `line-length = 120` and enforcing E501 would mean rewrapping 500 lines for nothing. Set
`line-length = 120` (so ruff's own fixes and isort wrap at 120) but don't select E501. If you want a guard against
the truly long lines, select E501 with `line-length = 150` instead and fix the 4 lines over 150 by hand.

About B023 (closure uses a loop variable): these are the bugs bugbear exists for (a lambda created in a loop that
sees the variable's last value). Each of the 11 must be read: some are fine because the closure runs inside the same
iteration (e.g. passed to `sorted(key=...)`), some may be real bugs. Fix the real ones in a **separate commit**
after the lint commit, each with a test, and mark the fine ones with `# noqa: B023 <why it's fine>`. B905 (zip
strict) is not adopted: the code zips lists of equal length by construction everywhere; adding `strict=False`
twenty times says nothing.

### 4.2 Configure

In `pyproject.toml`, add ruff to the existing development group and configure it:

```toml
[dependency-groups]
dev = [
    "pytest>=9.1.1",
    "ruff==0.14.0",
]

[tool.ruff]
line-length = 120
target-version = "py312"
# the projects' own build scripts and the templates generated projects get are checked too; the site's are not code
extend-exclude = ["site"]

[tool.ruff.lint]
# Only rules that find real problems or real inconsistencies: no formatter, no line length (see docs/CONVENTIONS.md)
select = ["F", "I", "E9", "B006", "B008", "B023"]

[tool.ruff.lint.isort]
known-first-party = ["decompnet"]
```

Then `uv lock` (ruff goes into `uv.lock`; the lock change is part of the same commit) and check the version pin
is in both places. Ruff is a development dependency only: it must not appear in `[project] dependencies`, since
users never run it.

Check the template folders `src/decompnet/forks/ds_template/` and `src/decompnet/forks/pc_template/`: their
`tools/*.py` are copied into generated projects and run there with the user's Python, not imported by decompdecomp.
They may use their own conventions (they're written to run standalone). If ruff complains about them in ways that
don't apply, exclude them with a comment saying why (`extend-exclude`), don't bend them.

### 4.3 Fix what it finds

```bash
uv run ruff check --fix src tests projects   # safe fixes: unused imports, import order, f-strings
uv run ruff check src tests projects         # what's left needs a human
```

For each remaining finding: fix it by hand, or add a `# noqa: <code> <reason>` with a reason a reviewer accepts.
There's one existing invalid noqa that ruff warns about (`app/support.py`: `# noqa: BLE001: a job must end, ...`,
around line 96): ruff wants `# noqa: BLE001` followed by the comment after a space or two, not a colon. Fix its form.

Unused imports in `__init__.py` files that re-export names (`app/engine/__init__.py` re-exports `COMPILERS`,
`SHORT_NAMES`, ... for `from .engine import ...`): those are listed in `__all__`, which ruff respects. Keep `__all__`
up to date if you add re-exports.

Run the full test suite. Commit: `Ruff checks the code for real problems (unused code, import order, undefined
names, closure bugs), in development and CI`, body: why ruff, why this rule set, why no formatter.

### 4.4 CI

There's no CI for tests yet (only `.github/workflows/release.yml` and `site.yml`). Add
`.github/workflows/check.yml`:

```yaml
# Every push and pull request: ruff, then the tests (the tests that need a compiler or tool skip without it)
name: check
on:
  push:
    branches: [main]
  pull_request:
jobs:
  check:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync
      - run: uv run ruff check src tests projects
      - run: uv run pytest -q
        env:
          BROWSER: "true"
          DECOMP_TRAY: "0"
```

Check before committing it: the tests must pass on a machine without the pinned compilers and without
`ld.lld`/`llvm-objcopy` (CONTRIBUTING.md says the N64 and PS1 demos need them; their tests should skip, verify that
they do and don't fail). If some test fails only because a tool is missing, make it skip with the reason (that's
the house rule already: "tests that need a real game or a proprietary compiler skip when it isn't there"), in a
separate commit. The suite takes about 3 minutes locally; on GitHub's runner expect 5–8.

Note for whoever pushes: the user pushes only after work hours and restamps commit dates first (see the user's
instructions in the conversation that started this, and `github-activity-after-work` in their memory). Don't push
yourself.

### 4.5 Write `docs/CONVENTIONS.md`

This is the important half of the step. AGENTS.md has a short "Conventions" list; CONVENTIONS.md is the full text a
human reads once and then knows how this codebase is written. Link it from AGENTS.md ("Conventions: read
docs/CONVENTIONS.md"), CONTRIBUTING.md (in "Working on the code") and the GUIDE's development section, and shorten
AGENTS.md's own list to a summary that points there (AGENTS.md is read by agents on every task: keep it short).

Write it in the house style for docs: short sentences, plain English, one idea per sentence, examples from the real
code (with `file:line` references the reader can open). It must cover, at least, these sections; for each, the
points listed are what to say, with where in the code to find the example.

**1. Where code goes.** A map from "I want to change X" to the file:

- importing a game from scratch: `rom/` (N64, PS1, PS2, GBA: splat, luvdis), `forks/scratch_*.py` (GameCube/Wii,
  DS, PC);
- the judge: `rom/match.py` (MIPS), `rom/match_arm.py` (ARM) for ROM mode; objdiff for fork mode
  (`forks/project.py`, `app/forkengine.py`);
- what agents can do: MCP tools in `app/tools.py` (both the MCP definition and the `HANDLERS` table), the API
  agents' schema in `app/agents.py` (`api_tool_schemas`), the work itself in `app/engine/<topic>.py` (ROM) or
  `app/forkengine.py` (forks);
- what agents read about a function: `app/engine/briefs.py`;
- compilers and ranges: `app/engine/compilers.py`, the compiler map in `compilermap.py`;
- C++ classes: `classes.py`;
- the GUI's API: `app/routes/<part>.py`, the GUI itself `app/static/` (no build step);
- know-how agents read: `app/guide/*.md` (served by `guide(topic)`);
- toolchains: `toolchains.toml` (pinned with checksums) and `toolchains.py`;
- a new platform: say what has to exist (an importer, a judge path, `PLATFORM_COMPILERS` and `PLATFORM_STATUS` in
  `app/engine/common.py`, a demo project in `projects/demo-*`, the platform table in ROADMAP.md).

Every new feature adds a row to (or a tick in) the table "What works where" in ROADMAP.md.

**2. Names.** The project is decompdecomp in everything users read; internal names stay as decomp.wtf left them
(`decomp`, `decompnet`, `~/Decomp`, `.decomp.wtf.json`, `.decomp-wtf/`, the `decomp-wtf` branch and WSL distro)
because existing installs depend on them. Python: `snake_case` functions, `CapWords` classes, module constants in
`UPPER_CASE` (`SHORT_NAMES`, `RANGE_PROOF_BYTES`), a leading underscore for what's private to a module or class.
Names say what a thing is in the domain's words (`claim`, `attempt`, `target`, `twin`, `range`), not in
implementation words. Mixins in the engine are `<Topic>Mixin`.

**3. Comments and docstrings.** The single most distinctive rule here: comments and docstrings say *why*, in plain
English, and often cite the game or the incident that caused the rule ("GTA San Andreas: agents said EE-GCC 2.96,
then 2.95.3", `app/engine/compilers.py`; "#22: ..." for an issue). They don't restate the code. Module docstrings
say what the module is for and how it fits with the rest (see `compilermap.py`, `classes.py`, `app/twins.py`).
Every public function longer than a few lines gets a docstring. A shortcut with a known limit gets a comment
`shortcut: <the limit>, <when to upgrade>`.

**4. Text users and agents read.** Errors are `NetworkError` (ROM mode and shared) or `ForkError` (fork mode) with a
sentence the reader can act on: what's wrong and what to do ("say which compiler built those ranges
(compiler=...)"). Never a bare exception text. Agent-facing tool results are plain text, written for an LLM to act
on. GUI text: short, plain, no jargon the user didn't bring. Docs (README, GUIDE) follow ASD-STE100-style simplified
English: short sentences, one instruction per sentence.

**5. Errors and robustness.** Catch the exceptions you expect and can say something about; a broad `except
Exception` is only for code that must never stand between the user (or an agent) and their work — a heuristic like
the class recovery, a background job — and then it logs with `logging.getLogger("decompdecomp").exception(...)`
and says so in a comment (`app/engine/briefs.py`, `_class_brief`). Background jobs always end, with done or an
error that says why (`app/support.py`, `Jobs`).

**6. External tools and safety.** Compilers and tools come only from the pinned toolchains (`toolchains.toml`),
never from PATH. Proprietary compilers install only after the user accepts in Setup. Heavy tools run through
`limits.capped(...)` with a timeout. Agents may edit only source and header files (`forks/project.py`: `EDITABLE`,
`FORBIDDEN_PARTS`). The judge is never weakened: a match is byte-identical after linking (ROM mode) or objdiff 100%
(fork mode). No game data in git, tests, issues or screenshots.

**7. Dependencies.** No new dependency for a few lines of code; capstone, pyelftools, httpx, rabbitizer are there.
A new runtime dependency needs a reason in its commit body. Development tools go in the `dev` group.

**8. Concurrency.** The server is async (Starlette under the MCP server); blocking work goes through
`app.call(...)` (a thread) in routes, never directly in an `async def`. Long work is a background job (`Jobs.start`)
the GUI follows, or a thread with its own state dict (autopilot, the compiler survey). Shared state is guarded where
two threads write it (`threading.Lock`).

**9. Tests.** Every behaviour change has a test in `tests/`. Tests that need a compiler, a tool or a real game skip
when it isn't there, with the reason. Homemade games and objects live in `projects/demo-*` and are built by their own
scripts; hand-assembled machine code in a test is fine for small things (`tests/test_classes.py`,
`tests/test_compilermap.py`). Tests never reach the user's app: `tests/conftest.py` sets a throwaway `DECOMP_HOME`,
`BROWSER=true` and `DECOMP_TRAY=0`. Name tests as a sentence of behaviour
(`test_an_install_from_main_updates_on_releases_only`).

**10. Never disturb the user's app.** Copy AGENTS.md's section and expand it: why (a newer `decomp` replaces an older
running app; the user's agents run there), how (a throwaway `DECOMP_HOME`, `UV_PROJECT_ENVIRONMENT`, read the live
database only with `?mode=ro`, work on copies made with `sqlite3 ... ".backup"`).

**11. Commits and releases.** Subjects say what users get, in one sentence; the body says why. Release commits:
`decompdecomp X.Y.Z: ...`, the version bumped in five places (pyproject.toml, `src/decompnet/__init__.py`,
`site/public/install.sh`, `site/public/install.ps1`, uv.lock). Pre-releases are `X.Y.Z-alpha`. A release is a
`vX.Y.Z` tag; the release workflow builds the .deb and the AppImage.

**12. Linting.** What ruff checks, how to run it (`uv run ruff check src tests projects`), that there's no formatter
and why, and how to write a `# noqa` (with the reason).

Commit: `The conventions are written down in docs/CONVENTIONS.md: where code goes, names, comments, errors, tests`.

### 4.6 Done when

- `uv run ruff check src tests projects` passes with the configured rules, and CI runs it and the tests.
- `docs/CONVENTIONS.md` exists, covers the twelve sections, and AGENTS.md, CONTRIBUTING.md and the GUIDE link to it.
- The full test suite passes (same count as before, give or take tests you deliberately added).
- No file was reformatted beyond what the fixes needed (`git diff --stat` shows small changes per file).

---

## Step 5: split `app.js`

### What and why

The whole GUI is one classic script, `src/decompnet/app/static/app.js`: 2,779 lines, 142 top-level functions, all
sharing globals (`S`, `$`, `api`, `colors`, `geo`, ...). There is no build step on purpose (AGENTS.md: "app/static/
(GUI)"; the app is a Python package, and a JavaScript toolchain would be a second ecosystem to install and pin). The
goal is to split it into a handful of files by screen, so a person fixing the function drawer opens a 300-line file
about the function drawer, without introducing a bundler, modules, or behaviour changes.

### 5.1 How the GUI is loaded (read this before moving anything)

- `static/index.html` loads one script at the end of `<body>`: `<script src="/assets/app.js"></script>` (line 488
  at `24b12d5`).
- `/assets/{name:path}` is served by `routes/pages.py` from the package's `static/` folder through
  `support._static(name)`, which refuses `..` and absolute paths and reads `importlib.resources` — any file under
  `static/` is served, subfolders included. Nothing in Python needs to change to serve more scripts.
- The page has a strict Content Security Policy (`index.html` line 6): `script-src 'self'`. Several same-origin
  scripts are fine; inline scripts and `eval` are not. Don't add inline `<script>` blocks or inline `style=`
  attributes (the CSP forbids inline styles too; the code sets widths through the DOM, see the comment at the
  sidebar section).
- The package ships `static/` through `importlib.resources`; check `pyproject.toml`'s build backend includes new
  files under `static/` (it includes the whole package directory today; verify with
  `uv build --wheel` and `unzip -l dist/*.whl | grep static/`).

### 5.2 The approach: classic scripts in a fixed order, no modules

Keep the code as classic (non-module) scripts sharing globals, split into files loaded in order. This is the
smallest change that works: no `import`/`export` to add, no change to how functions reference each other, and a
moved function means the same thing in its new file. ES modules were considered and rejected for this step: every
cross-file reference would need an import, top-level `const`/`let` would stop being shared, and the move would no
longer be verbatim.

Rules for the split:

- **Order matters only for top-level code that runs at load time**, not for function declarations (a function
  declared in a later script can be called from an earlier one once everything has loaded). But `const` and `let`
  at the top level of a classic script are shared across scripts *and* in the temporal dead zone until their script
  runs, so: shared state and helpers first, screens after, and the code that starts the app (routing on load, the
  first `poll`) last.
- **Top-level statements that run immediately** (event listeners attached with `$("x").addEventListener(...)`,
  `matchMedia(...)` listeners, `const PROGRESS_LEGEND = $("mapLegend").innerHTML` and the like) must stay in a file
  loaded after the DOM exists — they all do today because the script tag is at the end of `<body>`; keep all new
  tags there, in order.
- Function and section comments move with their code.

### 5.3 The proposed files

The section headers in `app.js` (`/* ---- name */`, measured at `24b12d5`) map to files like this. Line numbers are
where each section starts; re-measure before you cut (`grep -n "^/\* ---" src/decompnet/app/static/app.js`).

| New file | Sections of app.js (start line) | Roughly |
| :-- | :-- | --: |
| `static/js/base.js` | the file header and helpers before "theme" (1–62: `api`, `$`, `esc`, percentages, the map's drawing state), theme (63), state + routing (77) | 130 |
| `static/js/sidebar.js` | sidebar (129) | 75 |
| `static/js/home.js` | home, importing a game (204–530) | 330 |
| `static/js/project.js` | project (531: overview, live polling, stats, next steps), team/rooms (929), public decomps (1120) | 730 |
| `static/js/map.js` | map (1262), including the compiler map view | 210 |
| `static/js/agents.js` | now working (1471), agents on this project (1505), the launcher (1546), feeds (1731) | 380 |
| `static/js/function.js` | inspector (1852: the function drawer, the editor, the score chart, the diff), function list (2126) | 340 |
| `static/js/steering.js` | drawers and keys (2194), steering (2294), autopilot, source/export (2429), project settings (2490) | 350 |
| `static/js/pages.js` | agents & models page (2548), compilers & tools page (2626), settings page (2647), updates (2695) and whatever starts the app at the very end | 280 |

`index.html` then loads them in that order, each with its own tag, at the end of `<body>` where `app.js` was:

```html
<script src="/assets/js/base.js"></script>
<script src="/assets/js/sidebar.js"></script>
...
<script src="/assets/js/pages.js"></script>
```

Before cutting, find the code that runs at load time and decide where it goes:

```bash
grep -n "^[a-zA-Z\$(].*addEventListener\|^matchMedia\|^window\.\|^document\.\|^const .*= \$(\|^route\|^init\|^poll(" \
  src/decompnet/app/static/app.js
```

The app's start (the first route and poll, at the bottom of today's file) must be in the last file.

### 5.4 Doing the move

1. Write a small script (in your scratch folder, not the repository) that reads `app.js`, cuts it at the section
   header lines into the files above, and writes them verbatim. Cut at the `/* ---` lines so each section's header
   comment goes with it. Don't hand-copy 2,800 lines.
2. Check nothing was lost or duplicated: concatenating the new files in load order must give back the old file,
   apart from the cut points:
   `cat static/js/{base,sidebar,home,project,map,agents,function,steering,pages}.js | diff - static/app.js` should
   show no differences (if you keep the exact order of sections; if you reorder sections between files, compare
   sorted lines instead, or do the reorder in a second commit).
3. `node --check` every new file (node is on the machine; no npm packages are needed).
4. Update `index.html`, delete `app.js`.
5. Update the docs that name `app.js`: AGENTS.md (the "app/static/ (GUI)" mention), the GUIDE's layout table,
   CONVENTIONS.md section 1 if step 4 is done.

### 5.5 Checking it (there are no JavaScript tests: this is by hand)

The Python suite covers the API, not the page. Check the page in a browser, on a throwaway app, never the user's:

```bash
DECOMP_HOME=<scratch>/home BROWSER=true DECOMP_TRAY=0 uv run decomp --no-browser --port 6481
```

Open `http://127.0.0.1:6481/?k=<secret from <scratch>/home/.decomp/server.json>` and go through, with the browser's
console open (any error there is a failure):

- home: the project list, opening the demo ROM (Demo Quest 64, it ships with the app), the import screen;
- a project: overview (the ROM map in both Progress and Compiler view, the counters, the chart), Functions (search,
  filters), a function's drawer (assembly, the editor, Ctrl+Enter judging, the diff, the score chart), Agents (the
  launcher, starting and stopping a helper is not needed: opening the form is), Activity, Team (opening the room
  form), Source (export), Settings;
- the app pages: Agents & models, Compilers & tools, Settings, and the update panel;
- dark mode (the moon button at the bottom left) on two of those screens;
- a window narrower than 700 px (the layout's small-screen rules).

A headless browser driven over the Chrome DevTools Protocol can take the screenshots (the previous agent used a
small Python script with `websockets`, run with `uv run --no-project --with websockets`; see the Chromium flags
`--headless=new --remote-debugging-port=... --user-data-dir=<scratch>`). Compare against screenshots of the same
screens taken before the split: they must be identical.

Commit: `The GUI's script is split into a file per screen (no build step, no behaviour change)`, body: why classic
scripts and not modules, the load order rule.

### 5.6 Done when

- `app.js` is gone, the nine files exist, `index.html` loads them in order.
- Every screen in 5.5 works, with no console errors, in light and dark mode.
- The concatenation check in 5.4 passes (or the reorder is its own commit).
- Docs name the new files.

---

## After both steps

- Delete this file.
- Rebase or merge `main` into `tidy-codebase` (it will have moved on; see the next section), run the full suite,
  and hand the branch to the user to merge and push.

### Merging with work done on main meanwhile

`main` keeps getting features while this branch exists, and features usually touch `app/engine.py` and
`app/server.py`, which this branch turned into packages. Git can't follow a method that moved to another file, so a
merge conflicts on `engine.py` / `server.py` ("deleted in tidy-codebase, modified in main"). Resolve it by hand: for
each change main made to a method in the old `engine.py`, apply the same change to that method in its new module
(`grep -n "def <name>" src/decompnet/app/engine/*.py` finds it); for a new method, put it in the module of its topic
(`app/engine/__init__.py` lists them); for a new route in `server.py`, put it in the matching `app/routes/` module.
New features on main are asked to keep their logic in a module of their own with small hooks in `engine.py`, to keep
this cheap (the range checker, built while this branch waited, does this).
