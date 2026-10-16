"""Decisions (IP M5, TD §10): questions, options, answers with every override, merges, undo, refresh,
reconcile, verification and day load, driven through the CLI with invented persona data."""
from __future__ import annotations

import json
import os

from docket.store.paths import state_root
from tests.agent_loop import run_agent

REF_NOW = "2026-10-15T18:00:00-05:00"   # a Thursday in Chicago


def start(cli, ws, workflow="week-plan", now=REF_NOW):
    code, env = cli("run", "start", workflow, "--now", now, "--workspace", str(ws))
    assert code == 0, env
    return env["data"]["run"]


def ask(ws):
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    assert res.failures == [], res.failures
    assert res.final["kind"] == "ask", res.final
    return res.final


def answer(cli, ws, *args):
    return cli("ask", "answer", *args, "--workspace", str(ws))


def plan_json(ws, run_id, pd):
    return json.loads((state_root(ws, os.environ) / "runs" / run_id / "plan" / pd / "plan.json").read_text())


# --- the exit criterion (IP M5) ----------------------------------------------------------------


def test_structured_answers_that_forced_hand_edits_are_one_command_each(persona, cli):
    """"due Monday", "60 minutes", "merge these two" and "schedule it over the flight" are each one
    ``docket ask answer`` and appear correctly in the action preview and the plan."""
    ws = persona("reference-like")
    run_id = start(cli, ws)
    step = ask(ws)
    qa, qb, qc = [q["id"] for q in step["questions"]]
    titles = {q["id"]: q["prompt"] for q in step["questions"]}

    code, env = answer(cli, ws, qa, "add a task", "--due", "Monday", "--est", "60 minutes", "--merge-with", qb,
                       "--link", "https://tracker.example/HC-7")
    assert code == 0, env
    echo, prev = env["data"]["answer"], env["data"]["action_preview"]
    assert echo["option"] == "task" and echo["overrides"]["due"] == "2026-10-19" and echo["overrides"]["est"] == 60
    assert echo["est_normalized"] == "1h" and echo["overrides"]["merge_with"] == [qb]
    assert echo["normalized"] == (f"docket ask answer {qa} task --due 2026-10-19 --est 1h "
                                  f"--link https://tracker.example/HC-7 --merge-with {qb}")
    line = prev["operations"][0]["line"]
    assert line.startswith("- [ ] Please approve the headcount request") and "; Heads up: the nightly" in line
    assert line.endswith("[due:: 2026-10-19] [est:: 1h] [links:: https://tracker.example/HC-7]")
    assert prev["merged"]["questions"] == [qb]
    monday = prev["day_load"][0]
    assert monday["date"] == "2026-10-19" and monday["work_window"] == "09:00-17:00"
    assert monday["committed_minutes"] == 60 and monday["free_minutes"] == 360

    code, env = cli("ask", "list", "--workspace", str(ws))
    rows = {r["id"]: r for r in env["data"]["questions"]}
    assert rows[qb]["answer"]["merged_into"] == qa and rows[qb]["answer"]["option"] == "merged"

    code, env = answer(cli, ws, qc, "block", "--when", "Flight", "--est", "90", "--title", "Draft the offsite memo")
    assert code == 0, env
    prev = env["data"]["action_preview"]
    assert env["data"]["answer"]["overrides"]["when"] == "event:Flight"
    assert prev["window"]["resolved"] == "Wed 2026-10-21 13:00-16:00"
    assert prev["window"]["event"]["title"] == "Flight to the offsite"
    assert prev["schedule"] == {"title": "Draft the offsite memo", "est": 90, "when": "event:Flight"}
    assert prev["day_load"][0]["date"] == "2026-10-21" and prev["day_load"][0]["committed_minutes"] == 180

    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    assert res.failures == [], res.failures
    code, env = cli("plan", "diff", "--workspace", str(ws))
    plan = plan_json(ws, run_id, env["data"]["plan_digest"])
    lines = [o for o in plan["operations"] if "line" in o]
    assert [o["question_id"] for o in lines] == [qa] and lines[0]["line"] == line   # qb folded into qa
    blocks = {o["title"]: o for o in plan["operations"] if o.get("verb") == "create" and o.get("kind") != "mirror"}
    assert blocks["Draft the offsite memo"]["start"] == "2026-10-21T13:00:00-05:00"
    assert blocks["Draft the offsite memo"]["end"] == "2026-10-21T14:30:00-05:00"
    merged_block = next(t for t in blocks if t.startswith("Please approve the headcount"))
    assert blocks[merged_block]["start"].startswith("2026-10-19") and titles[qb]


