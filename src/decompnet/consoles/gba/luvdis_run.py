"""Run luvdis with the stack underflow fix needed by some GBA ROMs."""

import importlib.util
import sys

PUSH = """                        if self.sp >= len(self.stack):
                             self.stack.append(value)
                         else:
                             self.stack[self.sp] = value
"""
SAFE_PUSH = """                        if self.sp >= len(self.stack):
                             self.stack.append(value)
                         elif self.sp >= 0:
                             self.stack[self.sp] = value
"""


def main() -> int:
    spec = importlib.util.find_spec("luvdis.analyze")
    with open(spec.origin, encoding="utf-8") as source_file:
        source = source_file.read()
    module = importlib.util.module_from_spec(spec)
    sys.modules["luvdis.analyze"] = module
    exec(compile(source.replace(PUSH, SAFE_PUSH, 1), spec.origin, "exec"), module.__dict__)
    from luvdis.__main__ import main as luvdis

    return luvdis(prog_name="luvdis")


if __name__ == "__main__":
    raise SystemExit(main())
