"""Labels for task lines (TD §12.7): the words that stand for a task where others can read them.

Whether a title is safe to show is a judgment, so a label is written by the ``label``
task, with the workspace's guidance and examples, and not derived from the line. It is
written once per line: the label is remembered against the line's text and reused until
the text changes, so titles stay the same from run to run and a run pays only for lines
it has not seen.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any, Iterable

from docket.config.digest import digest

if TYPE_CHECKING:
    from docket.engine.context import RunContext

SOURCE = "tasks"


def answer_task_id(question_id: str) -> str:
    """The id of a task an answer places before any line is written for it."""
    return "t_" + digest(["answer", question_id])[:16]


def candidate(task_id: str, text: str, destination: str, *, area: str | None = None) -> dict[str, Any]:
    """A task line as an item the ``label`` task can read."""
    title = text.strip().splitlines()[0] if text.strip() else ""
    return {"id": task_id, "source": SOURCE, "unit": destination, "title": title[:200], "text": text,
            "fields": {"area": area} if area else {}}


def known(db: sqlite3.Connection, texts: dict[str, str]) -> dict[str, str]:
    """Remembered labels for these tasks (id -> line text), where the line is as it was."""
    out = {}
    for tid, text in texts.items():
        row = db.execute("SELECT text_digest, label FROM task_labels WHERE task_id=?", (tid,)).fetchone()
        if row is not None and row[0] == digest(text):
            out[tid] = row[1]
    return out


def pending(db: sqlite3.Connection, tasks: Iterable[tuple[str, str]], destination: str,
            areas: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Candidates for the tasks that have no remembered label for their present text."""
    tasks = list(tasks)
    have = known(db, dict(tasks))
    return [candidate(tid, text, destination, area=(areas or {}).get(tid)) for tid, text in tasks if tid not in have]


def remember(db: sqlite3.Connection, labels: dict[str, str], texts: dict[str, str], *, run_id: str) -> None:
    """Keep a run's labels (the caller holds the transaction)."""
    for tid, label in labels.items():
        if tid in texts and label:
            db.execute("INSERT OR REPLACE INTO task_labels (task_id, text_digest, label, run_id) VALUES (?,?,?,?)",
                       (tid, digest(texts[tid]), label, run_id))


def task_texts(rc: "RunContext", decide: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], str]:
    """Every task planning may place: id -> the words of its line, id -> area, and the
    destination they live in. Open lines of the task store, and this run's answers that
    add a task or place a block."""
    from docket.decide.operations import scheduling_inputs
    from docket.plan.build import planning_active, store_destination
    from docket.plan.taskstore import parse_line, read_store
    if not planning_active(rc):
        return {}, {}, ""
    did = store_destination(rc) or ""
    texts: dict[str, str] = {}
    areas: dict[str, str] = {}
    if did:
        rows, _ = read_store(rc, did, sections=rc.ws.config.planning.tasks.sections)
        for r in rows:
            texts[r.id] = r.title
            if r.raw.get("area"):
                areas[r.id] = r.raw["area"]
    for row in scheduling_inputs(rc, decide):
        tid, title = answer_task_id(row["question_id"]), str(row["title"] or "")
        op = row.get("op")
        if op is not None:
            parsed = parse_line(rc, op["destination"], op["line"])
            if parsed is not None:  # the id and words the task will have once its line is written
                tid, title = parsed.id, parsed.title
        texts.setdefault(tid, title)
    return texts, areas, did


def candidates(rc: "RunContext", decide: dict[str, Any]) -> list[dict[str, Any]]:
    texts, areas, did = task_texts(rc, decide)
    return pending(rc.run.store.db, sorted(texts.items()), did or SOURCE, areas)


def for_run(rc: "RunContext", decide: dict[str, Any]) -> dict[str, str]:
    """The labels planning uses: those this run's ``label`` stage wrote, over the remembered ones."""
    texts, _, _ = task_texts(rc, decide)
    out = known(rc.run.store.db, texts)
    out.update({k: v for k, v in (decide.get("labels") or {}).items() if v})
    return out
