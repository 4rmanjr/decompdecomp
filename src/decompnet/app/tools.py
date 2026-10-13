"""The tools a decompiling agent gets, served over MCP to CLI agents and called directly by the
built-in API agent. Every tool works on the project the agent was started on."""

from __future__ import annotations

from importlib import resources
from typing import Callable

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..network import Agent, NetworkError
from .engine import RomNetwork

GUIDES = {
    "workflow": "the matching loop, reading diffs, priorities",
    "mips": "MIPS registers, calling convention, delay slots, types from instructions",
    "gcc-kmc": "matching KMC GCC 2.7.2 code",
    "ido": "matching IDO 5.3/7.1 code",
    "m2c": "reading the machine-generated draft",
    "data": "globals, strings, tables, floats, data you define",
    "structs": "working out struct layouts from offsets",
    "n64": "N64 memory map, OS library, graphics/audio data",
    "naming": "naming functions and data",
}

INSTRUCTIONS = """\
decomp.wtf: you are decompiling a game ROM that the user owns into C that compiles to exactly the same
machine code. Your C is compiled with the game's original compiler, linked at the function's real address
and compared with the ROM: score 0 means byte-identical, and the function is saved as matched.
Start with project_info, then claim_function -> compile_and_diff (repeat). guide(topic) has know-how.
Everything runs on the user's computer. The game's code is the user's: never publish it anywhere.
"""

WORK_PROMPT = """\
You are a decompilation agent in decomp.wtf, working on {title}. Other agents may be working on other
functions of the same game at the same time; the tools keep you from colliding.

Your job: turn functions of this game into C that compiles to *exactly* the original machine code.
Use only the decomp tools. Repeat for up to {count} functions, then stop:

1. claim_function() gives you the next function: assembly, location, calls, data it uses, the best
   previous attempt and notes (start from those!), and a machine-generated draft.
2. get_context() shows the shared header every compile includes.
3. Write the C function (same name as the assembly) and call compile_and_diff after every change.
   Read the diff: '<' '>' missing/extra instructions, 'i' operands, 'o' order, 'r' registers, 's' stack.
   Fix structure first, registers last. guide("workflow") and guide("{compiler_guide}") have tricks.
4. Score 0 = matched and saved. Before moving on, share what you learned: edit_context for prototypes and
   structs you worked out, rename_symbol for functions/data whose purpose is now clear.
5. Stuck after ~20 compiles with no progress? release_function with precise notes (what you tried, what
   is left) and claim the next one. Don't spend forever on one function.

Write plausible original code, pure C (no inline asm). Be brief in your messages: work, don't narrate.
"""


def _guide(topic: str = "") -> str:
    topic = topic.strip().lower()
    if topic not in GUIDES:
        listing = "\n".join(f"- {k}: {v}" for k, v in GUIDES.items())
        return f"Guides (call guide with one of these topics):\n{listing}"
    return resources.files("decompnet.app").joinpath("guide", f"{topic}.md").read_text()


def project_info(net: RomNetwork, slug: str) -> str:
    ov = net.overview(slug)
    pct = 100 * ov["matched_bytes"] / (ov["bytes"] or 1)
    return (f"{ov['title']} ({ov['rom']['game_code']}, {ov['rom']['region']})\n"
            f"compiler: {ov['compiler']['name']} {' '.join(ov['compiler']['flags'])}\n"
            f"progress: {ov['matched']}/{ov['functions']} functions matched ({pct:.1f}% of code), "
            f"{ov['attempts']} compiles so far, context.h v{ov['context_version']}\n"
            f"Next: claim_function(). Guides: guide().")


# name -> handler(net, agent, slug, **args); every handler returns text for the model
HANDLERS: dict[str, Callable[..., str]] = {
    "project_info": lambda net, agent, slug: project_info(net, slug),
    "claim_function": lambda net, agent, slug, function="", strategy="easiest":
        net.claim(agent, slug, function or None, strategy),
    "get_function": lambda net, agent, slug, function: net.get_function(function, slug, agent),
    "compile_and_diff": lambda net, agent, slug, function, source: net.compile_and_diff(agent, function, source, slug),
    "release_function": lambda net, agent, slug, function, notes: net.release(agent, function, notes, slug),
    "get_context": lambda net, agent, slug, function="": net.get_context(slug, function or None),
    "edit_context": lambda net, agent, slug, old, new, message, function="":
        net.edit_context(agent, slug, old, new, message, function or None),
    "rename_symbol": lambda net, agent, slug, old, new, message="": net.rename_symbol(agent, slug, old, new, message),
    "split_function": lambda net, agent, slug, function, at, new_name="":
        net.split_function(agent, slug, function, at, new_name),
    "read_data": lambda net, agent, slug, what, length=64: net.read_data(slug, what, length),
    "list_functions": lambda net, agent, slug, status="open", limit=40: net.list_functions(slug, status, limit),
    "add_note": lambda net, agent, slug, text, function="": net.add_note(agent, slug, text, function or None),
    "search_notes": lambda net, agent, slug, query: net.search_notes(slug, query),
    "guide": lambda net, agent, slug, topic="": _guide(topic),
}


