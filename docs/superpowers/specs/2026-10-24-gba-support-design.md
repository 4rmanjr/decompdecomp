# GBA ROM Support Design

## Goal

Restore the full Game Boy Advance ROM workflow that existed in `decomp.wtf` in the `decompdecomp` console architecture. A user with a legally obtained GBA ROM must be able to start a project from it, work toward byte-identical function matches, and export a rebuildable ROM project. Existing public GBA fork support remains available.

## Architecture

Implement GBA as a ROM-mode console package under `src/decompnet/consoles/gba/`, registered through the console connector. The package owns GBA-specific ROM recognition and metadata, luvdis-driven function splitting plus recovery of uncalled code, GBA ROM layout facts, and GBA export behavior. Shared ROM importing, function storage, matching, and project workflows remain in the existing `app/engine/`, `rom/`, and `arch/` layers. Use the existing ARM/Thumb architecture and GBA guide where compatible rather than introducing a separate processor implementation.

Use the existing `Console.split_rom` hook for GBA's non-splat splitter. Preserve the current local edits to that hook and its caller; they establish the required console-level seam for all ROM consoles.

## User Workflow

- Identify a GBA ROM during file selection and provide its title, game code, region, size, and GBA platform status.
- Permit confirmed import through the common ROM import pipeline, storing the correct GBA platform, ARM architecture, Thumb code addresses, ROM metadata, and compiler settings.
- Offer the GBA compiler/toolchain through normal setup and consent flows; do not silently install proprietary software.
- Keep existing GBA public-decomp forks working through the public console registry.
- Export an imported GBA project to a source tree that rebuilds the ROM byte for byte from the user's own ROM and matched C, with assembly as reference/fallback.

## Compatibility and Boundaries

Do not add GBA-specific platform conditionals to shared application code when console facts or console methods can express the behavior. Keep GBA fork metadata and start-from-scratch ROM behavior on the same `gba` platform identity without disrupting the public fork path. Never check ROM/game data into the repository; a homemade demo must generate its ROM locally and keep generated build artifacts ignored.

Retain the prior GBA format/compiler semantics from `decomp.wtf` where applicable, but adapt them to current interfaces and pinned-toolchain conventions. Do not weaken byte-identical judging or add a dependency unless the existing dependency set cannot support the port.

## Validation

- Unit tests cover GBA ROM identification, metadata, function splitting/recovery edge cases, and console registration/facts.
- An optional homemade GBA demo test, skipped when required compiler/disassembler tools are unavailable, covers import, representative ARM/Thumb matching and mismatch rejection, and export/rebuild identity.
- Existing public GBA fork tests and the general console conformance tests continue to pass.
- Run focused GBA and console tests, then the project test suite and `ruff check src tests projects` when the environment permits.

## Scope Exclusions

This work does not add support for other consoles, change public GBA fork project formats, or install compilers/tools without user consent.
