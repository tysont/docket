"""State-based freshness (TD §13.2, FR-APP-02).

At plan build, ``attach`` snapshots every destination the plan touches and
records each snapshot digest in the plan as ``inputs.destinations``
(``{dest_id: digest}``); the snapshot data goes beside the plan as
``snapshots.json`` so a refusal can say what changed. At approval, ``check``
snapshots the same destinations again. Equal digests: the plan is fresh, however
long ago it was built (there is no clock check). Any difference refuses the
approval with ``apply.stale_inputs``, a per-destination change summary and
``docket plan build`` as the next command.

A refusal also records the observed state as the plan's rebuild token
(``freshness.json`` in the run directory). The plan stage folds the token into
its digest, so the plan is stale until it is rebuilt against the state that
was observed, and the approval goes with it.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from docket.cli.errors import IntegrityError
from docket.config.digest import digest, pretty_json
from docket.store.paths import read_json, write_atomic

SNAPSHOTS = "snapshots.json"
TOKEN_FILE = "freshness.json"


@dataclass
class Change:
    destination: str
    adapter: str
    recorded: str
    current: str
    summary: str


def touched(rc: Any, plan: dict[str, Any]) -> list[str]:
    """Destinations the plan writes, plus the task store and calendar planning reads when planning is active."""
    dests = rc.ws.config.destinations
    out = {op["destination"] for op in plan.get("operations") or [] if op.get("destination") in dests}
    pl = rc.ws.config.planning
    if "calendar_digest" in (plan.get("inputs") or {}):
        if pl.calendar is not None and pl.calendar.destination in dests:
            out.add(pl.calendar.destination)
        from docket.plan.build import store_destination
        sd = store_destination(rc)
        if sd is not None and sd in dests:
            out.add(sd)
    return sorted(out)


def snapshot(rc: Any, dest_ids: list[str]) -> dict[str, dict[str, Any]]:
    """``{dest_id: {"adapter", "digest", "data"}}`` for each destination (twins in a sandbox run)."""
    from docket.destinations import for_run
    out: dict[str, dict[str, Any]] = {}
    for did in sorted(set(dest_ids)):
        d = for_run(rc, did)
        try:
            st = d.snapshot()
        finally:
            d.close()
        out[did] = {"adapter": st.adapter, "digest": st.digest, "data": st.data}
    return out


def attach(rc: Any, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Record ``inputs.destinations`` in the plan; returns the snapshots for ``save``."""
    snaps = snapshot(rc, touched(rc, plan))
    plan.setdefault("inputs", {})["destinations"] = {k: v["digest"] for k, v in sorted(snaps.items())}
    return snaps


def save(pdir: Path, snaps: dict[str, dict[str, Any]]) -> None:
    write_atomic(pdir / SNAPSHOTS, pretty_json(snaps))


# --- change summaries ------------------------------------------------------------------


def _lines(text: Any) -> Counter:
    return Counter((text or "").splitlines()) if isinstance(text, str) else Counter()


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _text_change(old: Any, new: Any, where: str = "") -> list[str]:
    a, b = _lines(old), _lines(new)
    added, removed = sum((b - a).values()), sum((a - b).values())
    parts = []
    if added:
        parts.append(_count(added, "line") + " added")
    if removed:
        parts.append(_count(removed, "line") + " removed")
    if not parts and old != new:
        parts.append("whitespace or order changed")
    return [f"{where}{', '.join(parts)}"] if parts else []


def summarize(adapter: str, old: dict[str, Any] | None, new: dict[str, Any]) -> str:
    if old is None:
        return "changed since build (no recorded snapshot to compare)"
    noun = "calendar event" if adapter.startswith("calendar_") else ("draft" if adapter == "message_mcp" else "object")
    parts: list[str] = []
    if "text" in new or "text" in old:
        parts += _text_change(old.get("text"), new.get("text"))
    if "files" in new or "files" in old:
        fo, fn = old.get("files") or {}, new.get("files") or {}
        for p in sorted(set(fo) | set(fn)):
            if fo.get(p) == fn.get(p):
                continue
            if fo.get(p) is None:
                parts.append(f"{p}: file added")
            elif fn.get(p) is None:
                parts.append(f"{p}: file removed")
            else:
                parts += _text_change(fo.get(p), fn.get(p), f"{p}: ")
    if "owned" in new or "unowned_ids" in new:
        ou, nu = set(old.get("unowned_ids") or []), set(new.get("unowned_ids") or [])
        oo, no = old.get("owned") or {}, new.get("owned") or {}
        add = len(nu - ou) + len(set(no) - set(oo))
        rem = len(ou - nu) + len(set(oo) - set(no))
        chg = sum(1 for k in set(oo) & set(no) if oo[k] != no[k])
        if add:
            parts.append(_count(add, noun) + " added")
        if rem:
            parts.append(_count(rem, noun) + " removed")
        if chg:
            parts.append(_count(chg, "owned " + noun) + " changed")
        if not (add or rem or chg) and old.get("busy") != new.get("busy"):
            parts.append(_count(len(new.get("busy") or []), "busy interval") + " now, times changed")
    if not parts:
        parts.append("state changed")
    return "; ".join(parts) + " since build"


def check(rc: Any, plan_doc: dict[str, Any], pdir: Path, *, mark: bool = True) -> list[Change]:
    """Changes in the destinations the plan recorded; empty when every snapshot digest is equal."""
    recorded: dict[str, str] = (plan_doc.get("inputs") or {}).get("destinations") or {}
    if not recorded:
        return []
    snaps_path = pdir / SNAPSHOTS
    old = read_json(snaps_path) if snaps_path.is_file() else {}
    present = [d for d in recorded if d in rc.ws.config.destinations]
    now = snapshot(rc, present)
    out = []
    for did in sorted(recorded):
        cur = now.get(did)
        if cur is None:
            out.append(Change(did, "", recorded[did], "", "the destination is no longer configured"))
            continue
        if cur["digest"] != recorded[did]:
            prev = (old.get(did) or {}).get("data")
            out.append(Change(did, cur["adapter"], recorded[did], cur["digest"],
                              summarize(cur["adapter"], prev, cur["data"])))
    if out and mark:
        mark_stale(rc, {k: v["digest"] for k, v in now.items()})
    return out


def refusal(rc: Any, changes: list[Change], plan_digest: str) -> IntegrityError:
    build = rc.argv("plan", "build")
    text = "; ".join(f"{c.destination}: {c.summary}" for c in changes)
    return IntegrityError("apply.stale_inputs",
                          f"The plan is stale: {text}. Rebuild it and show the new diff before approving.",
                          hint=" ".join(build), retryable=True, next_command=build,
                          details={"plan_digest": plan_digest,
                                   "destinations": {c.destination: asdict(c) for c in changes}})


# --- the rebuild token -------------------------------------------------------------------


def mark_stale(rc: Any, observed: dict[str, str]) -> None:
    write_atomic(rc.run.dir / TOKEN_FILE, pretty_json({"observed": dict(sorted(observed.items()))}))


def rebuild_token(rc: Any) -> str | None:
    """The digest of the destination state the last refusal observed, or None when nothing was refused."""
    p = rc.run.dir / TOKEN_FILE
    if not p.is_file():
        return None
    try:
        return digest(json.loads(p.read_text(encoding="utf-8")).get("observed"))
    except ValueError:
        return None
