"""A drawing of a graph (TD §5.6): its nodes laid out left to right, for ``docket graph show``
and ``docket run status --graph``.

A node's column is the longest chain of nodes before it, so everything a node waits for
stands to its left. A node takes the row of the first node it waits for when that cell is
free, so a chain reads along one line; otherwise the first free row of its column. The seal
is drawn last, one column past the deepest node, on the row of the deepest node that no
other node waits for.

The text drawing has two lines per node: its name, and its verb in brackets that say who
does its work (``[read]`` docket computes, ``{judge}`` an agent judges, ``(ask)`` the
operator is asked). An edge between two neighbours on one row is drawn as a line between
them: solid when it hands on data (``from`` or the verb's own inputs), dotted when it only
orders (``after``). Every other edge is listed under the drawing, and the node it enters
carries ``+N``. Columns that do not fit the width wrap into bands, and a row that goes on
in the next band ends in ``»``. So a graph of twenty nodes in twelve columns stays inside
eighty characters and every edge is still shown.
"""
from __future__ import annotations

import math
import textwrap
from dataclasses import dataclass, field
from typing import Any

from docket.config.models import Node, ordered

SEAL = "seal"
WHO = {"computed": "docket", "judged": "agent", "asked": "operator"}
BRACKETS = {"computed": "[]", "judged": "{}", "asked": "()"}
NAME_CAP = 16


@dataclass
class Edge:
    src: str
    dst: str
    kind: str  # data | order


@dataclass
class Placed:
    name: str
    verb: str
    how: str
    column: int
    row: int
    needs: list[str] = field(default_factory=list)


@dataclass
class Layout:
    nodes: dict[str, Placed]   # in layout order (column, then graph order); the seal last
    edges: list[Edge]
    columns: int               # including the seal's
    rows: int

    def cell(self, column: int, row: int) -> Placed | None:
        for p in self.nodes.values():
            if p.column == column and p.row == row:
                return p
        return None


def edges_of(graph: dict[str, Node]) -> list[Edge]:
    """Every edge of the graph, the seal's included: data edges from a node's inputs and the
    read node its covered questions take meetings from, order edges from ``after``, and an
    order edge into the seal from every node no other node waits for."""
    out: list[Edge] = []
    for name in ordered(graph):
        n = graph[name]
        data = list(n.inputs)
        if n.verb == "ask" and n.questions.covered is not None:
            data.append(n.questions.covered.meetings)
        for other in n.needs:
            out.append(Edge(other, name, "data" if other in data else "order"))
    needed = {e.src for e in out}
    out += [Edge(name, SEAL, "order") for name in ordered(graph) if name not in needed]
    return out


def layout(graph: dict[str, Node]) -> Layout:
    from docket.engine.nodes import KINDS
    order = ordered(graph)
    col: dict[str, int] = {}
    for name in order:  # every node's needs come before it
        needs = graph[name].needs
        col[name] = 1 + max(col[x] for x in needs) if needs else 0
    taken: set[tuple[int, int]] = set()
    row: dict[str, int] = {}

    def free(c: int) -> int:
        r = 0
        while (c, r) in taken:
            r += 1
        return r

    for name in sorted(order, key=lambda n: (col[n], order.index(n))):
        needs = graph[name].needs
        want = row[needs[0]] if needs else None
        r = want if want is not None and (col[name], want) not in taken else free(col[name])
        row[name] = r
        taken.add((col[name], r))
    edges = edges_of(graph)
    sinks = [e.src for e in edges if e.dst == SEAL]
    deepest = max(sinks, key=lambda n: (col[n], -order.index(n)))
    seal_col = max(col.values()) + 1
    nodes = {name: Placed(name, graph[name].verb, KINDS[graph[name].verb].how, col[name], row[name],
                          list(graph[name].needs))
             for name in sorted(order, key=lambda n: (col[n], order.index(n)))}
    nodes[SEAL] = Placed(SEAL, SEAL, "computed", seal_col, row[deepest], sinks)
    return Layout(nodes, edges, seal_col + 1, max(row.values()) + 1)


# --- run states -------------------------------------------------------------

