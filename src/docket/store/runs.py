"""The store facade and run directories (TD §4.1)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from docket.cli.errors import StateError
from docket.config.digest import pretty_json
from docket.store.blobs import BlobStore
from docket.store.db import open_db
from docket.store.paths import read_json, write_atomic

RUN_SCHEMA = 1


class Clock(BaseModel):
    """The run's pinned clock (TD §4.6)."""
    date: str
    timezone: str
    instant: str


class RunHeader(BaseModel):
    """``run.json``: the run header."""
    schema_version: int = RUN_SCHEMA
    run_id: str
    workflow: str
    clock: Clock
    sandbox: bool = False
    status: str = "open"  # open | sealed | abandoned
    resumed: list[str] = []
    applied_plan: str | None = None
    pinned: dict[str, str] = {}     # stage -> digest the applied plan was built from (TD §5.4)
    drift_ack: str | None = None    # digest of the upstream drift the operator chose to finish on


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._db: sqlite3.Connection | None = None
        self.blobs = BlobStore(root / "blobs")

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = open_db(self.root / "docket.db")
        return self._db

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def run_row(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def latest_open(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM runs WHERE status='open' ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()

    def list_runs(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC").fetchall()


def wall_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunPaths:
    dir: Path

    @property
    def header(self) -> Path:
        return self.dir / "run.json"

    def artifact(self, stage: str, digest: str) -> Path:
        return self.dir / "artifacts" / stage / f"{digest}.json"

    def artifacts(self, stage: str) -> Path:
        return self.dir / "artifacts" / stage

    def records(self, generation: int, source: str) -> Path:
        return self.dir / f"gen-{generation}" / "records" / f"{source}.jsonl"

    def task(self, task_id: str) -> Path:
        return self.dir / "tasks" / task_id

    def plan(self, digest: str) -> Path:
        return self.dir / "plan" / digest

    @property
    def questions(self) -> Path:
        return self.dir / "questions.json"

    @property
    def answers(self) -> Path:
        return self.dir / "answers.json"

    @property
    def receipts(self) -> Path:
        return self.dir / "receipts.jsonl"

    @property
    def journal(self) -> Path:
        return self.dir / "journal.md"

    @property
    def unexpected(self) -> Path:
        return self.dir / "unexpected.jsonl"

    @property
    def sandbox(self) -> Path:
        return self.dir / "sandbox"


class Run:
    def __init__(self, store: Store, header: RunHeader) -> None:
        self.store = store
        self.header = header
        self.paths = RunPaths(store.runs_dir / header.run_id)

    @property
    def id(self) -> str:
        return self.header.run_id

    @property
    def dir(self) -> Path:
        return self.paths.dir

    @property
    def sealed(self) -> bool:
        return self.header.status == "sealed"

    def save_header(self) -> None:
        write_atomic(self.paths.header, pretty_json(self.header.model_dump(mode="json")))

    def set_status(self, status: str) -> None:
        self.header.status = status
        self.save_header()
        self.store.db.execute("UPDATE runs SET status=?, updated_at=? WHERE run_id=?", (status, wall_stamp(), self.id))

    def answers(self) -> dict[str, Any]:
        p = self.paths.answers
        return read_json(p) if p.is_file() else {}

    def write_answers(self, answers: dict[str, Any]) -> None:
        write_atomic(self.paths.answers, pretty_json(answers))

    def append_jsonl(self, path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def load_run(store: Store, run_id: str) -> Run:
    row = store.run_row(run_id)
    hp = store.runs_dir / run_id / "run.json"
    if row is None or not hp.is_file():
        known = [r["run_id"] for r in store.list_runs()][:20]
        raise StateError("run.not_found", f"No run {run_id!r} in this workspace.", hint="docket run list",
                         details={"known": known})
    return Run(store, RunHeader.model_validate(read_json(hp)))


def resolve_run(store: Store, run_id: str | None, *, start_hint: str) -> Run:
    if run_id:
        return load_run(store, run_id)
    row = store.latest_open()
    if row is None:
        raise StateError("run.none_open", "No unsealed run exists in this workspace.", hint=start_hint)
    return load_run(store, row["run_id"])


def new_run_id(store: Store, date: str, workflow: str) -> str:
    base = f"{date}-{workflow}"
    rid, n = base, 1
    while store.run_row(rid) is not None or (store.runs_dir / rid).exists():
        n += 1
        rid = f"{base}-{n}"
    return rid


def create_run(store: Store, *, run_id: str, workflow: str, clock: Clock, sandbox: bool) -> Run:
    header = RunHeader(run_id=run_id, workflow=workflow, clock=clock, sandbox=sandbox)
    run = Run(store, header)
    run.dir.mkdir(parents=True, exist_ok=False)
    run.save_header()
    stamp = wall_stamp()
    store.db.execute(
        "INSERT INTO runs (run_id, workflow, date, status, generation, sandbox, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, workflow, clock.date, "open", 0, int(sandbox), stamp, stamp),
    )
    return run


def find_existing(store: Store, workflow: str, date: str) -> sqlite3.Row | None:
    """The newest open or sealed run for this workflow and date, for an idempotent ``run start``."""
    return store.db.execute(
        "SELECT * FROM runs WHERE workflow=? AND date=? AND status IN ('open','sealed') "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (workflow, date)).fetchone()

