# Matching PlayStation 2 code

CPU: the Emotion Engine (R5900), MIPS III-like with 128-bit registers.
- `lq`/`sq` move 128 bits; MMI instructions (`pcpyld`, `paddw`, `pextlw`...) come from inlined
  memcpy/memset or vector code.
- `mult rd, rs, rt` (three operands) and `madd` exist; `mflo` still appears.
- The FPU is single precision only: `double` arithmetic becomes calls (`__adddf3`, `__muldf3`): if you
  see those, a double was involved (or a literal without `f`).
- 64-bit integer ops (`daddu`, `dsll32`, `ld`/`sd`) mean `long`/`long long` (ee-gcc: `long` is 64-bit).
  GCC writes register moves as `daddu rd, rs, $zero` (a 64-bit move), older code as `addu`/`or`.
- COP2 is VU0 in macro mode (`vadd.xyzw`, `qmfc2`, `qmtc2`): from the SDK's inline VU0 helpers; use the
  project's macros/functions.
- Float compares: `c.lt.s` + `bc1t`/`bc1f`; float to int: `cvt.w.s` + `mfc1`.

Compilers: Sony's ee-gcc 2.9x (most games), Metrowerks for PS2, or SN Systems' compiler; the project
decides per file. Games started from scratch get one picked from the code itself (CodeWarrior leaves `nop` in
the delay slot of nearly every `jr $ra`, GCC fills it; CodeWarrior 2.x moves registers without `daddu`), then
among the GCCs by compiling drafts with each. Notes in the program (".comment") can come from one library only.
If nothing matches although the C is right, the compiler may be another one: say so in a note, the user can switch it.
- CodeWarrior: small data is `-sdatathreshold 8`, not `-G8`; it gives each function its own section and
  loves `lui`/`addiu` pairs over `$gp` when the threshold is 0.
- EE-GCC 3.2 loads a switch's jump table with `addiu %lo(jtbl)` then `lw 0(reg)`; 2.9x/2.96 use
  `lw %lo(jtbl)(reg)`. That alone tells the two apart. C++ is common: old GCC 2.9x mangling (`update__7CPlayerf`) and vtable thunks.
Small data is `$gp`-relative (-G8 typical), so declare externs with their real sizes.

Libraries: Sony's libgraph, libdma, libpad, libcdvd, libkernl, libsd and libvu0 are linked as
libraries; their prototypes are in the project's headers.