# --- options -------------------------------------------------------------------------------------


def _forum_question(step):
    return next(q for q in step["questions"] if (q["details"].get("defaults") or {}).get("forum") == "platform-sync")


def test_guards_can_accept_and_label_templates(persona, cli):
    ws = persona("reference-like")
    start(cli, ws)
    q = _forum_question(ask(ws))
    labels = {o["id"]: o["label"] for o in q["options"]}
    # labels render the derived defaults: run date + 3 lead days, the default estimate, "this week"
    assert labels["task"] == "Add a task (due Sun)" and labels["block"] == "Block 30m this week"
    refused = {r["option"]: r["reason"] for r in q["details"]["refused"]}
    assert refused == {"agenda": "agendas: no agenda found for this item",
                       "draft": "no configured destination accepts message.draft"}
    assert "Not offered: agenda: agendas: no agenda found for this item" in q["details"]["notes"]

    agenda = ws / "Syncs" / "Platform.md"
    agenda.parent.mkdir()
    agenda.write_text("# Platform Sync\n\n## Next\n- Review the error budget\n")
    dest = ws / "destinations.yaml"
    dest.write_text(dest.read_text().replace("    path: Syncs\n", "    path: Syncs\n    capacity: 1\n", 1))
    ask(ws)  # a destinations edit stales merge (it reads task stores); merge again
    code, env = cli("ask", "show", q["id"], "--workspace", str(ws))
    assert code == 0, env
    refused = {r["option"]: r["reason"] for r in env["data"]["refused"]}
    assert refused["agenda"] == "agendas: Syncs/Platform.md ## Next is full (1/1)"

    dest.write_text(dest.read_text().replace("capacity: 1", "capacity: 5"))
    ask(ws)
    code, env = cli("ask", "show", q["id"], "--workspace", str(ws))
    opts = {o["id"]: o for o in env["data"]["options"]}
    assert opts["agenda"]["label"] == "Raise it at Platform Sync" and opts["agenda"]["destination"] == "agendas"
    code, env = answer(cli, ws, q["id"], "raise it", "--note", "ask who owns it")
    assert code == 0, env
    op = env["data"]["action_preview"]["operations"][0]
    assert (op["destination"], op["path"], op["anchor"]) == ("agendas", "Syncs/Platform.md", "## Next")
    assert op["line"].startswith("- Who owns the on-call rotation")


def test_custom_option_set_with_guards_for_the_support_lead(persona, cli, pinned_now):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace(
        '        - { id: watch, action: watch, label: "Watch" }',
        '        - { id: handover, action: task.add, label: "Hand {title|truncate:20} to the next shift", '
        'when: "source_is:pager", destination: worklog }\n'
        '        - { id: reply, action: task.add, label: "Reply to the requester", when: "relation_is:external" }\n'
        '        - { id: watch, action: watch, label: "Watch" }'))
    run_id = start(cli, ws, "shift", pinned_now)
    step = ask(ws)
    tickets = [q for q in step["questions"] if q["prompt"].startswith("[tickets]")]
    assert tickets
    assert not any("handover" in [o["id"] for o in q["options"]] for q in step["questions"])  # no page asked
    assert all("reply" in [o["id"] for o in q["options"]] for q in tickets)   # customers are external
    t = tickets[0]
    labels = {o["id"]: o["label"] for o in t["options"]}
    assert labels["escalate"].startswith("Escalate to the ") and labels["escalate"].endswith(" lead")
    code, env = answer(cli, ws, t["id"], "escalate", "--due", "tue", "--note", "customer is waiting")
    assert code == 0, env
    assert env["data"]["action_preview"]["operations"][0]["destination"] == "worklog"
    assert env["data"]["preview"]["line"].endswith("| due=2026-10-20")
    code, env = answer(cli, ws, t["id"], "close", "--note", "duplicate of an older ticket")
    assert code == 0 and env["data"]["action_preview"]["outcome"]["decision"] == "dismiss"
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask" and s["stage"] == "approve")
    assert res.failures == []
    plan = plan_json(ws, run_id, res.final["questions"][0]["details"]["digest"])
    assert {"question": t["id"], "item": t["details"]["item_id"], "topic": plan["outcomes"][0]["topic"],
            "decision": "dismiss", "note": "duplicate of an older ticket", "until": None} in plan["outcomes"]


