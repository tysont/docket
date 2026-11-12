"""The drawing of a graph (TD §5.6): layout, text in Unicode and ASCII, Mermaid, and run marks."""
from __future__ import annotations

from typing import Any

import pytest

from docket.config.models import Node
from docket.engine.drawing import SEAL, draw, edges_of, layout, mermaid, run_states

pytestmark = pytest.mark.smoke

SMALL: dict[str, dict[str, Any]] = {
    "gather": {"read": "inbox"},
    "assess": {"judge": "assess", "from": ["gather"]},
    "merge": {"merge": ["assess"]},
    "decide": {"ask": "default", "from": ["merge"]},
    "plan": {"plan": ["decide"]},
    "approve": {"approve": "plan"},
    "apply": {"apply": "approve"},
    "touch": {"run": ["true"]},
}

# Five nodes with nothing upstream (one run, four reads); a read -> judge -> write -> read ->
# judge chain; three other read -> judge pairs; a merge of the four judges; a review; an ask
# after the write and the run node; a label; a plan from the ask and the label, after the run
# node; approve; apply; and the seal.
BIG: dict[str, dict[str, Any]] = {
    "read-a": {"read": "alpha"},
    "prep": {"run": ["true"]},
    "read-b": {"read": "beta"},
    "read-c": {"read": "gamma"},
    "read-d": {"read": "delta"},
    "judge-a": {"judge": "assess", "from": ["read-a"]},
    "write-a": {"write": "roll", "from": ["judge-a"], "to": "notes"},
    "read-e": {"read": "epsilon", "after": ["write-a"]},
    "judge-e": {"judge": "assess", "from": ["read-e"]},
    "judge-b": {"judge": "assess", "from": ["read-b"]},
    "judge-c": {"judge": "assess", "from": ["read-c"]},
    "judge-d": {"judge": "assess", "from": ["read-d"]},
    "merge": {"merge": ["judge-e", "judge-b", "judge-c", "judge-d"]},
    "review": {"judge": "review", "from": ["merge"]},
    "ask": {"ask": "default", "from": ["review"], "after": ["write-a", "prep"]},
    "label": {"judge": "label", "from": ["ask"]},
    "plan": {"plan": ["ask", "label"], "after": ["prep"]},
    "approve": {"approve": "plan"},
    "apply": {"apply": "approve"},
}


def graph(spec: dict[str, dict[str, Any]]) -> dict[str, Node]:
    return {k: Node.model_validate(v) for k, v in spec.items()}


SMALL_TEXT = """\
graph small
[verb] docket computes  {verb} an agent judges  (verb) the operator is asked
──▶ hands on data  ┄┄▷ only orders  +N edges listed below  » goes on below

      0             1             2             3
      gather ─────▶ assess ─────▶ merge ──────▶ decide ───»
      [read]        {judge}       [merge]       (ask)
      touch
      [run]

      4             5             6             7
  »─▶ plan ───────▶ approve ────▶ apply ┄┄┄┄┄┄▷ seal
      [plan]        (approve)     [apply]       [seal] +1

edges not drawn in the rows:
  seal ◀┄┄ touch"""

BIG_TEXT = """\
graph big
[verb] docket computes  {verb} an agent judges  (verb) the operator is asked
──▶ hands on data  ┄┄▷ only orders  +N edges listed below  » goes on below

      0              1              2              3              4
      read-a ──────▶ judge-a ─────▶ write-a ┄┄┄┄┄▷ read-e ──────▶ judge-e ───»
      [read]         {judge}        [write]        [read]         {judge}
      prep
      [run]
      read-b ──────▶ judge-b
      [read]         {judge}
      read-c ──────▶ judge-c
      [read]         {judge}
      read-d ──────▶ judge-d
      [read]         {judge}

      5              6              7              8
  »─▶ merge ───────▶ review ──────▶ ask ─────────▶ label ─────»
      [merge] +3     {judge}        (ask) +2       {judge}

      9              10             11             12
  »─▶ plan ────────▶ approve ─────▶ apply ┄┄┄┄┄┄┄▷ seal
      [plan] +2      (approve)      [apply]        [seal]

edges not drawn in the rows:
  merge ◀── judge-b, judge-c, judge-d
  ask   ◀┄┄ write-a, prep
  plan  ◀── ask  ◀┄┄ prep"""

