"""Questions a driver can show without reading an artifact, and nothing an ask node was given
dropped unseen (TD §5.2, §10.1, FR-DEC-10, FR-DEC-11).

A finding's details carry its item's link, every link merged into it, source, sender, time,
score and bounded text. Items past a question budget are still asked, flagged ``overflow``
and placed after the rest (``questions.overflow: list``, the default), or are not asked and
reported as ``not_asked`` on the ask step, in ``ask list``, in the seal step and in the run
summary (``drop``).
"""
from __future__ import annotations

import json

from tests.agent_loop import run_agent

GRAPH = """\
  cards:
    read-tickets: { read: tickets }
    read-pager: { read: pager }
    assess: { judge: assess, from: [read-tickets, read-pager] }
    sla-check: { judge: sla_check, from: [read-tickets] }
    merge: { merge: [assess, sla-check] }
    decide: { ask: queue, from: [merge], questions: QUESTIONS }
    plan: { plan: [decide] }
    approve: { approve: plan }
    apply: { apply: approve }
"""


def cards_ws(persona, cli, pinned_now, questions: str, sandbox: bool = True, under: str = "ws"):
    ws = persona("support-lead", under)
    cfg = ws / "docket.yaml"
    text = cfg.read_text().replace("graphs:\n", "graphs:\n" + GRAPH.replace("QUESTIONS", questions), 1)
    # every ticket links to its page
    cfg.write_text(text.replace('      title: "$.summary"\n', '      title: "$.summary"\n      url: "$.link"\n', 1))
    data = ws / "data" / "tickets.jsonl"
    rows = [json.loads(ln) for ln in data.read_text().splitlines() if ln.strip()]
    data.write_text("".join(json.dumps({**r, "link": f"https://tickets.example.org/browse/{r['key']}"}) + "\n"
                            for r in rows))
    args = ["run", "start", "cards", "--now", pinned_now, "--workspace", str(ws)] + (["--sandbox"] if sandbox else [])
    code, env = cli(*args)
    assert code == 0, env
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    assert res.failures == [] and res.final["kind"] == "ask", res.final
    return ws


def ask_step(env):
    (step,) = [s for s in env["data"]["steps"] if s["kind"] == "ask"]
    return step


def kept_count(persona, cli, pinned_now) -> int:
    ws = cards_ws(persona, cli, pinned_now, "{ kinds: [finding], budget: null, batch_budget: null }", under="all")
    code, env = cli("next", "--workspace", str(ws))
    step = ask_step(env)
    assert step["not_asked"] == {"count": 0}
    assert not any(q["details"].get("overflow") for q in step["questions"])
    return len(step["questions"])


def test_a_finding_carries_what_a_card_needs_in_full_show_and_list_but_not_compact(persona, cli, pinned_now):
    ws = cards_ws(persona, cli, pinned_now, "{ kinds: [finding], budget: null }")
    code, full = cli("next", "--full", "--workspace", str(ws))
    qs = ask_step(full)["questions"]
    with_url = [q for q in qs if q["details"].get("url")]
    assert with_url, [q["details"] for q in qs]
    q = with_url[0]
    d = q["details"]
    assert d["links"][0] == d["url"] and len(d["links"]) == len(set(d["links"]))
    assert d["source"] in ("tickets", "pager") and d["source_label"]
    assert d["at"] and isinstance(d["score"], int) and d["text"] and len(d["text"]) <= 600
    assert all(q["details"].get("source") for q in qs)
    code, compact = cli("next", "--workspace", str(ws))
    for c in ask_step(compact)["questions"]:
        assert not {"url", "links", "text", "from", "at", "source"} & set(c["details"]), c["details"]
    code, shown = cli("ask", "show", q["id"], "--workspace", str(ws))
    assert code == 0 and shown["data"]["url"] == d["url"] and shown["data"]["links"] == d["links"]
    assert shown["data"]["text"] == d["text"] and shown["data"]["score"] == d["score"]
    code, listed = cli("ask", "list", "--workspace", str(ws))
    row = next(r for r in listed["data"]["questions"] if r["id"] == q["id"])
    assert {k: row[k] for k in ("url", "links", "source", "source_label", "at", "score", "text")} == \
        {k: d[k] for k in ("url", "links", "source", "source_label", "at", "score", "text")}
    assert listed["data"]["not_asked"] == []


def test_null_budgets_mean_no_limit(persona, cli, pinned_now):
    assert kept_count(persona, cli, pinned_now) > 4