# --- answers ---------------------------------------------------------------------------------------


def test_every_override_is_echoed_normalized_and_reaches_the_line(persona, cli):
    ws = persona("reference-like")
    start(cli, ws)
    q = _forum_question(ask(ws))
    code, env = answer(cli, ws, q["id"], "1", "--due", "fri", "--est", "1.5h", "--when", "mon 09:00-11:00",
                       "--title", "On-call owner", "--action", "Name the on-call owner for next sprint",
                       "--link", "https://wiki.example/oncall", "--link", "https://wiki.example/rota",
                       "--area", "platform", "--forum", "platform", "--note", "ask Kim first")
    assert code == 0, env
    ov = env["data"]["answer"]["overrides"]
    assert ov == {"due": "2026-10-16", "est": 90, "when": "2026-10-19 09:00-11:00", "title": "On-call owner",
                  "action": "Name the on-call owner for next sprint",
                  "links": ["https://wiki.example/oncall", "https://wiki.example/rota"], "area": "Platform",
                  "forum": "platform-sync", "note": "ask Kim first"}
    assert env["data"]["answer"]["input"] == "1" and env["data"]["answer"]["est_normalized"] == "1h30m"
    assert "--when '2026-10-19 09:00-11:00'" in env["data"]["answer"]["normalized"]
    line = env["data"]["preview"]["line"]
    assert line == ("- [ ] Name the on-call owner for next sprint [source:: chat] [due:: 2026-10-16] [est:: 1h30m] "
                    "[links:: https://wiki.example/oncall, https://wiki.example/rota]")
    loads = env["data"]["action_preview"]["day_load"]
    assert [d["date"] for d in loads] == ["2026-10-16", "2026-10-19"]
    # the same decision in other words is not a change
    code, env = answer(cli, ws, q["id"], "task", "--due", "2026-10-16", "--est", "90", "--when",
                       "2026-10-19 09:00-11:00", "--title", "On-call owner", "--action",
                       "Name the on-call owner for next sprint", "--link", "https://wiki.example/oncall",
                       "--link", "https://wiki.example/rota", "--area", "Platform", "--forum", "Platform Sync",
                       "--note", "ask Kim first")
    assert code == 0 and env["data"]["changed"] is False


def test_ambiguous_and_unknown_options_and_bad_merges(persona, cli):
    ws = persona("reference-like")
    start(cli, ws)
    qs = [q["id"] for q in ask(ws)["questions"]]
    code, env = answer(cli, ws, qs[0], "d")
    assert code == 2 and env["error"]["code"] == "input.ambiguous_option"
    assert {c["id"] for c in env["error"]["details"]["candidates"]} == {"done", "dismiss"}
    code, env = answer(cli, ws, qs[0], "nothing like it")
    assert code == 2 and env["error"]["code"] == "input.unknown_option"
    code, env = answer(cli, ws, qs[0], "task", "--merge-with", qs[0])
    assert code == 2 and env["error"]["code"] == "input.bad_merge"
    code, env = answer(cli, ws, qs[0], "task", "--merge-with", "q_nope")
    assert code == 2 and env["error"]["code"] == "input.bad_merge"
    code, env = answer(cli, ws, qs[1], "task", "--merge-with", qs[2])
    assert code == 0
    code, env = answer(cli, ws, qs[0], "task", "--merge-with", qs[1])
    assert code == 2 and env["error"]["code"] == "input.bad_merge" and qs[1] in env["error"]["hint"]