def call(net: RomNetwork, agent: Agent, slug: str, name: str, args: dict) -> str:
    """Run one tool for the built-in agent; errors come back as text the model can act on."""
    if name not in HANDLERS:
        return f"error: unknown tool {name!r}"
    try:
        return HANDLERS[name](net, agent, slug, **args)
    except NetworkError as e:
        return f"error: {e}"
    except TypeError as e:
        return f"error: bad arguments for {name}: {e}"


def build_mcp(net: RomNetwork, agent_of: Callable[[Context], tuple[Agent, str]]) -> MCPServer:
    """MCP server for CLI agents. `agent_of` maps a request to (agent, project slug)."""
    mcp = MCPServer(name="decomp", title="decomp.wtf", instructions=INSTRUCTIONS, version="0.4.1")

    async def run(ctx: Context, name: str, **args) -> str:
        try:
            agent, slug = agent_of(ctx)
        except NetworkError as e:
            raise ToolError(str(e)) from None
        try:
            return await anyio.to_thread.run_sync(lambda: HANDLERS[name](net, agent, slug, **args))
        except NetworkError as e:
            raise ToolError(str(e)) from None

    @mcp.tool()
    async def project_info(ctx: Context) -> str:
        """Which game you're working on, its compiler, and progress so far."""
        return await run(ctx, "project_info")

    @mcp.tool()
    async def claim_function(ctx: Context, function: str = "", strategy: str = "easiest") -> str:
        """Claim the next function to decompile (or a specific one by name or address).

        Returns its assembly, location, calls and data, the best previous attempt, notes from earlier agents
        and a machine-generated draft. You hold one claim at a time; each compile renews it.

        Args:
            function: optional name or address (0x80001234) of a specific function
            strategy: easiest (fewest instructions), closest (nearly matched already), hardest, random
        """
        return await run(ctx, "claim_function", function=function, strategy=strategy)

    @mcp.tool()
    async def get_function(ctx: Context, function: str) -> str:
        """Show any function (by name or address): assembly, status, best attempt, notes. Read matched
        functions to learn how this codebase is written."""
        return await run(ctx, "get_function", function=function)

    @mcp.tool()
    async def compile_and_diff(ctx: Context, function: str, source: str) -> str:
        """Compile your C for `function` with the game's compiler, link it at the function's address and
        diff it against the ROM. Define the function with exactly its name. context.h is included for you.
        Score 0 = byte-identical: the function is saved as matched."""
        return await run(ctx, "compile_and_diff", function=function, source=source)

    @mcp.tool()
    async def release_function(ctx: Context, function: str, notes: str) -> str:
        """Give up a function you're stuck on. Notes are required: what you tried, what is left. Your best
        attempt is kept for the next agent."""
        return await run(ctx, "release_function", function=function, notes=notes)

    @mcp.tool()
    async def get_context(ctx: Context, function: str = "") -> str:
        """The shared header (types, structs, prototypes) included before every compile."""
        return await run(ctx, "get_context", function=function)

    @mcp.tool()
    async def edit_context(ctx: Context, old: str, new: str, message: str, function: str = "") -> str:
        """Edit the shared header: replace `old` (must occur exactly once) with `new`, or append `new` when
        old is "". Accepted only if every matched function still matches. Add prototypes and structs here."""
        return await run(ctx, "edit_context", old=old, new=new, message=message, function=function)

    @mcp.tool()
    async def rename_symbol(ctx: Context, old: str, new: str, message: str = "") -> str:
        """Give a function or data address a real name (e.g. func_80001234 -> player_update). Updates the
        assembly everyone sees, matched sources and the header; never breaks a match."""
        return await run(ctx, "rename_symbol", old=old, new=new, message=message)

    @mcp.tool()
    async def split_function(ctx: Context, function: str, at: str, new_name: str = "") -> str:
        """Split a function the disassembler merged with the next one (e.g. after a function that never
        returns). `at` is the address where the second function starts."""
        return await run(ctx, "split_function", function=function, at=at, new_name=new_name)

    @mcp.tool()
    async def read_data(ctx: Context, what: str, length: int = 64) -> str:
        """Hex dump of ROM data at an address or symbol, with text, pointers and floats decoded."""
        return await run(ctx, "read_data", what=what, length=length)

    @mcp.tool()
    async def list_functions(ctx: Context, status: str = "open", limit: int = 40) -> str:
        """List functions by status: open, matched or all."""
        return await run(ctx, "list_functions", status=status, limit=limit)

    @mcp.tool()
    async def add_note(ctx: Context, text: str, function: str = "") -> str:
        """Leave a note for other agents, on a function or the whole project."""
        return await run(ctx, "add_note", text=text, function=function)

    @mcp.tool()
    async def search_notes(ctx: Context, query: str) -> str:
        """Search notes left by agents."""
        return await run(ctx, "search_notes", query=query)

    @mcp.tool()
    async def guide(ctx: Context, topic: str = "") -> str:
        """Decompilation know-how. Call without a topic for the list (workflow, mips, gcc-kmc, ido, m2c,
        data, structs, n64, naming)."""
        return await run(ctx, "guide", topic=topic)

    @mcp.prompt(title="Decompile", description="Work loop: claim functions and match them byte for byte.")
    def work(count: str = "5") -> str:
        return "Call project_info first, then follow the decomp.wtf work loop for up to " + count + " functions."

    return mcp
