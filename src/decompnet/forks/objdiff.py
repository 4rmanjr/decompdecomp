"""objdiff-cli: the community's diff engine, for every architecture it supports."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import toolchains
from ..network import NetworkError

KIND_MARK = {None: " ", "DIFF_NONE": " ", "DIFF_REPLACE": "|", "DIFF_DELETE": "<", "DIFF_INSERT": ">",
             "DIFF_OP_MISMATCH": "|", "DIFF_ARG_MISMATCH": "i"}


class ObjdiffError(NetworkError):
    pass


def _exe(log=print) -> str:
    toolchains.ensure("objdiff-cli", log=log)
    return str(toolchains.binary("objdiff-cli"))


def _run(args: list[str], cwd: Path, timeout: float = 600) -> str:
    res = subprocess.run([_exe(), "--no-color", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise ObjdiffError((res.stderr or res.stdout).strip()[-1500:] or f"objdiff-cli exited with {res.returncode}")
    return res.stdout


def report(project: Path, out: Path) -> dict:
    """Progress report (objdiff report.proto as JSON) for a project with an objdiff.json.

    objdiff-cli stops at the first unit whose compiled object is missing (work-in-progress source that doesn't
    compile yet, or that the project's default build never compiles). Those units are measured from the game's
    object alone, as not matched yet, the way the projects' own report scripts do it."""
    cfg = json.loads((project / "objdiff.json").read_text())
    missing = [u for u in cfg.get("units", []) if u.get("base_path") and not (project / u["base_path"]).is_file()]
    if not missing:
        _run(["report", "generate", "-p", str(project), "-o", str(out)], cwd=project, timeout=1800)
        return json.loads(out.read_text())
    for unit in cfg.get("units", []):
        for key in ("target_path", "base_path"):
            if unit.get(key):
                unit[key] = str((project / unit[key]).resolve())
    for unit in missing:
        del unit["base_path"]
    with tempfile.TemporaryDirectory(prefix="decompwtf-report-") as tmp:
        (Path(tmp) / "objdiff.json").write_text(json.dumps(cfg))
        _run(["report", "generate", "-p", tmp, "-o", str(out.resolve())], cwd=project, timeout=1800)
    return json.loads(out.read_text())


@dataclass
class FunctionDiff:
    symbol: str
    match_percent: float | None  # None: the function isn't in the base object (not written yet)
    rows: list[tuple[str, str, str]]  # (marker, target, base)
    target_size: int
    unit_scores: dict[str, float | None]  # every function in the unit -> match % (None: not in your build)
    order: list[str]  # the unit's functions in target address order
    ops: dict[str, list[str]]  # the unit's functions -> target mnemonics (for finding look-alikes)

    @property
    def matched(self) -> bool:
        return self.match_percent is not None and self.match_percent >= 100.0

    def target_asm(self) -> str:
        return "\n".join(t for _, t, _ in self.rows if t)

    def render(self, context: int = 2, max_rows: int = 90) -> str:
        legend = "markers: |=different instruction i=operand differs <=missing (target only) >=extra (yours only)"
        width = min(46, max((len(t) for _, t, _ in self.rows), default=10) + 2)
        show = set()
        for i, (m, _, _) in enumerate(self.rows):
            if m != " ":
                show.update(range(max(0, i - context), min(len(self.rows), i + context + 1)))
        lines, prev = [f"   {'TARGET':<{width}}| YOURS"], -1
        for i in sorted(show):
            if i != prev + 1:
                lines.append("   ...")
            m, t, b = self.rows[i]
            lines.append(f" {m} {t[:width - 1]:<{width}}| {b}")
            prev = i
            if len(lines) >= max_rows:
                lines.append(f"   ... ({len(self.rows) - i - 1} more rows)")
                break
        return legend + "\n" + "\n".join(lines)


def _sym(obj: dict | None, name: str) -> dict | None:
    for s in (obj or {}).get("symbols", []):
        if s.get("name") == name:
            return s
    return None


def _fmt(row: dict) -> str:
    ins = row.get("instruction")
    return ins.get("formatted", "") if ins else ""


def _option(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def diff(target: Path | None, base: Path | None, symbol: str, cwd: Path, options: dict | None = None) -> FunctionDiff:
    """Diff one function between the target (expected) and base (yours) objects.

    `options` are the project's objdiff.json options, so percentages agree with its progress report."""
    args = ["diff", "-o", "-", "--format", "json"]
    for key, value in (options or {}).items():
        args += ["-c", f"{key}={_option(value)}"]
    if target is not None and target.exists():
        args += ["-1", str(target)]
    if base is not None and base.exists():
        args += ["-2", str(base)]
    data = json.loads(_run([*args, symbol], cwd=cwd, timeout=120))
    left, right = _sym(data.get("left"), symbol), _sym(data.get("right"), symbol)
    if left is None:
        raise ObjdiffError(f"{symbol} isn't in the target object")
    lrows = left.get("instructions", [])
    rrows = right.get("instructions", []) if right else []
    rows = []
    for k in range(max(len(lrows), len(rrows))):
        lr = lrows[k] if k < len(lrows) else {}
        rr = rrows[k] if k < len(rrows) else {}
        kind = lr.get("diff_kind") or rr.get("diff_kind")
        rows.append((KIND_MARK.get(kind, "|"), _fmt(lr), _fmt(rr)))
    pct = right.get("match_percent", left.get("match_percent")) if right else None
    if right is not None and pct is None:
        pct = 100.0 if all(m == " " for m, _, _ in rows) else 0.0
    built = {s.get("name") for s in (data.get("right") or {}).get("symbols", [])}
    funcs = [s for s in (data.get("left") or {}).get("symbols", []) if s.get("kind") == "SYMBOL_FUNCTION"]
    scores = {s["name"]: (float(s["match_percent"]) if s.get("match_percent") is not None and s["name"] in built else None)
              for s in funcs}
    order = [s["name"] for s in sorted(funcs, key=lambda s: int(s.get("address") or 0))]
    ops = {s["name"]: [r["instruction"]["formatted"].split(None, 1)[0] for r in s.get("instructions", [])
                       if r.get("instruction") and r["instruction"].get("formatted")] for s in funcs}
    return FunctionDiff(symbol, float(pct) if pct is not None else None, rows, int(left.get("size", 0)), scores, order, ops)