def test_undo_and_merge_with(persona, cli):
    ws = persona("reference-like")
    start(cli, ws)
    qa, qb, qc = [q["id"] for q in ask(ws)["questions"]]
    assert answer(cli, ws, qa, "task", "--merge-with", qb)[0] == 0
    code, env = answer(cli, ws, qc, "dismiss")
    assert code == 0 and env["data"]["step"]["kind"] == "run"       # all three answered: plan is next
    code, env = cli("ask", "undo", qa, "--workspace", str(ws))
    assert code == 0 and env["data"]["removed"] == [qa, qb]
    step = env["data"]["step"]
    assert step["kind"] == "ask" and sorted(q["id"] for q in step["questions"]) == sorted([qa, qb])
    assert answer(cli, ws, qa, "task", "--merge-with", qb)[0] == 0
    code, env = cli("ask", "undo", qb, "--workspace", str(ws))
    assert code == 0 and env["data"]["removed"] == [qb]
    code, env = cli("ask", "show", qa, "--workspace", str(ws))
    assert "merge_with" not in env["data"]["answer"]["overrides"]
    code, env = cli("ask", "undo", qb, "--workspace", str(ws))
    assert code == 0 and env["data"]["changed"] is False


def test_batch_answers_from_a_file_are_all_or_nothing(persona, cli, tmp_path):
    ws = persona("reference-like")
    start(cli, ws)
    qa, qb, qc = [q["id"] for q in ask(ws)["questions"]]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"question": qa, "option": "task", "due": "monday"},
                               {"question": qb, "option": "task", "est": "forever"}]))
    code, env = answer(cli, ws, "--file", str(bad))
    assert code == 2 and env["error"]["code"] == "input.bad_duration" and env["error"]["details"]["row"] == 2
    code, env = cli("ask", "list", "--workspace", str(ws))
    assert not any(r["answer"] for r in env["data"]["questions"])
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"answers": [
        {"question": qa, "option": "task", "due": "monday", "est": "60m", "merge_with": qb},
        {"question": qc, "option": "watch", "note": "check again next week"}]}))
    code, env = answer(cli, ws, "--file", str(good))
    assert code == 0, env
    rows = env["data"]["answers"]
    assert [r["question_id"] for r in rows] == [qa, qc]
    assert rows[1]["action_preview"]["outcome"]["decision"] == "watch"
    assert env["data"]["step"]["kind"] == "run"


def test_refresh_keeps_unchanged_answers_and_reasks_changed_items_with_previous(persona, cli, pinned_now):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("workflows:\n", "workflows:\n" + """\
  reviewed:
    stages: [collect, assess, merge, review, decide, plan, approve, apply, close]
    sources: [tickets]
    questions: { kinds: [finding], budget: 40 }
    options: queue
""", 1))
    start(cli, ws, "reviewed", pinned_now)
    step = ask(ws)
    for q in step["questions"]:
        assert answer(cli, ws, q["id"], "take")[0] == 0
    asked = {q["id"] for q in step["questions"]}

    feed = ws / "data" / "tickets.jsonl"
    rows = [json.loads(x) for x in feed.read_text().splitlines()]
    changed = rows[0]
    with feed.open("a") as fh:
        fh.write(json.dumps({**changed, "description": "Customer report 0: the customer replied with a new date."})
                 + "\n")
    code, env = cli("collect", "--refresh", "--sources", "tickets", "--workspace", str(ws))
    assert code == 0, env
    step = ask(ws)
    reasked = {q["id"]: q for q in step["questions"]}
    assert len(reasked) == 1, list(reasked)
    (qid, q), = reasked.items()
    assert qid in asked and q["details"]["previous"]["option"] == "take"
    assert "re-asked" in step["why"]
    code, env = answer(cli, ws, qid, "close")
    assert code == 0 and env["data"]["step"]["kind"] == "run"
    stored = json.loads((state_root(ws, os.environ) / "runs" / env["data"]["step"]["command"]["argv"][
        env["data"]["step"]["command"]["argv"].index("--run") + 1] / "answers.json").read_text())
    assert stored[qid]["previous"]["option"] == "take"
    assert sum(1 for k, v in stored.items() if k in asked and v["option"] == "take") == len(asked) - 1


# --- reconcile -------------------------------------------------------------------------------------

SEED_KEY = "dk_t_61a4bf1529ea"


