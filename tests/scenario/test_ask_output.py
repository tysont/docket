"""Ask output size and the batch answers file (AE-07, AE-08, AE-09, FR-DEC-07).

An ask step is compact by default: per question its id, kind, prompt and option ids and
labels, with one answer template for the whole step whose only placeholders are the
question id and the option. ``docket next --full`` keeps every option's command and the
question details. Answer responses carry the recorded answer, its preview and a brief of
the next step, never the whole burst.
"""
from __future__ import annotations

import json

from tests.agent_loop import fill_template, run_agent

BURST = """\
  burst:
    stages: [collect, assess, merge, decide, plan, approve, apply, close]
    sources: [tickets, pager]
    questions: { kinds: [finding], budget: 15 }
    options: queue
"""


def burst_ws(persona, cli, pinned_now):
    ws = persona("support-lead")
    cfg = ws / "docket.yaml"
    cfg.write_text(cfg.read_text().replace("workflows:\n", "workflows:\n" + BURST, 1))
    code, env = cli("run", "start", "burst", "--now", pinned_now, "--workspace", str(ws))
    assert code == 0, env
    res = run_agent(ws, stop_at=lambda s: s["kind"] == "ask")
    assert res.failures == [] and res.final["kind"] == "ask", res.final
    return ws


def test_an_ask_step_with_15_questions_is_under_12_kb(persona, cli, pinned_now):
    ws = burst_ws(persona, cli, pinned_now)
    code, env = cli("next", "--workspace", str(ws))
    assert code == 0 and len(env["data"]["step"]["questions"]) == 15
    assert len(cli.last_stdout.encode("utf-8")) < 12 * 1024, len(cli.last_stdout)


def test_ask_step_is_compact_with_one_concrete_template(persona, cli, pinned_now):
    ws = burst_ws(persona, cli, pinned_now)
    code, env = cli("next", "--workspace", str(ws))
    step = env["data"]["step"]
    for q in step["questions"]:
        assert set(q) <= {"id", "kind", "prompt", "options", "details"}, set(q)
        assert all(set(o) == {"id", "label"} for o in q["options"]), q["options"]
        assert "evidence" not in q.get("details", {}) and "defaults" not in q.get("details", {})
    tpl = step["answer"]["argv"]
    assert tpl[:5] == ["docket", "ask", "answer", "{question}", "{option}"]
    rest = tpl[5:]
    assert str(ws) in rest and "--run" in rest and "--json" in rest
    assert not any("{" in a or "<" in a for a in rest)  # concrete apart from the two placeholders
    assert step["show"]["argv"][:4] == ["docket", "ask", "show", "{question}"]
    assert "{question}" in step["why"] and "{option}" in step["why"] and "ask show" in step["why"]
    # the template, filled, is exactly the command --full gives for that option
    code, full = cli("next", "--full", "--workspace", str(ws))
    fq = full["data"]["step"]["questions"][0]
    assert fq["details"]["evidence"] and fq["options"][0]["answer"]["argv"] == \
        fill_template(tpl, step["questions"][0]["id"], step["questions"][0]["options"][0]["id"])
    code, shown = cli(*fill_template(step["show"]["argv"], fq["id"], "")[1:])
    assert code == 0 and shown["data"]["evidence"] == fq["details"]["evidence"]


def test_answer_response_is_the_answer_its_preview_and_a_brief_of_the_next_step(persona, cli, pinned_now):
    ws = burst_ws(persona, cli, pinned_now)
    code, env = cli("next", "--workspace", str(ws))
    step = env["data"]["step"]
    q = step["questions"][0]
    code, env = cli(*fill_template(step["answer"]["argv"], q["id"], "take")[1:])
    assert code == 0, env
    data = env["data"]
    assert data["question_id"] == q["id"] and data["answer"]["option"] == "take"
    assert data["preview"]["line"].startswith("TODO ")
    brief = data["step"]
    assert brief["kind"] == "ask" and brief["counts"] == {"questions": 14}
    assert len(brief["question_ids"]) == 14 and "questions" not in brief
    assert brief["next"]["argv"][1] == "next"
    assert len(cli.last_stdout.encode("utf-8")) < 4 * 1024, len(cli.last_stdout)


# --- the batch answers file (finding: the format was undocumented) ------------------------------


def test_answers_file_schema_and_help_example(cli):
    code, env = cli("schema", "list")
    assert "answers-file" in env["data"]["schemas"]
    code, env = cli("schema", "answers-file")
    text = json.dumps(env["data"]["schema"])
    assert code == 0 and "question_id" in text and "option" in text
    code, env = cli("help", "ask", "answer")
    inputs = env["data"]["command"]["inputs"]
    ex = inputs["--file"]
    assert ex["schema"] == "answers-file" and ex["example"]["answers"][0]["question"].startswith("q_")


def test_answers_file_accepts_question_id_and_names_the_schema_when_a_row_has_neither(persona, cli, pinned_now,
                                                                                       tmp_path):
    ws = burst_ws(persona, cli, pinned_now)
    code, env = cli("next", "--workspace", str(ws))
    qa, qb = [q["id"] for q in env["data"]["step"]["questions"][:2]]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"question_id": qa, "option": "take"}, {"option": "watch"}]))
    code, env = cli("ask", "answer", "--file", str(bad), "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.bad_json" and env["error"]["details"]["row"] == 2
    assert "docket schema answers-file" in env["error"]["hint"]
    good = tmp_path / "good.json"
    good.write_text(json.dumps([{"question_id": qa, "option": "take"}, {"question": qb, "option": "watch"}]))
    code, env = cli("ask", "answer", "--file", str(good), "--workspace", str(ws))
    assert code == 0, env
    assert [r["question_id"] for r in env["data"]["answers"]] == [qa, qb]