BIG_ASCII = """\
graph big
[verb] docket computes  {verb} an agent judges  (verb) the operator is asked
--> hands on data  ..> only orders  +N edges listed below  >> goes on below

       0              1              2              3              4
       read-a ------> judge-a -----> write-a .....> read-e ------> judge-e --->>
       [read]         {judge}        [write]        [read]         {judge}
       prep
       [run]
       read-b ------> judge-b
       [read]         {judge}
       read-c ------> judge-c
       [read]         {judge}
       read-d ------> judge-d
       [read]         {judge}

       5              6              7              8
  >>-> merge -------> review ------> ask ---------> label ----->>
       [merge] +3     {judge}        (ask) +2       {judge}

       9              10             11             12
  >>-> plan --------> approve -----> apply .......> seal
       [plan] +2      (approve)      [apply]        [seal]

edges not drawn in the rows:
  merge <-- judge-b, judge-c, judge-d
  ask   <.. write-a, prep
  plan  <-- ask  <.. prep"""

SMALL_MERMAID = """\
flowchart LR
  n_gather["gather<br/>read · docket"]:::docket
  n_touch["touch<br/>run · docket"]:::docket
  n_assess["assess<br/>judge · agent"]:::agent
  n_merge["merge<br/>merge · docket"]:::docket
  n_decide["decide<br/>ask · operator"]:::operator
  n_plan["plan<br/>plan · docket"]:::docket
  n_approve["approve<br/>approve · operator"]:::operator
  n_apply["apply<br/>apply · docket"]:::docket
  seal(["seal<br/>seal · docket"]):::docket
  n_gather --> n_assess
  n_assess --> n_merge
  n_merge --> n_decide
  n_decide --> n_plan
  n_plan --> n_approve
  n_approve --> n_apply
  n_apply -.-> seal
  n_touch -.-> seal
  classDef docket fill:#eef2f7,stroke:#4a5a70
  classDef agent fill:#eef7ee,stroke:#3f6f3f
  classDef operator fill:#fbf3e6,stroke:#8a6420"""

SMALL_RUN = """\
run r1 of graph small (open)
[verb] docket computes  {verb} an agent judges  (verb) the operator is asked
──▶ hands on data  ┄┄▷ only orders  +N edges listed below  » goes on below
✓ done  ● ready now  ○ waiting  ✗ blocked

      0             1             2             3
      ✓ gather ───▶ ● assess ───▶ ○ merge ────▶ ○ decide ─»
      [read]        {judge}       [merge]       (ask)
      ● touch
      [run]

      4             5             6             7
  »─▶ ○ plan ─────▶ ○ approve ──▶ ✗ apply ┄┄┄┄▷ ○ seal
      [plan]        (approve)     [apply]       [seal] +1

edges not drawn in the rows:
  seal ◀┄┄ touch

ready now: touch (docket), assess (agent)
waiting:   merge (on assess), decide (on merge), plan (on decide),
           approve (on plan), seal (on apply, touch)
blocked:   apply (apply_partial)"""

# What RunContext.node_report() says of a run of SMALL partway through.
REPORT = [
    {"node": "gather", "state": "current", "reason": None, "needs": []},
    {"node": "touch", "state": "pending", "reason": "not built", "needs": []},
    {"node": "assess", "state": "waiting", "reason": "tasks", "needs": ["gather"]},
    {"node": "merge", "state": "blocked", "reason": "waiting on assess", "needs": ["assess"]},
    {"node": "decide", "state": "blocked", "reason": "waiting on merge", "needs": ["merge"]},
    {"node": "plan", "state": "blocked", "reason": "waiting on decide", "needs": ["decide"]},
    {"node": "approve", "state": "blocked", "reason": "waiting on plan", "needs": ["plan"]},
    {"node": "apply", "state": "waiting", "reason": "apply_partial", "needs": ["approve"]},
    {"node": "seal", "state": "blocked", "reason": "waiting on touch", "needs": []},
]


def test_columns_are_the_longest_chain_before_a_node_and_a_chain_keeps_its_row() -> None:
    lay = layout(graph(BIG))
    at = {n: (p.column, p.row) for n, p in lay.nodes.items()}
    assert at["read-a"] == (0, 0) and at["prep"] == (0, 1) and at["read-d"] == (0, 4)
    chain = ["read-a", "judge-a", "write-a", "read-e", "judge-e", "merge", "review", "ask", "label", "plan",
             "approve", "apply", SEAL]
    assert [at[n] for n in chain] == [(c, 0) for c in range(13)]
    assert at["judge-b"] == (1, 2) and at["judge-c"] == (1, 3) and at["judge-d"] == (1, 4)
    assert lay.columns == 13 and lay.rows == 5
    assert lay.nodes[SEAL].needs == ["apply"]