def test_items_past_the_budget_are_asked_after_the_rest_flagged_overflow(persona, cli, pinned_now, tmp_path):
    total = kept_count(persona, cli, pinned_now)
    ws = cards_ws(persona, cli, pinned_now, "{ kinds: [finding], budget: 3, batch_budget: 0 }")
    code, env = cli("next", "--workspace", str(ws))
    step = ask_step(env)
    flags = [bool(q["details"].get("overflow")) for q in step["questions"]]
    assert len(flags) == total and flags[:3] == [False] * 3 and all(flags[3:]) and len(flags) > 3
    assert step["not_asked"] == {"count": 0} and "past the question budget" in step["why"]
    code, full = cli("next", "--full", "--workspace", str(ws))
    assert ask_step(full)["not_asked"] == {"count": 0, "items": []}
    code, listed = cli("ask", "list", "--workspace", str(ws))
    assert [bool(r.get("overflow")) for r in listed["data"]["questions"]] == flags
    done = run_agent(ws)
    assert done.failures == [] and done.final["kind"] == "done"
    assert done.final["summary"]["not_asked"] == 0 and done.final["summary"]["questions"] == total


def test_with_drop_the_items_past_the_budget_are_reported_everywhere_up_to_the_seal(persona, cli, pinned_now):
    total = kept_count(persona, cli, pinned_now)
    ws = cards_ws(persona, cli, pinned_now, "{ kinds: [finding], budget: 3, batch_budget: 0, overflow: drop }")
    code, env = cli("next", "--workspace", str(ws))
    step = ask_step(env)
    cut = total - 3
    assert len(step["questions"]) == 3 and step["not_asked"] == {"count": cut}
    assert "not asked (not_asked)" in step["why"]
    code, full = cli("next", "--full", "--workspace", str(ws))
    na = ask_step(full)["not_asked"]
    assert na["count"] == cut and len(na["items"]) == cut
    row = na["items"][0]
    assert set(row) == {"item_id", "title", "score", "recommended", "url", "reason"}
    assert row["title"] and row["reason"] in ("past the question budget", "past the batch budget")
    assert [r["score"] for r in na["items"]] == sorted((r["score"] for r in na["items"]), reverse=True) or \
        any(r["reason"] == "past the batch budget" for r in na["items"])
    code, listed = cli("ask", "list", "--workspace", str(ws))
    assert [r["item_id"] for r in listed["data"]["not_asked"]] == [r["item_id"] for r in na["items"]]
    assert all(r["node"] == "decide" for r in listed["data"]["not_asked"])
    assert f"not asked ({cut})" in cli("ask", "list", "--workspace", str(ws), json_out=False)[1]
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "run" and s["node"] == "seal")
    assert res.failures == [] and f"{cut} item(s) kept were never put to the operator" in res.final["why"]
    done = run_agent(ws)
    assert done.failures == [] and done.final["summary"]["not_asked"] == cut


# --- questions about a task line or an agenda entry -------------------------------------------


def test_a_reconcile_question_carries_the_task_line_s_link(persona, cli):
    from tests.scenario.test_decisions_scenario import SEED_KEY, _elapsed_seed, ask, start
    ws = persona("reference-like")
    _elapsed_seed(ws)
    bl = ws / "Backlog.md"
    bl.write_text(bl.read_text().replace("[source:: seed]", "[source:: seed] [ref:: https://wiki.example.org/runbook]"))
    start(cli, ws)
    q = ask(ws)["questions"][0]
    assert q["id"] == f"r_{SEED_KEY}"
    assert q["details"]["url"] == "https://wiki.example.org/runbook"
    assert q["details"]["links"] == ["https://wiki.example.org/runbook"]
    code, env = cli("ask", "show", q["id"], "--workspace", str(ws))
    assert env["data"]["url"] == "https://wiki.example.org/runbook"


def test_a_covered_question_carries_the_agenda_entry_s_link(persona, cli, pinned_now):
    from tests.scenario.test_covered_scenario import setup, start
    ws = persona("reference-like")
    note = setup(ws)
    note.write_text(note.read_text().replace("- Hiring\n", "- Hiring, see [the req](https://jobs.example.org/r/7)\n"))
    start(cli, ws, pinned_now)
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask", full=True)
    by_text = {q["prompt"].split('"')[1]: q for q in res.final["questions"]}
    hiring = next(q for t, q in by_text.items() if t.startswith("Hiring"))
    assert hiring["details"]["url"] == "https://jobs.example.org/r/7"
    assert "url" not in by_text["Capacity plan"]["details"]