def _elapsed_seed(ws, carry=None):
    cal = ws / "data" / "calendar.json"
    data = json.loads(cal.read_text())
    for ev in data["events"]:
        if ev["id"] == "blk-seed":
            ev["start"], ev["end"] = "2026-10-15T11:00:00", "2026-10-15T11:30:00"
    cal.write_text(json.dumps(data))
    if carry is not None:
        bl = ws / "Backlog.md"
        bl.write_text(bl.read_text().replace("[source:: seed]", f"[source:: seed] [carry:: {carry}]"))


def test_reconcile_options_map_to_task_store_operations(persona, cli):
    ws = persona("reference-like")
    _elapsed_seed(ws)
    run_id = start(cli, ws)
    step = ask(ws)
    q = step["questions"][0]
    assert q["id"] == f"r_{SEED_KEY}" and q["kind"] == "reconcile"          # reconcile comes first
    assert q["prompt"] == 'Did "Review the ingest service runbook" happen (Thu 2026-10-15 11:00-11:30)?'
    assert [o["id"] for o in q["options"]] == ["done", "partly", "not_done", "to_agenda", "drop"]
    old = "- [ ] Review the ingest service runbook [source:: seed]"

    def ops(*args):
        code, env = answer(cli, ws, q["id"], *args)
        assert code == 0, env
        return env["data"]["action_preview"]["operations"]

    done = ops("done")
    assert [(o["verb"], o["old_line"], o["line"]) for o in done] == [
        ("replace", old, "- [x] Review the ingest service runbook [source:: seed]")]
    code, env = answer(cli, ws, q["id"], "partly")
    assert code == 2 and env["error"]["code"] == "input.missing_override" and "--est" in env["error"]["hint"]
    assert ops("partly", "--est", "30")[0]["line"] == old + " [carry:: 1] [est:: 30m]"
    assert ops("did not happen")[0]["line"] == old + " [carry:: 1]"
    assert ops("drop")[0]["line"].startswith("- [x] ")
    code, env = answer(cli, ws, q["id"], "to_agenda")
    assert code == 4 and env["error"]["code"] == "dest.not_accepted"      # no agenda file yet
    agenda = ws / "Syncs" / "Platform.md"
    agenda.parent.mkdir()
    agenda.write_text("# Platform Sync\n\n## Next\n")
    moved = ops("move it", "--forum", "platform sync")
    assert [(o["destination"], o["verb"]) for o in moved] == [("agendas", "append"), ("backlog", "replace")]
    assert moved[0]["line"] == "- Review the ingest service runbook"

    ops("done")
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    assert res.failures == [], res.failures
    code, env = cli("plan", "diff", "--workspace", str(ws))
    plan = plan_json(ws, run_id, env["data"]["plan_digest"])
    assert any(o.get("verb") == "replace" and o["question_id"] == q["id"] for o in plan["operations"])
    seed = [o["verb"] for o in plan["operations"] if o.get("key") == SEED_KEY]
    assert seed == ["release"]                                              # closed: its block is not kept
    assert any("change `- [ ] Review the ingest" in x for g in env["data"]["groups"] for x in g["lines"])


def test_reconcile_at_the_carry_limit_offers_rescope_instead_of_another_carry(persona, cli):
    ws = persona("reference-like")
    _elapsed_seed(ws, carry=3)
    start(cli, ws)
    q = ask(ws)["questions"][0]
    assert [o["id"] for o in q["options"]] == ["done", "partly", "rescope", "to_agenda", "drop"]
    assert any("Carried 3 times (limit 3)" in n for n in q["details"]["notes"])
    assert q["prompt"].endswith("Carried 3 time(s) so far.")
    code, env = answer(cli, ws, q["id"], "rescope")
    assert code == 2 and env["error"]["code"] == "input.missing_override"
    code, env = answer(cli, ws, q["id"], "re-scope", "--est", "45", "--title", "Skim the runbook's alert section")
    assert code == 0, env
    assert env["data"]["preview"]["line"] == "- [ ] Skim the runbook's alert section [source:: seed] [carry:: 0] [est:: 45m]"
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("decisions:\n", "decisions:\n  reconcile: { carry_limit: 5, labels: "
                                           "{ not_done: \"Carry it ({carry} of {carry_limit})\" } }\n", 1))
    code, env = cli("ask", "show", q["id"], "--workspace", str(ws))
    assert {o["id"]: o["label"] for o in env["data"]["options"]}["not_done"] == "Carry it (3 of 5)"
    cfg.write_text(cfg.read_text().replace("decisions:\n", "decisions:\n  order: [finding, reconcile]\n", 1))
    code, env = cli("ask", "list", "--workspace", str(ws))
    assert [r["kind"] for r in env["data"]["questions"]] == ["finding"] * 3 + ["reconcile"]


