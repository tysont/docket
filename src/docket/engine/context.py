"""``RunContext``: one run seen through the current workspace configuration.

This is the ``run`` argument of the stage protocol (TD §5.1). It evaluates the
stage graph once per command and memoizes the result; a mutating command calls
``invalidate()`` after it writes.
"""
from __future__ import annotations

from typing import Any, Mapping

from docket.cli.errors import InputError
from docket.config import Workspace
from docket.config.digest import digest
from docket.config.loader import closest
from docket.config.models import Workflow
from docket.engine.stages import STAGE_IMPLS, StageEval
from docket.store.runs import Run

APPLY_INDEX = list(STAGE_IMPLS).index("apply")


class RunContext:
    def __init__(self, ws: Workspace, run: Run, env: Mapping[str, str]) -> None:
        self.ws = ws
        self.run = run
        self.env = env
        wf = ws.config.workflows.get(run.header.workflow)
        if wf is None:
            raise InputError("input.unknown_workflow",
                             f"Run {run.id} uses workflow {run.header.workflow!r}, which the workspace no longer defines.",
                             hint="Restore the workflow in docket.yaml or abandon the run",
                             details={"suggestion": closest(run.header.workflow, list(ws.config.workflows))})
        self.workflow: Workflow = wf
        self._evals: list[StageEval] | None = None

    # --- commands and config -------------------------------------------------
    def argv(self, *words: str) -> list[str]:
        return ["docket", *words, "--workspace", str(self.ws.root), "--run", self.run.id, "--json"]

    def partial_config(self, sections: tuple[str, ...]) -> str:
        wf_raw = self.ws.resolved["workflows"][self.run.header.workflow]
        parts: dict[str, Any] = {}
        for s in sections:
            if s.startswith("workflow."):
                parts[s] = digest(wf_raw.get(s.split(".", 1)[1]))
            else:
                parts[s] = self.ws.sections.get(s, digest(None))
        return digest(parts)

    def clock_view(self, *, with_time: bool) -> dict[str, Any]:
        c = self.run.header.clock
        out = {"date": c.date, "timezone": c.timezone}
        if with_time:
            out["instant"] = c.instant
        return out

    def workflow_name(self) -> str:
        return self.run.header.workflow

    def source_ids(self) -> list[str]:
        if self.workflow.sources is not None:
            return list(self.workflow.sources)
        return sorted(self.ws.config.sources)

    # --- evaluation ----------------------------------------------------------
    def evaluate(self) -> list[StageEval]:
        if self._evals is not None:
            return self._evals
        out: list[StageEval] = []
        up: StageEval | None = None
        blocked_by: str | None = None
        hdr = self.run.header
        for name in self.workflow.stages:
            impl = STAGE_IMPLS[name]
            if blocked_by is not None:
                out.append(StageEval(name, impl.kind, "blocked", reason=f"waiting on {blocked_by}"))
                continue
            idx = list(STAGE_IMPLS).index(name)
            ev = impl.evaluate(self, up)
            if hdr.applied_plan and idx < APPLY_INDEX:
                # TD §5.4: past the apply boundary an upstream stage that no longer matches is read as pinned.
                from docket.apply.boundary import pinned_eval
                pinned = None if ev.state == "current" and ev.digest == hdr.pinned.get(name) else \
                    pinned_eval(self, impl)
                if pinned is not None:
                    pinned.extra["fresh_state"] = ev.state
                    out.append(pinned)
                    up = pinned
                    continue
            if ev.state != "current":
                if hdr.applied_plan and idx < APPLY_INDEX:
                    ev.extra["underlying"] = ev.state
                    ev.state, ev.reason = "blocked", "post_apply_change"
                elif self.workflow.pause_before == name and name not in hdr.resumed:
                    ev.extra["underlying"] = ev.state
                    ev.state, ev.reason = "waiting", "paused"
                blocked_by = name
            out.append(ev)
            up = ev
        self._evals = out
        return out

    def invalidate(self) -> None:
        self._evals = None

    def eval(self, name: str) -> StageEval:
        for ev in self.evaluate():
            if ev.name == name:
                return ev
        raise KeyError(name)

    def eval_or_none(self, name: str) -> StageEval | None:
        try:
            return self.eval(name)
        except KeyError:
            return None

    def upstream(self, name: str) -> StageEval | None:
        evs = self.evaluate()
        for i, ev in enumerate(evs):
            if ev.name == name:
                return evs[i - 1] if i else None
        return None

    def first_pending(self) -> StageEval | None:
        for ev in self.evaluate():
            if ev.state != "current":
                return ev
        return None

    def materialize(self) -> list[str]:
        """Write artifacts for current delegated and interactive stages that have none on disk."""
        written = []
        for ev in self.evaluate():
            if ev.state != "current":
                break
            impl = STAGE_IMPLS[ev.name]
            if impl.kind != "computational" and not self.run.paths.artifact(ev.name, ev.digest).is_file():
                impl.write_artifact(self, ev)
                written.append(ev.name)
        return written

    def stage_report(self) -> list[dict[str, Any]]:
        rows = []
        for ev in self.evaluate():
            impl = STAGE_IMPLS[ev.name]
            st = ev.status()
            latest = impl.latest_config_digest(self)
            if latest is not None:
                st.config_changed = latest != self.partial_config(impl.config_sections)
            rows.append(st.model_dump())
        return rows
