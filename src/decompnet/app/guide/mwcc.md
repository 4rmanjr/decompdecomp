# Matching Metrowerks CodeWarrior (GameCube, Wii, DS)

The project sets each file's compiler version (GC/1.x-3.x, Wii/1.x, DS 1.x-2.x) and flags, typically
`-O4,p -inline auto -fp hard -enum int -Cpp_exceptions off -RTTI off`. Files may carry `#pragma`s
(`optimization_level`, `inline_depth`, `dont_inline`, `scheduling`, `peephole`, `opt_strength_reduction`):
keep them, and only add your own the way the project does elsewhere.

Inlining (the biggest source of mismatches)
- With `-inline auto`, small functions defined earlier in the same file, and `inline` functions in
  headers, are inlined. If the target calls a function you wrote as inline (or inlines one you call),
  move or change the definition, or use the project's existing inline helper.
- Vector/matrix math and getters are usually header inlines in the project: use them rather than
  writing the arithmetic out, or the instruction order differs.

Registers and stack
- Callee-saved registers are handed out from r31 downward roughly in order of first use; reordering
  local declarations or first uses changes which r31/r30... a value gets.
- Locals keep declaration order on the stack; unexplained gaps usually mean a removed or inlined local
  (or a struct temporary).

Code shape
- `if`/`else` keeps source order: the fall-through block is the first branch you wrote.
- Ternaries can compile differently from the equivalent `if`/`else`; try both.
- Loops with known trip counts get strength-reduced (pointer walking) and sometimes unrolled.
- `BOOL` is usually `int` (Dolphin/Revolution SDK); `bool` is a byte in C++. Using the wrong one adds
  or drops `clrlwi`/`extsb`.
- Float literals come from .sdata2 in first-use order; `0.0f` vs `0.0` matters (double adds `frsp`).
- `-enum int`: enums are 4 bytes; enum-typed struct fields are words.

Libraries: the Dolphin/Revolution SDK (OS, GX, DVD, PAD, VI, AX), MSL (the C library), the Runtime
(`__ptmf`, `__construct_array`), NW4R, EGG or JSystem are usually already decompiled in the community;
the project's headers have their prototypes and structs.

DS (mwccarm, CodeWarrior for DS 1.2-2.0 and DSi 1.x): the same inlining rules; see guide("arm") for the
instruction set. Notes that differ from GameCube/Wii:
- Code is ARM unless a file says `#pragma thumb on`; `-interworking` lets ARM and Thumb call each other.
- Callee-saved registers r4-r11 are pushed together (`stmdb sp!, {r4-r8, lr}`); which register a local gets
  follows first use, like on PowerPC.
- No FPU: with `-fp soft`, float math calls `_fadd`, `_fsub`, `_fmul`, `_fdiv`, `_ffix`, `_fflt`, `_fls`,
  `_fgr`...; division calls `_s32_div_f` / `_u32_div_f` (or the SDK's divider at 0x04000280).
- Strings and constants sit in .rodata, reached through each function's literal pool; `-str noreuse` keeps
  equal strings as separate copies.
- NitroSDK functions (OS_, MI_, GX_, G2_, FS_, SND_, PAD_, TP_) and its macros are usually already known in the
  project; ITCM functions (fast copies, math) and DTCM data are linked separately but compile like the rest.
- Started from scratch: ds-decomp named things `func_02001234` and `data_02041480`; use those names (or rename
  them with the project's tools) and keep each function in the `src/auto/` file that holds its address.
