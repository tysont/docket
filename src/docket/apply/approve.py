"""Approval (TD §13.1, FR-APP-01) and its freshness check (TD §13.2).

``docket ask answer approve yes --digest <plan_digest>`` binds the approval to
that digest. The approval record is the ``approve`` answer::

    {"option": "yes", "action": "approve", "digest": "<plan digest>",
     "destinations": {"<dest_id>": "<snapshot digest>"}, "overrides": {...}, "input": "yes"}

``destinations`` are the snapshot digests that were verified fresh at the
moment of approval. Any change to an input digest makes the plan stale (TD
§4.4), and an approval whose digest differs from the current plan's binds
nothing. The wall-clock times of build and approval go to the ``approvals``
ledger, not the run directory, so artifacts stay deterministic; the journal's
``approval_delay_note`` reads them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from docket.apply import freshness
from docket.cli.errors import InputError, IntegrityError
from docket.store.paths import read_json


def plan_paths(rc: Any, plan_digest: str) -> tuple[dict[str, Any], Any]:
    pdir = rc.run.paths.plan(plan_digest)
    p = pdir / "plan.json"
    return (read_json(p) if p.is_file() else {}), pdir


def bind(rc: Any, q: dict[str, Any], option_id: str, given: str | None, answer: dict[str, Any], *,
         dry_run: bool = False) -> dict[str, Any]:
    """Validate ``--digest`` and freshness; return the approval record to store as the answer."""
    if not given:
        raise InputError("input.missing_digest", "Approving a plan requires --digest with the plan digest.",
                         hint=" ".join(rc.argv("ask", "answer", "approve", option_id, "--digest", q["digest"])))
    if given != q["digest"]:
        raise IntegrityError("approve.digest_mismatch",
                             "The digest does not match the current plan; the plan changed since it was shown.",
                             hint=" ".join(rc.argv("next")),
                             details={"expected": q["digest"], "received": given, "diff_path": q.get("diff_path")},
                             next_command=rc.argv("next"))
    out = {**answer, "digest": given}
    if option_id != "yes":
        return out
    if rc.run.header.applied_plan:
        # past the apply boundary nothing is re-checked: a repeat of the approval is a no-op, a change is refused
        prior = rc.run.answers().get("approve") or {}
        if prior.get("digest") == given:
            out["destinations"] = prior.get("destinations")
        return out
    doc, pdir = plan_paths(rc, given)
    changes = freshness.check(rc, doc, pdir, mark=not dry_run)
    if changes:
        raise freshness.refusal(rc, changes, given)
    out["destinations"] = dict((doc.get("inputs") or {}).get("destinations") or {})
    return out


def check_unreachable(rc: Any, *, dry_run: bool = False) -> None:
    """``approve`` was answered while the plan is stale: refuse with the destination change when that is why."""
    p = rc.run.paths.artifacts("plan") / "latest"
    if not p.is_file():
        return
    art_path = rc.run.paths.artifact("plan", p.read_text().strip())
    if not art_path.is_file():
        return
    pd = (read_json(art_path).get("data") or {}).get("plan_digest")
    if not pd:
        return
    doc, pdir = plan_paths(rc, pd)
    changes = freshness.check(rc, doc, pdir, mark=not dry_run)
    if changes:
        raise freshness.refusal(rc, changes, pd)


def record(rc: Any, plan_digest: str, record_: dict[str, Any]) -> None:
    """Append the approval's wall-clock facts to the ``approvals`` ledger (outside the run directory)."""
    from docket.close import ledgers
    _, pdir = plan_paths(rc, plan_digest)
    built = None
    try:
        built = datetime.fromtimestamp((pdir / "plan.json").stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        pass
    db = rc.run.store.db
    with db:
        ledgers.append(db, "approvals", rc.run.id, plan_digest,
                       {"plan_digest": plan_digest, "option": record_.get("option"),
                        "destinations": record_.get("destinations") or {},
                        "approved_at": datetime.now(timezone.utc).isoformat(), "built_at": built})
