"""A stub hook for tests (TD §13.6). It never touches anything outside the paths it is given.

    hook_stub.py --count FILE [--env-out FILE] [--exit N] [--fail-unless FILE] [--sleep S] [--label TEXT]

Appends one line (the label and argv) to ``--count``; writes the environment it
received (names and values, one per line) and whether stdin and stdout are
terminals to ``--env-out``; exits with ``--exit`` (default 0), or 3 while the
``--fail-unless`` file is absent.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--count", required=True)
p.add_argument("--env-out")
p.add_argument("--exit", type=int, default=0)
p.add_argument("--fail-unless")
p.add_argument("--sleep", type=float, default=0.0)
p.add_argument("--label", default="hook")
a, rest = p.parse_known_args()
with open(a.count, "a", encoding="utf-8") as fh:
    fh.write(a.label + " " + " ".join(rest) + "\n")
if a.env_out:
    data = sys.stdin.read()  # closed stdin reads empty at once; an open one would block
    with open(a.env_out, "w", encoding="utf-8") as fh:
        for k in sorted(os.environ):
            fh.write(f"{k}={os.environ[k]}\n")
        fh.write(f"#stdin_tty={os.isatty(0)} stdin_bytes={len(data)}\n")
        try:
            os.open("/dev/tty", os.O_RDWR)
            fh.write("#controlling_tty=True\n")
        except OSError:
            fh.write("#controlling_tty=False\n")
if a.sleep:
    time.sleep(a.sleep)
print(f"stub hook {a.label} ran")
if a.fail_unless and not os.path.exists(a.fail_unless):
    print("stub hook: precondition file missing", file=sys.stderr)
    sys.exit(3)
sys.exit(a.exit)
