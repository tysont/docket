"""What a question about an item carries for a card (TD §10.1): the item's link and every link
merged into it, its source and label, sender, time, score, bounded text, and what a review
changed. A question past a budget, and the overflow setting that decides its fate."""
from __future__ import annotations

import pytest

from docket.config.models import Questions
from docket.decide.questions import bounded, card, links_of, reviewed_of
from docket.text.grammar import web_links

pytestmark = pytest.mark.smoke


def test_text_is_bounded_at_a_word():
    assert bounded("  a  short\ntext ") == "a short text"
    long = " ".join(["word"] * 300)          # 1499 characters
    out = bounded(long)
    assert len(out) <= 600 and out.endswith("word\u2026")
    assert bounded("x" * 700, 600) == "x" * 599 + "\u2026"   # one long word is cut where it must be
    assert bounded(None) == ""


def test_links_are_the_item_s_then_its_members_then_what_a_review_merged_in_each_once():
    a = {"id": "a", "url": "https://chat.example.org/t/1",
         "members": [{"id": "a2", "url": "https://chat.example.org/t/2"},
                     {"id": "a3", "url": "https://chat.example.org/t/1"}],
         "merged": ["b", "missing"]}
    b = {"id": "b", "url": "https://mail.example.org/m/9", "merged": ["a"],
         "members": [{"id": "b2", "url": ""}, {"id": "b3"}]}
    assert links_of(a, {"a": a, "b": b}) == ["https://chat.example.org/t/1", "https://chat.example.org/t/2",
                                             "https://mail.example.org/m/9"]
    assert links_of({"id": "z"}) == []


def test_reviewed_says_what_a_review_changed_or_doubted():
    changed = {"id": "a", "verdict": "ask", "review": {"decision": "watch", "reason": "nothing is owed yet",
                                                       "against": "it may resolve itself"},
               "trace": [{"step": "agent"}, {"verb": "judge", "step": "reviewed", "was": "task", "verdict": "ask"}]}
    assert reviewed_of(changed) == {"was": "task", "now": "watch", "why": "nothing is owed yet"}
    same = {**changed, "trace": [{"step": "reviewed", "was": "watch"}]}
    assert reviewed_of(same) is None                       # the reviewer chose what was already recommended
    assert reviewed_of({"id": "a", "verdict": "ask", "review": {"against": "someone else may"}}) is None
    doubt = {"id": "a", "verdict": "needs_verification", "review": {"against": "the ticket may be closed",
                                                                    "reason": "check first"}}
    assert reviewed_of(doubt) == {"doubt": "the ticket may be closed", "why": "check first"}


def test_a_card_carries_link_source_sender_time_score_and_text():
    it = {"id": "a", "source": "mail", "url": "", "from": "Ana Example <ana@example.org>",
          "sender": {"display": "Ana Example"}, "at": "2026-10-15T09:00:00Z", "score": 72,
          "title": "Budget review", "text": "Please look at the budget. " * 40,
          "members": [{"id": "m", "url": "https://mail.example.org/m/2"}]}
    c = card(it, "mail thread")
    assert c["url"] == "https://mail.example.org/m/2" and c["links"] == ["https://mail.example.org/m/2"]
    assert (c["source"], c["source_label"], c["from"], c["at"], c["score"]) == \
        ("mail", "mail thread", "Ana Example", "2026-10-15T09:00:00Z", 72)
    assert len(c["text"]) <= 600 and c["text"].startswith("Please look at the budget.")
    assert "reviewed" not in c
    bare = card({"id": "b", "source": "chat", "title": "Only a title", "from": "Bo"}, "chat")
    assert bare["url"] == "" and bare["links"] == [] and bare["text"] == "Only a title" and bare["from"] == "Bo"


def test_a_task_line_or_agenda_entry_gives_its_web_links():
    line = "- [ ] Review [the doc](https://docs.example.org/d/1) and [[Notes/Plan]] [links:: https://t.example.org/T-2]"
    assert web_links([line]) == {"url": "https://docs.example.org/d/1",
                                 "links": ["https://docs.example.org/d/1", "https://t.example.org/T-2"]}
    assert web_links(["- Raise the budget", "  - see https://w.example.org/p and https://w.example.org/p"]) == \
        {"url": "https://w.example.org/p", "links": ["https://w.example.org/p"]}
    assert web_links(["- nothing linked"]) == {}


def test_overflow_is_list_or_drop_and_null_budgets_mean_no_limit():
    q = Questions.model_validate({"budget": None, "batch_budget": None})
    assert (q.budget, q.batch_budget, q.overflow) == (None, None, "list")
    assert Questions.model_validate({"budget": 3, "overflow": "drop"}).overflow == "drop"
    with pytest.raises(ValueError):
        Questions.model_validate({"overflow": "keep"})
    with pytest.raises(ValueError):
        Questions.model_validate({"budget": -1})


def test_null_budgets_ask_every_kept_item():
    from docket.decide.questions import select_asked

    def f(i, score, decision):
        return {"id": i, "keep": True, "score": score, "assessment": {"keep": True, "decision": decision}}
    items = [f(f"t{i}", 90 - i, "task") for i in range(30)] + [f(f"a{i}", 80 - i, "ack") for i in range(30)]
    asked, over = select_asked(items, 0, None, batch={"ack"}, batch_budget=None)
    assert len(asked) == 60 and over == []
    asked, over = select_asked(items, 0, 5, batch={"ack"}, batch_budget=None)
    assert len(asked) == 35 and len(over) == 25