def run_states(report: list[dict[str, Any]], lay: Layout) -> dict[str, dict[str, Any]]:
    """Each node of a run as the drawing marks it: ``done``, ``ready`` (everything it needs is
    done and its work can be done now), ``waiting`` (on the nodes it is drawn waiting for that
    are not done) or ``blocked`` (a failure or a refusal holds it), from
    ``RunContext.node_report()``."""
    by = {r["node"]: r for r in report}
    out: dict[str, dict[str, Any]] = {}
    for r in report:
        st, reason = r["state"], r.get("reason")
        if st == "current":
            mark = "done"
        elif st == "blocked" and reason and reason.startswith("waiting on"):
            mark = "waiting"
        elif st in ("stale", "pending") or (st == "waiting" and reason in ("tasks", "answers", "resolve")):
            mark = "ready"
        else:
            mark = "blocked"
        row: dict[str, Any] = {"mark": mark, "state": st, "reason": reason}
        if mark == "waiting":
            needs = lay.nodes[r["node"]].needs if r["node"] in lay.nodes else r.get("needs") or []
            row["waiting_on"] = [n for n in needs if n in by and by[n]["state"] != "current"]
        out[r["node"]] = row
    return out


# --- text -------------------------------------------------------------------

@dataclass
class Glyphs:
    data: str
    data_head: str
    order: str
    order_head: str
    cont: str
    into_data: str
    into_order: str
    marks: dict[str, str]
    ellipsis: str


UNICODE = Glyphs("─", "▶", "┄", "▷", "»", "◀──", "◀┄┄",
                 {"done": "✓", "ready": "●", "waiting": "○", "blocked": "✗"}, "…")
ASCII = Glyphs("-", ">", ".", ">", ">>", "<--", "<..",
               {"done": "+", "ready": "!", "waiting": "?", "blocked": "x"}, "~")


def _clip(name: str, g: Glyphs) -> str:
    return name if len(name) <= NAME_CAP else name[:NAME_CAP - len(g.ellipsis)] + g.ellipsis


