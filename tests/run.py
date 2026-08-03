#!/usr/bin/env python3
"""Run the test suite without pytest.

    python tests/run.py            # everything
    python tests/run.py redaction  # only files whose name contains "redaction"

Collects every test_* function from every tests/test_*.py, runs it, and reports.
Exit code is non-zero if anything failed, so CI can use it directly.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# When this file runs as __main__ AND a test module does `from run import skip`,
# Python would build a second module object, so run._Skip and __main__._Skip
# would be different classes and skips would surface as failures. Registering
# ourselves under the name tests import makes both resolve to this module.
sys.modules.setdefault("run", sys.modules[__name__])


def _load(path: str):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str]) -> int:
    wanted = argv[1] if len(argv) > 1 else ""
    files = sorted(f for f in glob.glob(os.path.join(HERE, "test_*.py"))
                   if wanted in os.path.basename(f))
    if not files:
        print(f"no test files match {wanted!r}")
        return 1

    passed = failed = skipped = 0
    failures: list[tuple[str, str]] = []

    for path in files:
        name = os.path.basename(path)
        try:
            mod = _load(path)
        except Exception:
            print(f"\n{name}\n  COLLECTION ERROR")
            failures.append((name, traceback.format_exc()))
            failed += 1
            continue
        tests = [(n, f) for n, f in vars(mod).items()
                 if n.startswith("test_") and callable(f)]
        print(f"\n{name}  ({len(tests)} tests)")
        for tname, fn in tests:
            try:
                fn()
            except _Skip as e:
                skipped += 1
                print(f"  skip {tname}  ({e})")
            except Exception:
                failed += 1
                failures.append((f"{name}::{tname}", traceback.format_exc()))
                print(f"  FAIL {tname}")
            else:
                passed += 1
                print(f"  ok   {tname}")

    print("\n" + "=" * 68)
    for where, tb in failures:
        print(f"\n--- {where}\n{tb}")
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


class _Skip(Exception):
    """Raised by a test that cannot run in this environment."""


# Exposed so test modules can `from run import skip`.
def skip(reason: str):
    raise _Skip(reason)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