def test_reconcile_window_starts_at_the_last_sealed_run(persona, cli):
    ws = persona("reference-like")
    _elapsed_seed(ws)
    start(cli, ws)
    assert run_agent(ws).final["kind"] == "done"
    start(cli, ws, now="2026-10-16T09:00:00-05:00")
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    assert not any(q["kind"] == "reconcile" for q in (res.final.get("questions") or []))


# --- verification ----------------------------------------------------------------------------------

TRACKER = """
  tracker:
    extends: exec/jsonl-source
    description: An invented issue tracker, read only through resolve.
    transport: tracker-bridge
    resolve:
      patterns: ['\\b(?P<key>TRK-\\d+)\\b']
"""


def _tracker(ws, states, max_per_run=None):
    src = ws / "sources.yaml"
    text = src.read_text().replace("transports:\n", "transports:\n  tracker-bridge: { kind: replay, path: "
                                                     "recordings/tracker-bridge.jsonl }\n", 1)
    src.write_text(text + TRACKER)
    sig = ws / "signals.yaml"
    sig.write_text(sig.read_text().replace("  references:\n", "  references:\n    tracker: { pattern: "
                                                              "\"\\\\bTRK-\\\\d+\\\\b\" }\n", 1))
    rec = []
    for ref, result in states.items():
        rec.append({"request": {"op": "resolve", "args": {"ref": ref}},
                    "response": {"body_json": {"v": 1, "id": 1, "ok": True, "result": result}}})
    (ws / "recordings" / "tracker-bridge.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rec))
    bl = ws / "Backlog.md"
    bl.write_text(bl.read_text().replace("## Someday", "- [ ] Rotate the signing keys TRK-42 [source:: seed] "
                                                      "[created:: 2026-10-10]\n"
                                                      "- [ ] Chase the vendor contract TRK-43 [source:: seed] "
                                                      "[created:: 2026-10-14]\n"
                                                      "- [ ] Plan the capacity review TRK-44 [source:: seed]\n\n"
                                                      "## Someday"))
    if max_per_run is not None:
        cfg = ws / "docket.yaml"
        cfg.write_text(cfg.read_text().replace("decisions:\n", f"decisions:\n  verify: {{ max_per_run: {max_per_run} }}\n", 1))


STATES = {
    "TRK-42": {"state": "closed", "last_activity": "2026-10-14T10:00:00Z", "last_actor": "users/9",
               "title": "Rotate the signing keys"},
    "TRK-43": {"state": "open", "last_activity": "2026-10-15T15:00:00Z", "last_actor": "users/1001"},
    "TRK-44": {"state": "open", "last_activity": "2026-10-15T15:00:00Z", "last_actor": "users/3003"},
}


def test_verify_questions_come_from_resolve(persona, cli):
    ws = persona("reference-like")
    _tracker(ws, STATES)
    run_id = start(cli, ws)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    assert res.failures == [], res.failures
    resolve_steps = [s for s in res.steps if s["kind"] == "run" and s["command"]["argv"][1:3] == ["ask", "resolve"]]
    assert len(resolve_steps) == 1 and "no source reads" in resolve_steps[0]["why"]
    verify = [q for q in res.final["questions"] if q["kind"] == "verify"]
    assert sorted(q["prompt"] for q in verify) == [
        'Looks done: close "Chase the vendor contract TRK-43"? You were the last to act on it (2026-10-15).',
        'Looks done: close "Rotate the signing keys TRK-42"? tracker reports it closed.']
    assert res.final["questions"][-1]["kind"] == "verify"                  # after the findings
    by_title = {q["prompt"].split('"')[1]: q for q in verify}
    closed = by_title["Rotate the signing keys TRK-42"]
    code, env = answer(cli, ws, closed["id"], "close")
    assert code == 0 and env["data"]["preview"]["line"].startswith("- [x] Rotate the signing keys TRK-42")
    mine = by_title["Chase the vendor contract TRK-43"]
    code, env = answer(cli, ws, mine["id"], "update")
    assert code == 2 and env["error"]["code"] == "input.missing_override"
    code, env = answer(cli, ws, mine["id"], "update", "--due", "fri")
    assert code == 0 and env["data"]["preview"]["line"].endswith("[due:: 2026-10-16]")
    code, env = answer(cli, ws, mine["id"], "keep")
    assert code == 0 and env["data"]["action_preview"]["operations"] == []
    cache = json.loads((state_root(ws, os.environ) / "runs" / run_id / "decide" / "resolutions.json").read_text())
    assert sorted(cache) == ["TRK-42", "TRK-43", "TRK-44"] and cache["TRK-42"]["state"] == "closed"
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    assert res.failures == [], res.failures
    code, env = cli("plan", "diff", "--workspace", str(ws))
    plan = plan_json(ws, run_id, env["data"]["plan_digest"])
    changes = [o for o in plan["operations"] if o.get("verb") == "replace"]
    assert [o["question_id"] for o in changes] == [closed["id"]]
    assert any("kept open after verification" in n for n in plan["notes"])
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"


def test_verification_is_bounded_per_run(persona, cli):
    ws = persona("reference-like")
    _tracker(ws, STATES, max_per_run=1)
    run_id = start(cli, ws)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    assert res.failures == []
    cache = json.loads((state_root(ws, os.environ) / "runs" / run_id / "decide" / "resolutions.json").read_text())
    assert len(cache) == 1


# --- message drafts ---------------------------------------------------------------------------------


def test_a_draft_answer_previews_and_plans_a_message_draft(persona, cli, monkeypatch):
    import docket.destinations as dmod
    from docket.destinations.mcp import MemoryDrafts
    store = MemoryDrafts()
    real = dmod.env_for

    def env_for(ws, **kw):
        kw["call_tool"] = lambda dest_id: store if dest_id == "replies" else None
        return real(ws, **kw)
    monkeypatch.setattr(dmod, "env_for", env_for)
    ws = persona("reference-like")
    dest = ws / "destinations.yaml"
    dest.write_text(dest.read_text() + "  replies:\n    adapter: message_mcp\n    tools: { list: list, draft: draft }\n"
                    "    title_template: \"Re: {title|truncate:40}\"\n    notes_template: \"{note}\"\n")
    run_id = start(cli, ws)
    q = ask(ws)["questions"][0]
    assert "draft" in [o["id"] for o in q["options"]]
    code, env = answer(cli, ws, q["id"], "draft a reply", "--note", "Approved; send me the final numbers.")
    assert code == 0, env
    op = env["data"]["action_preview"]["operations"][0]
    assert (op["destination"], op["verb"], op["to"]) == ("replies", "draft", ["users/3003"])
    assert op["subject"].startswith("Re: Please approve the headcount") and op["body"].startswith("Approved;")
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["stage"] == "close")
    assert res.failures == [], res.failures
    code, env = cli("plan", "diff", "--workspace", str(ws))
    plan = plan_json(ws, run_id, env["data"]["plan_digest"])
    assert [o["verb"] for o in plan["operations"] if o["destination"] == "replies"] == ["draft"]
    assert any(x.startswith("draft `Re: Please approve") for g in env["data"]["groups"] for x in g["lines"])
    assert store.drafts == {}                                   # previewing writes nothing


def test_drafts_are_not_offered_without_an_mcp_transport(persona, cli):
    ws = persona("reference-like")
    dest = ws / "destinations.yaml"
    dest.write_text(dest.read_text() + "  replies:\n    adapter: message_mcp\n    tools: { list: list, draft: draft }\n")
    start(cli, ws)
    q = ask(ws)["questions"][0]
    refused = {r["option"]: r["reason"] for r in q["details"]["refused"]}
    assert refused["draft"] == "replies: no MCP transport is configured for this destination"