def test_a_node_takes_the_first_free_row_when_its_first_input_s_row_is_taken() -> None:
    lay = layout(graph({"a": {"read": "x"}, "b": {"judge": "assess", "from": ["a"]},
                        "c": {"judge": "tone", "from": ["a"]}}))
    assert (lay.nodes["b"].column, lay.nodes["b"].row) == (1, 0)
    assert (lay.nodes["c"].column, lay.nodes["c"].row) == (1, 1)
    assert lay.nodes[SEAL].needs == ["b", "c"] and lay.nodes[SEAL].row == 0


def test_edges_carry_data_from_inputs_and_covered_meetings_and_only_order_from_after() -> None:
    g = graph({"meet": {"read": "notes"}, "gather": {"read": "mail"}, "prep": {"run": ["true"]},
               "decide": {"ask": "default", "from": ["gather"], "after": ["prep"],
                          "questions": {"kinds": ["covered"],
                                        "covered": {"meetings": "meet", "to": "agendas"}}}})
    kinds = {(e.src, e.dst): e.kind for e in edges_of(g)}
    assert kinds == {("gather", "decide"): "data", ("prep", "decide"): "order", ("meet", "decide"): "data",
                     ("decide", SEAL): "order"}


def test_small_graph_drawing() -> None:
    assert draw(layout(graph(SMALL)), title="graph small") == SMALL_TEXT


def test_twenty_node_graph_drawing_fits_eighty_columns() -> None:
    text = draw(layout(graph(BIG)), title="graph big")
    assert text == BIG_TEXT
    assert max(len(ln) for ln in text.splitlines()) <= 80


def test_ascii_drawing_uses_only_ascii() -> None:
    text = draw(layout(graph(BIG)), ascii=True, title="graph big")
    assert text == BIG_ASCII
    assert text.isascii()


def test_every_edge_is_drawn_once_inline_or_listed() -> None:
    lay = layout(graph(BIG))
    text = draw(lay)
    listed = text.split("edges not drawn in the rows:")[1]
    n_listed = sum(len(part.split(",")) for ln in listed.splitlines() for part in ln.split("◀")[1:])
    inline = text.split("edges not drawn")[0]
    n_inline = sum(inline.count(h) for h in ("▶ ", "▷ ")) - 1 - 1   # the legend's two arrows
    # an edge that crosses into the next band is drawn once, its head in the band it enters
    assert n_listed + n_inline == len(lay.edges) == 22


def test_a_wider_terminal_takes_fewer_bands_and_a_narrow_one_more() -> None:
    lay = layout(graph(BIG))
    assert "»" not in draw(lay, width=220)
    narrow = draw(lay, width=40)
    assert max(len(ln) for ln in narrow.splitlines()[3:]) <= 40


def test_long_names_are_clipped_in_the_drawing() -> None:
    lay = layout(graph({"gather-every-message-of-the-week": {"read": "x"}}))
    text = draw(lay)
    assert "gather-every-me…" in text and "gather-every-mes" not in text


def test_mermaid_has_solid_data_edges_dotted_order_edges_and_a_class_per_worker() -> None:
    assert mermaid(layout(graph(SMALL))) == SMALL_MERMAID
    big = mermaid(layout(graph(BIG)))
    assert big.count(" --> ") == 17 and big.count(" -.-> ") == 5
    assert big.count(":::agent") == 7 and big.count(":::operator") == 2


def test_run_states_mark_done_ready_waiting_and_blocked() -> None:
    lay = layout(graph(SMALL))
    st = run_states(REPORT, lay)
    assert {n: s["mark"] for n, s in st.items()} == {
        "gather": "done", "touch": "ready", "assess": "ready", "merge": "waiting", "decide": "waiting",
        "plan": "waiting", "approve": "waiting", "apply": "blocked", "seal": "waiting"}
    assert st["seal"]["waiting_on"] == ["apply", "touch"] and st["merge"]["waiting_on"] == ["assess"]


def test_run_overlay_drawing() -> None:
    lay = layout(graph(SMALL))
    assert draw(lay, states=run_states(REPORT, lay), title="run r1 of graph small (open)") == SMALL_RUN
