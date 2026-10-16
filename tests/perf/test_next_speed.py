"""``docket next`` warm, mid-run, reference-like persona: under 300 ms median of 20 (TP §10, AE-10)."""
from __future__ import annotations

import io
import os
import statistics
import time

import pytest

from docket.cli import main
from tests.agent_loop import run_agent

pytestmark = pytest.mark.slow


def test_next_is_fast_warm(persona, cli, pinned_now):
    ws = persona("reference-like")
    cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    run_agent(ws, resume_paused=False, stop_at=lambda s: s["kind"] == "wait")
    argv = ["next", "--json", "--workspace", str(ws)]
    env = dict(os.environ)
    main(argv, stdout=io.StringIO(), env=env, cwd=str(ws))  # warm
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        assert main(argv, stdout=io.StringIO(), env=env, cwd=str(ws)) == 0
        times.append(time.perf_counter() - t0)
    median = statistics.median(times)
    print(f"docket next median {median * 1000:.1f} ms")
    assert median < 0.3
