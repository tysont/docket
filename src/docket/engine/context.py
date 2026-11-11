"""``RunContext``: one run of a graph, seen through the current workspace configuration.

It evaluates the graph once per command and memoizes the result; a mutating command calls
``invalidate()`` after it writes. A node is evaluated when every node it needs is current;
otherwise it is blocked. Nodes that do not depend on each other are independent here too:
one that waits holds back only the nodes downstream of it.
"""
from __future__ import annotations

from typing import Any, Mapping

from docket.cli.errors import InputError
from docket.config import Workspace
from docket.config.digest import digest
from docket.config.loader import closest
from docket.config.models import Node, ordered
from docket.engine.nodes import SEAL, BaseNode, Eval, build, join
from docket.store.runs import Run


class RunContext:
    def __init__(self, ws: Workspace, run: Run, env: Mapping[str, str]) -> None:
        self.ws = ws
        self.run = run
        self.env = env
        graph = ws.config.graphs.get(run.header.graph)
        if graph is None:
            raise InputError("input.unknown_graph",
                             f"Run {run.id} uses graph {run.header.graph!r}, which the workspace no longer defines.",
                             hint="Restore the graph in docket.yaml or abandon the run",
                             details={"suggestion": closest(run.header.graph, list(ws.config.graphs))})
        self.graph: dict[str, Node] = graph
        self.order: list[str] = ordered(graph) + [SEAL]
        self.impls: dict[str, BaseNode] = {n: build(n, graph.get(n)) for n in self.order}
        self._evals: dict[str, Eval] | None = None

    # --- commands and config -------------------------------------------------
    def argv(self, *words: str) -> list[str]:
        return ["docket", *words, "--workspace", str(self.ws.root), "--run", self.run.id, "--json"]

    def partial_config(self, sections: tuple[str, ...]) -> str:
        return digest({s: self.ws.sections.get(s, digest(None)) for s in sections})

    def clock_view(self, *, with_time: bool) -> dict[str, Any]:
        c = self.run.header.clock
        out = {"date": c.date, "timezone": c.timezone}
        if with_time:
            out["instant"] = c.instant
        return out

    def graph_name(self) -> str:
        return self.run.header.graph

    # --- the graph ------------------------------------------------------------
    def needs(self, name: str) -> list[str]:
        """The nodes ``name`` waits for. The seal waits for every node."""
        return list(self.graph) if name == SEAL else self.graph[name].needs

    def inputs(self, name: str) -> list[str]:
        return [] if name == SEAL else self.graph[name].inputs

    def ancestors(self, name: str) -> set[str]:
        out: set[str] = set()
        todo = list(self.needs(name))
        while todo:
            n = todo.pop()
            if n not in out:
                out.add(n)
                todo += self.needs(n)
        return out

    def by_verb(self, verb: str) -> list[str]:
        """The graph's nodes of one verb, in order."""
        return [n for n in self.order if n != SEAL and self.graph[n].verb == verb]

    def sources(self) -> list[str]:
        """The sources this graph reads."""
        return [self.graph[n].read for n in self.by_verb("read")]  # type: ignore[misc]

    # --- evaluation ----------------------------------------------------------
    def evaluate(self) -> list[Eval]:
        if self._evals is None:
            from docket.apply import boundary
            evs: dict[str, Eval] = {}
            pins = boundary.pins(self.run)
            for name in self.order:
                impl = self.impls[name]
                waits = [n for n in self.needs(name) if evs[n].state != "current"]
                if waits and name not in pins:
                    evs[name] = Eval(name, impl.verb, "blocked", reason=f"waiting on {', '.join(waits)}",
                                     extra={"needs": waits}, how=impl.how)
                    continue
                if name in pins:
                    # TD §5.4: a node upstream of an applied write is read as it was when the write began.
                    ev = None if waits else impl.evaluate(self, join([evs[n] for n in self.inputs(name)]))
                    if ev is None or ev.state != "current" or ev.digest != pins[name]:
                        pinned = boundary.pinned_eval(self, impl, pins[name])
                        if pinned is not None:
                            pinned.extra["fresh_state"] = ev.state if ev is not None else "blocked"
                            evs[name] = pinned
                            continue
                        if ev is None:
                            ev = Eval(name, impl.verb, "blocked", reason=f"waiting on {', '.join(waits)}", how=impl.how)
                        if ev.state != "current":
                            ev.extra["underlying"] = ev.state
                            ev.state, ev.reason = "blocked", "post_apply_change"
                    evs[name] = ev
                    continue
                evs[name] = impl.evaluate(self, join([evs[n] for n in self.inputs(name)]))
            self._evals = evs
        return [self._evals[n] for n in self.order]

    def invalidate(self) -> None:
        self._evals = None

    def eval(self, name: str) -> Eval:
        self.evaluate()
        assert self._evals is not None
        if name not in self._evals:
            raise KeyError(name)
        return self._evals[name]

    def eval_or_none(self, name: str | None) -> Eval | None:
        try:
            return self.eval(name) if name else None
        except KeyError:
            return None

    def upstream(self, name: str) -> Eval | None:
        """What ``name`` works on: the outputs of its input nodes, as one."""
        return join([self.eval(n) for n in self.inputs(name)])

    def of(self, verb: str) -> Eval | None:
        """What the graph's current nodes of one verb hand on, as one; None when none is current."""
        return join([ev for ev in (self.eval(n) for n in self.by_verb(verb))
                     if ev.state == "current" and ev.output is not None])

    def output(self, verb: str) -> dict[str, Any]:
        """The same as a plain map (empty when no node of the verb is current)."""
        up = self.of(verb)
        return (up.output if up is not None else None) or {}

    def judging(self, task: str) -> list[str]:
        """The judge nodes of one task kind."""
        return [n for n in self.by_verb("judge") if self.graph[n].judge == task]

    def ready(self) -> list[Eval]:
        """The nodes that can move now: not current, with everything they need current."""
        return [ev for ev in self.evaluate() if ev.state != "current"
                and not (ev.state == "blocked" and ev.reason and ev.reason.startswith("waiting on"))]

    def done(self) -> bool:
        return all(ev.state == "current" for ev in self.evaluate())

    def materialize(self) -> list[str]:
        """Write artifacts for current judged and asked nodes that have none on disk."""
        written = []
        for ev in self.evaluate():
            impl = self.impls[ev.name]
            if ev.state == "current" and impl.how != "computed" and not ev.extra.get("pinned") \
                    and not self.run.paths.artifact(ev.name, ev.digest).is_file():
                impl.write_artifact(self, ev)
                written.append(ev.name)
        return written

    def node_report(self) -> list[dict[str, Any]]:
        rows = []
        for ev in self.evaluate():
            impl = self.impls[ev.name]
            st = ev.status()
            latest = impl.latest_config_digest(self)
            if latest is not None:
                st.config_changed = latest != impl.current_config_digest(self)
            row = st.model_dump()
            row["needs"] = self.needs(ev.name) if ev.name != SEAL else []
            rows.append(row)
        return rows
