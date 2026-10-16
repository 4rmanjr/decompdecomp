# Roadmap

What's planned for decompdecomp, roughly in order, and why. Every item keeps the rule that matters most: a match is
byte-identical after linking (ROM mode) or objdiff 100% (fork mode). Nothing here accepts code that only does the
same thing.

Want to work on one? Open an issue first, so two people don't build the same thing. CONTRIBUTING.md has the rest.

## Recently done

- **C++ classes from the game's code** (MIPS: PlayStation 2, PlayStation, N64). Constructors store their class's
  vtable into the object; from those stores decompdecomp finds each class, its virtual functions, its base class and
  every field its functions touch (offset, type). Agents read them with `get_class`; `get_function` names the fields
  of `this` a function uses. On GTA San Andreas: 507 classes, and the recovered entity layout (position at +4, matrix
  pointer at +0x14, model index at +0x22) is the one found by hand. Why: functions with virtual calls matched 7% of
  the time against 62% for the rest, because agents guessed the layouts.
- **Linux packages**: a `.deb` and an AppImage with their own Python, built for each release.
- **Its own window and a tray icon**, so closing the window doesn't stop helper agents.

## Next

### Compiler map

Find which compiler built each part of a game from the quirks in its code, not only for the game as a whole. Big
games mix compilers: GTA San Andreas is EE-GCC 2.95 with CodeWarrior and GCC 2.96 libraries linked in. Today agents
find the exceptions by hand, one function at a time, and single-function guesses go wrong (43 of them were removed
from GTA San Andreas).

- Score every function on quirks: branch-likely instructions (in GTA San Andreas, none in 54 large CodeWarrior
  functions, 9–24% of GCC functions), the delay slot of returns, register moves (`daddu` / `or` / `addu`), how `$ra`
  is saved (`sq` / `sd`), `$gp`-relative accesses (`-G8` against `-G0`), stack frame setup and teardown, jump table
  and constant loading.
- Learn from the project's own matches: each one is a confirmed example of its compiler.
- Group neighbouring functions (one object file is one compiler) and propose a range for each run that differs from
  the game's compiler; the judge confirms it with real matches before it applies (256 bytes or 3 functions, as for
  agents' ranges today).
- Show it as a band under the ROM map.

Limit: until bigger functions match, the learned part has few examples; the starting rules carry it.

### Twins across your games

Twins already give free matches for code a game has more than once. PlayStation 2 games share RenderWare, Sony's
libraries and libc, so a library function matched in one of your games should be tried in all your others: a
fingerprint index shared by your projects, the same trial, the same judge. No agent time; nothing leaves your
computer.

### Names from another build you own

Many games have a second build with symbols: a demo, a prototype, or a port to another platform. C++ symbols say the
class and the parameter types, so carrying them over gives agents real signatures. Pair functions by the text and
constants they use, spread the pairs through the call graph, and let the user approve the names in bulk (as boundary
reports are approved now). Names only: matching is unaffected. It also names the classes `get_class` finds.

### C++ classes for PowerPC and x86

The class recovery reads MIPS today. GameCube and Wii (CodeWarrior: the vtable pointer often isn't at offset 0) and
Windows PC (MSVC: `this` in ecx) work the same way with their own instruction readers, in fork mode.

### A modding-ready check for the export

The export rebuilds the game byte for byte; mods need code that can move. Build a second copy with padding inserted
and report every address that is still hard-coded ("93% movable, 41 addresses left"). The matching build stays as
it is.

### A shareable progress card

An image with % matched, functions and platform, for READMEs and posts. No game code in it.

## Later

- A tray icon on Windows (decompdecomp runs in WSL there; it needs a small Windows program).
- macOS.