def draw(lay: Layout, *, width: int = 80, ascii: bool = False, states: dict[str, dict[str, Any]] | None = None,
         title: str | None = None) -> str:
    g = ASCII if ascii else UNICODE
    nodes = lay.nodes
    by_row: dict[int, list[Placed]] = {}
    for p in nodes.values():
        by_row.setdefault(p.row, []).append(p)
    for cells in by_row.values():
        cells.sort(key=lambda p: p.column)
    kind = {(e.src, e.dst): e.kind for e in lay.edges}
    inline: dict[tuple[str, str], str] = {}
    for cells in by_row.values():
        for a, b in zip(cells, cells[1:]):
            if (a.name, b.name) in kind:
                inline[(a.name, b.name)] = kind[(a.name, b.name)]
    listed = [e for e in lay.edges if (e.src, e.dst) not in inline]
    extra: dict[str, int] = {}
    for e in listed:
        extra[e.dst] = extra.get(e.dst, 0) + 1

    def label(p: Placed) -> str:
        name = _clip(p.name, g)
        return f"{g.marks[states[p.name]['mark']]} {name}" if states and p.name in states else name

    def tag(p: Placed) -> str:
        o, c = BRACKETS[p.how]
        return f"{o}{p.verb}{c}" + (f" +{extra[p.name]}" if p.name in extra else "")

    cw = max(6, *(max(len(label(p)), len(tag(p))) for p in nodes.values()))
    gap, left, gutter, tail = 5, 2, len(g.cont) + 3, len(g.cont) + 1
    per_band = max(1, (width - left - gutter - tail + gap) // (cw + gap))
    bands = math.ceil(lay.columns / per_band)
    sizes = [lay.columns // bands + (1 if i < lay.columns % bands else 0) for i in range(bands)]

    out: list[str] = []
    if title:
        out.append(title)
    out.append(f"{BRACKETS['computed'][0]}verb{BRACKETS['computed'][1]} docket computes  "
               f"{{verb}} an agent judges  (verb) the operator is asked")
    out.append(f"{g.data * 2}{g.data_head} hands on data  {g.order * 2}{g.order_head} only orders  "
               f"+N edges listed below" + (f"  {g.cont} goes on below" if bands > 1 else ""))
    if states:
        out.append("  ".join(f"{g.marks[m]} {w}" for m, w in
                             (("done", "done"), ("ready", "ready now"), ("waiting", "waiting"), ("blocked", "blocked"))))
    for b in range(bands):
        lo = sum(sizes[:b])
        hi = lo + sizes[b] - 1

        def x0(c: int) -> int:
            return left + gutter + (c - lo) * (cw + gap)

        right = x0(hi) + cw + 1
        out.append("")
        head = [" "] * (right + tail)
        for c in range(lo, hi + 1):
            s = str(c)
            head[x0(c):x0(c) + len(s)] = list(s)
        out.append("".join(head).rstrip())
        for r in range(lay.rows):
            cells = by_row.get(r, [])
            l1 = [" "] * (right + tail)
            l2 = [" "] * (right + tail)
            used = False
            for a, bb in zip(cells, cells[1:]):
                k = inline.get((a.name, bb.name))
                if k is None or bb.column < lo or a.column > hi:
                    continue
                fill, headc = (g.data, g.data_head) if k == "data" else (g.order, g.order_head)
                start = x0(a.column) + len(label(a)) + 1 if a.column >= lo else left
                if a.column < lo:
                    l1[left:left + len(g.cont)] = list(g.cont)
                    start = left + len(g.cont)
                end = x0(bb.column) - 2 if bb.column <= hi else right
                for x in range(start, end):
                    l1[x] = fill
                if bb.column <= hi:
                    l1[end] = headc
                else:
                    l1[right:right + len(g.cont)] = list(g.cont)
                used = True
            for p in cells:
                if lo <= p.column <= hi:
                    s, t = label(p), tag(p)
                    l1[x0(p.column):x0(p.column) + len(s)] = list(s)
                    l2[x0(p.column):x0(p.column) + len(t)] = list(t)
                    used = True
            if used:
                out.append("".join(l1).rstrip())
                if "".join(l2).strip():
                    out.append("".join(l2).rstrip())
    if listed:
        out += ["", "edges not drawn in the rows:"]
        targets = list(dict.fromkeys(e.dst for e in sorted(listed, key=lambda e: list(nodes).index(e.dst))))
        tw = max(len(t) for t in targets)
        for t in targets:
            parts = []
            for k, arrow in (("data", g.into_data), ("order", g.into_order)):
                srcs = [e.src for e in listed if e.dst == t and e.kind == k]
                if srcs:
                    parts.append(f"{arrow} {', '.join(srcs)}")
            out += textwrap.wrap(f"{t:<{tw}} " + "  ".join(parts), width=width, initial_indent="  ",
                                 subsequent_indent=" " * (tw + 7), break_on_hyphens=False)
    if states:
        out += [""] + status_lines(lay, states, width=width)
    return "\n".join(out)


def status_lines(lay: Layout, states: dict[str, dict[str, Any]], *, width: int = 80) -> list[str]:
    groups: dict[str, list[str]] = {"ready": [], "waiting": [], "blocked": []}
    for name in lay.nodes:
        s = states.get(name)
        if s is None or s["mark"] == "done":
            continue
        p = lay.nodes[name]
        if s["mark"] == "ready":
            groups["ready"].append(f"{name} ({WHO[p.how]})")
        elif s["mark"] == "waiting":
            on = s.get("waiting_on") or []
            groups["waiting"].append(f"{name} (on {', '.join(on)})" if on else name)
        else:
            groups["blocked"].append(f"{name} ({s.get('reason') or s['state']})")
    out = []
    for key, head in (("ready", "ready now: "), ("waiting", "waiting:   "), ("blocked", "blocked:   ")):
        if groups[key]:
            # one entry never breaks across lines
            text = head + ", ".join(x.replace(" ", "\u00a0") for x in groups[key])
            out += [ln.replace("\u00a0", " ") for ln in
                    textwrap.wrap(text, width=width, subsequent_indent=" " * len(head), break_on_hyphens=False)]
    if not out:
        out.append("every node is done")
    return out


# --- other forms --------------------------------------------------------------

def flat(rows: list[dict[str, Any]], states: dict[str, dict[str, Any]] | None = None) -> str:
    """The flat list: each node, its verb, who does it, and what it waits for."""
    out = []
    for r in rows:
        line = f"{r['node']} ({r['verb']}, {r['by']})" + \
            (f" <- {', '.join(r['from'] + r['after'])}" if r["from"] or r["after"] else "")
        if states and r["node"] in states:
            line += f" [{states[r['node']]['mark']}]"
        out.append(line)
    return "\n".join(out)


def mermaid(lay: Layout, states: dict[str, dict[str, Any]] | None = None) -> str:
    """A Mermaid flowchart: data edges solid, order edges dotted, one class per who does the work."""
    def nid(name: str) -> str:
        return SEAL if name == SEAL else "n_" + name.replace("-", "_")

    out = ["flowchart LR"]
    for p in lay.nodes.values():
        text = f"{p.name}<br/>{p.verb} · {WHO[p.how]}"
        if states and p.name in states:
            text += f" · {states[p.name]['mark']}"
        shape = ('(["', '"])') if p.name == SEAL else ('["', '"]')
        out.append(f"  {nid(p.name)}{shape[0]}{text}{shape[1]}:::{WHO[p.how]}")
    for e in lay.edges:
        out.append(f"  {nid(e.src)} {'-->' if e.kind == 'data' else '-.->'} {nid(e.dst)}")
    out += ["  classDef docket fill:#eef2f7,stroke:#4a5a70",
            "  classDef agent fill:#eef7ee,stroke:#3f6f3f",
            "  classDef operator fill:#fbf3e6,stroke:#8a6420"]
    return "\n".join(out)
