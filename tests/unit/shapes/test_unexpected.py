from __future__ import annotations

import pytest

from docket.shapes.unexpected import collecting, unexpected

pytestmark = pytest.mark.smoke


def test_extend_must_be_non_empty():
    with pytest.raises(ValueError):
        unexpected("a", "b", reading="r", extend="")
    with pytest.raises(ValueError):
        unexpected("a", "b", reading="r", extend="   ")


def test_collector_dedupes_and_truncates_samples():
    with collecting() as col:
        unexpected("a", "b", reading="r", extend="x", sample="s" * 2000)
        unexpected("a", "b", reading="r", extend="x")
        unexpected("a", "c", reading="r", extend="x")
        rows = col.drain()
    assert [(r.area, r.detail) for r in rows] == [("a", "b"), ("a", "c")]
    assert len(rows[0].sample.encode()) == 500


def test_shapes_reach_envelope_run_file_db_and_report(cli, persona, pinned_now):
    ws = persona("reference-like")
    code, env = cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    run_id = env["data"]["run"]
    code, env = cli("collect", "--workspace", str(ws))
    assert code == 0
    got = {(u["area"], u["detail"]) for u in env["unexpected"]}
    assert ("source.unit", "chat unit without name") in got  # the recorded gateway lists a nameless space
    assert ("source.contract", "chat.space.spaceType=THREADED_ROOM") in got  # of a type the preset does not know
    assert ("item.mapping", "chat.from missing") in got  # one chat record lacks a sender
    assert all(u["extend"] for u in env["unexpected"])
    assert env["data"]["result"]["sources"]["transcripts"]["outcome"] == "skipped"
    import os
    from docket.store.paths import state_root
    lines = (state_root(ws, os.environ) / "runs" / run_id / "unexpected.jsonl").read_text().splitlines()
    assert len(lines) == len(env["unexpected"])
    code, env = cli("unexpected", "--workspace", str(ws))
    shapes = {(s["area"], s["detail"]): s for s in env["data"]["shapes"]}
    assert shapes[("source.contract", "chat.space.spaceType=THREADED_ROOM")]["extend"].startswith(
        "preset mcp/chat-codemode: contract.space")
    code, env = cli("unexpected", "--since", "1d", "--workspace", str(ws))
    assert env["data"]["total"] == len(shapes)
    shape = ("source.unit", "chat unit without name")
    code, env = cli("unexpected", "accept", *shape, "--reason", "the hallway space has no name", "--workspace", str(ws))
    assert code == 0
    code, env = cli("unexpected", "--workspace", str(ws))
    assert shape not in {(s["area"], s["detail"]) for s in env["data"]["shapes"]}
    code, env = cli("unexpected", "--all", "--workspace", str(ws))
    assert shape in {(s["area"], s["detail"]) for s in env["data"]["shapes"]}


def test_dry_run_records_nothing(cli, persona, pinned_now):
    ws = persona("reference-like")
    cli("run", "start", "daily", "--now", pinned_now, "--workspace", str(ws))
    code, env = cli("collect", "--dry-run", "--workspace", str(ws))
    assert code == 0 and env["data"]["dry_run"] is True and env["unexpected"] == []
    code, env = cli("unexpected", "--workspace", str(ws))
    assert env["data"]["shapes"] == []
