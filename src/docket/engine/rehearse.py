"""``docket run rehearse`` (TD §14.3, FR-ROB-03).

Rehearsal drives a copy of a run to its end and reports what the live run
would do, writing nothing outside ``runs/<id>/rehearse-<n>/``:

1. The run directory, the blobs its records name and the workspace database
   are copied into ``rehearse-<n>/state/<workspace-slug>/``, a complete state
   root of its own. The copy's run is marked sandbox, so every destination is
   its sandbox twin (Markdown twins write under the copy; calendars are
   in-memory), close moves no durable cursor, and hooks run in their declared
   ``dry_run`` form or are skipped with a note. The copy's lease rows are
   dropped: the rehearsal owns its copy.
2. Every source is replaced by replay over the run's recorded records
   (``docket.sources.recorded``): a collect in the copy re-serves the recorded
   pages and contacts no system.
3. The engine loop runs in process against the copy, exactly as an agent
   would: ``run`` steps execute their command; ``dispatch`` steps reuse
   accepted results (the task store's carry-forward) and otherwise submit a
   synthetic conservative result (``docket.engine.synthetic``); ``ask`` steps
   take the recorded answer, else the recommended option, else the first
   option (``--answers recommended`` ignores recorded answers; ``file:<path>``
   answers the questions the run has not answered from an ``ask answer --file``
   document before falling back); approval binds the current digest;
   a paused workflow is resumed.

The report (``rehearse-<n>/report.json``) lists each stage's outcome, every
operation the live run would perform, every refusal with its code, every
unexpected shape and the elapsed time.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

REHEARSE_PREFIX = "rehearse-"
MAX_STEPS = 400
REPORT_SCHEMA = 1


@dataclass
class Log:
    calls: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)
    unexpected: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    answers: list[dict[str, Any]] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    stop: dict[str, Any] | None = None


def next_index(run_dir: Path) -> int:
    n = 1
    while (run_dir / f"{REHEARSE_PREFIX}{n}").exists():
        n += 1
    return n


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)  # blobs are immutable: a hard link costs nothing and the copy never writes it
    except OSError:
        shutil.copy2(src, dst)


def copy_state(live_root: Path, run_id: str, dest_root: Path, *, drop_answers: bool) -> None:
    """A self-contained state root holding the database, the run and the blobs its records name."""
    run_src = live_root / "runs" / run_id
    run_dst = dest_root / "runs" / run_id
    dest_root.mkdir(parents=True, exist_ok=True)
    src_db = sqlite3.connect(str(live_root / "docket.db"), timeout=10.0)
    dst_db = sqlite3.connect(str(dest_root / "docket.db"))
    try:
        src_db.backup(dst_db)
    finally:
        src_db.close()
    try:
        with dst_db:
            dst_db.execute("DELETE FROM leases")
            dst_db.execute("UPDATE runs SET sandbox=1 WHERE run_id=?", (run_id,))
    finally:
        dst_db.close()
    shutil.copytree(run_src, run_dst, ignore=lambda d, names: [
        n for n in names if Path(d) == run_src and (n.startswith(REHEARSE_PREFIX) or n == "session")])
    hdr_path = run_dst / "run.json"
    hdr = json.loads(hdr_path.read_text(encoding="utf-8"))
    hdr["sandbox"] = True
    hdr_path.write_text(json.dumps(hdr, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if drop_answers and (run_dst / "answers.json").is_file():
        (run_dst / "answers.json").unlink()
    for idx in run_dst.glob("gen-*/records/*.jsonl"):
        for ln in idx.read_text(encoding="utf-8").splitlines():
            try:
                ref = json.loads(ln).get("blob")
            except ValueError:
                continue
            if not ref:
                continue
            hexd = ref.split(":", 1)[1] if ":" in ref else ref
            src = live_root / "blobs" / hexd[:2] / hexd
            dst = dest_root / "blobs" / hexd[:2] / hexd
            if src.is_file() and not dst.exists():
                _link_or_copy(src, dst)


class Rehearsal:
    def __init__(self, *, ws: Any, live_root: Path, run_id: str, env: Mapping[str, str], answers: str = "recorded",
                 cwd: Path | None = None) -> None:
        from docket.store.paths import workspace_slug
        self.ws = ws
        self.live_root = live_root
        self.run_id = run_id
        self.mode, self.answers_file = self._mode(answers)
        self.live_run_dir = live_root / "runs" / run_id
        self.index = next_index(self.live_run_dir)
        self.dir = self.live_run_dir / f"{REHEARSE_PREFIX}{self.index}"
        self.state_base = self.dir / "state"
        self.root = self.state_base / workspace_slug(ws.root)
        self.env = {**dict(env), "DOCKET_STATE_ROOT": str(self.state_base)}
        self.env.pop("DOCKET_SESSION", None)
        self.cwd = cwd or ws.root
        if self.answers_file is not None and not self.answers_file.is_absolute():
            self.answers_file = self.cwd / self.answers_file
        self.log = Log()
        self.file_rows: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _mode(answers: str) -> tuple[str, Path | None]:
        from docket.cli.errors import InputError
        if answers in ("recorded", "recommended"):
            return answers, None
        if answers.startswith("file:") and answers[5:]:
            return "file", Path(answers[5:])
        raise InputError("input.bad_argument", f"--answers {answers!r}: use recorded, recommended or file:<path>.",
                         hint="docket help run rehearse")

    # --- running commands against the copy ------------------------------------------------
    def call(self, argv: list[str], *, stage: str | None = None, record_failure: bool = True) -> dict[str, Any]:
        from docket.cli import main
        if argv and argv[0] == "docket":
            argv = argv[1:]
        if "--json" not in argv:
            argv = [*argv, "--json"]
        out = io.StringIO()
        code = main(argv, stdout=out, stderr=io.StringIO(), env=self.env, cwd=str(self.cwd))
        try:
            env_out = json.loads(out.getvalue())
        except ValueError:
            env_out = {"ok": False, "error": {"code": "internal.bug", "kind": "internal",
                                              "message": "unparseable output", "hint": None}}
        self.log.calls.append({"argv": argv[:6], "stage": stage, "exit": code})
        for u in env_out.get("unexpected") or []:
            self.log.unexpected.setdefault((u["area"], u["detail"]), u)
        if code != 0 and record_failure:
            e = env_out.get("error") or {}
            self.log.refusals.append({"stage": stage, "command": " ".join(argv[:4]), "exit": code,
                                      "code": e.get("code"), "kind": e.get("kind"), "message": e.get("message"),
                                      "hint": e.get("hint")})
        env_out["_exit"] = code
        return env_out

    # --- the loop ----------------------------------------------------------------------------
    def drive(self) -> None:
        from docket.engine.synthetic import synthetic_result
        from docket.tasks.kinds import kinds_for
        kinds = kinds_for(self.ws.config)
        run_date = None
        for _ in range(MAX_STEPS):
            nxt = self.call(["next", "--workspace", str(self.ws.root), "--run", self.run_id], stage="next")
            if not nxt["ok"]:
                self.log.stop = {"kind": "error", "code": nxt["error"].get("code")}
                return
            step = nxt["data"]["step"]
            stage = step.get("stage")
            k = step["kind"]
            if k == "run":
                res = self.call(step["command"]["argv"], stage=stage)
                if not res["ok"]:
                    self.log.stop = {"kind": "refused", "stage": stage, "code": res["error"].get("code")}
                    return
            elif k == "dispatch":
                if run_date is None:
                    run_date = self._run_date()
                for t in step["tasks"]:
                    pack = json.loads(Path(t["pack_path"]).read_text(encoding="utf-8"))
                    result = synthetic_result(kinds[t["kind"]], pack, run_date)
                    syn = self.dir / "synthetic" / f"{t['task_id']}.json"
                    syn.parent.mkdir(parents=True, exist_ok=True)
                    syn.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
                    Path(t["result_path"]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(syn, t["result_path"])
                    res = self.call(t["submit"]["argv"], stage=stage)
                    self.log.tasks.append({"task_id": t["task_id"], "kind": t["kind"], "stage": stage,
                                           "items": len(pack.get("items") or []), "synthetic": True,
                                           "accepted": res["ok"],
                                           "result": syn.relative_to(self.dir).as_posix()})
                    if not res["ok"]:
                        self.log.stop = {"kind": "refused", "stage": stage, "code": res["error"].get("code")}
                        return
            elif k == "ask":
                for q in step["questions"]:
                    if not self.answer(q, stage, step["answer"]["argv"]):
                        self.log.stop = {"kind": "refused", "stage": stage, "question": q["id"]}
                        return
            elif k == "wait":
                if step["reason"] == "paused" and step.get("command"):
                    self.log.resumed.append(stage or "")
                    res = self.call(step["command"]["argv"], stage=stage)
                    if not res["ok"]:
                        self.log.stop = {"kind": "refused", "stage": stage, "code": res["error"].get("code")}
                        return
                    continue
                self.log.stop = {"kind": "wait", "stage": stage, "reason": step["reason"],
                                 "instructions": step.get("instructions")}
                return
            else:
                self.log.stop = {"kind": "done"}
                return
        self.log.stop = {"kind": "error", "code": "rehearse.max_steps"}

    def _run_date(self) -> str:
        hdr = json.loads((self.root / "runs" / self.run_id / "run.json").read_text(encoding="utf-8"))
        return hdr["clock"]["date"]

    def answer(self, q: dict[str, Any], stage: str | None, template: list[str]) -> bool:
        from docket.engine.steps import OPTION, QUESTION

        def argv(option: str) -> list[str]:  # the step's answer template, filled (AE-07)
            return [q["id"] if a == QUESTION else option if a == OPTION else a for a in template]

        opts = q["options"]
        if self.mode == "file" and q["id"] in self.file_rows:
            row = {"question": q["id"], **self.file_rows[q["id"]]}
            if q["id"] == "approve":
                row["digest"] = q["details"].get("digest")
            path = self.dir / "answers" / f"{q['id']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"answers": [row]}), encoding="utf-8")
            res = self.call(["ask", "answer", "--file", str(path), "--workspace", str(self.ws.root),
                             "--run", self.run_id], stage=stage, record_failure=False)
            if res["ok"]:
                self.log.answers.append({"question": q["id"], "kind": q["kind"], "option": row.get("option"),
                                         "source": "file", "stage": stage})
                return True
        rec = [o for o in opts if o.get("recommended")]
        order = [(o, "recommended") for o in rec] + [(o, "first" if i == 0 else "fallback")
                                                     for i, o in enumerate(opts) if o not in rec]
        failures = []
        for o, why in order:
            res = self.call(argv(o["id"]), stage=stage, record_failure=False)
            if res["ok"]:
                self.log.answers.append({"question": q["id"], "kind": q["kind"], "option": o["id"], "source": why,
                                         "stage": stage, "skipped": [f["option"] for f in failures]})
                return True
            e = res.get("error") or {}
            failures.append({"option": o["id"], **e})
            if e.get("kind") == "integrity":  # the live answer would be refused: report it even if another works
                self.log.refusals.append({"stage": stage, "command": f"ask answer {q['id']} {o['id']}", "exit": 4,
                                          "code": e.get("code"), "kind": e.get("kind"), "message": e.get("message"),
                                          "hint": e.get("hint"), "recovered_by_next_option": True})
        e = failures[0] if failures else {}
        self.log.refusals.append({"stage": stage, "command": f"ask answer {q['id']}", "exit": 4,
                                  "code": e.get("code"), "kind": e.get("kind"), "message": e.get("message"),
                                  "hint": e.get("hint")})
        return False

    # --- the report -----------------------------------------------------------------------------
    def report(self, elapsed: float, recorded_answers: int) -> dict[str, Any]:
        st = self.call(["run", "status", "--workspace", str(self.ws.root), "--run", self.run_id], stage="report",
                       record_failure=False)
        stages = st["data"]["stages"] if st.get("ok") else []
        db = sqlite3.connect(str(self.root / "docket.db"))
        db.row_factory = sqlite3.Row
        try:
            receipts = {r["op_id"]: dict(r) for r in db.execute("SELECT * FROM receipts WHERE run_id=?", (self.run_id,))}
        finally:
            db.close()
        run_dir = self.root / "runs" / self.run_id
        operations = []
        plan = _latest_artifact(run_dir, "plan")
        for op in (plan or {}).get("operations") or []:
            rec = receipts.get(op.get("op_id"), {})
            operations.append({"op_id": op.get("op_id"), "destination": op.get("destination"), "verb": op.get("verb"),
                               "what": _what(op), "outcome": rec.get("outcome"),
                               "verified": rec.get("verify_status") == "verified" if rec else None})
        for r in _receipt_rows(run_dir):
            if r.get("outcome") == "refused":
                err = r.get("error") or {}
                self.log.refusals.append({"stage": "apply", "command": f"apply {r.get('op_id')}", "exit": 4,
                                          "code": err.get("code"), "kind": err.get("kind"),
                                          "message": err.get("message"), "hint": err.get("hint")})
        per_stage = []
        for s in stages:
            name = s["stage"]
            per_stage.append({
                "stage": name, "state": s["state"], "reason": s.get("reason"),
                "commands": sum(1 for c in self.log.calls if c["stage"] == name),
                "synthetic_tasks": sum(1 for t in self.log.tasks if t["stage"] == name),
                "answers": sum(1 for a in self.log.answers if a["stage"] == name),
                "refusals": sum(1 for r in self.log.refusals if r["stage"] == name),
            })
        sources: dict[str, int] = {}
        for a in self.log.answers:
            sources[a["source"]] = sources.get(a["source"], 0) + 1
        if recorded_answers:
            sources["recorded"] = recorded_answers
        reached = (self.log.stop or {}).get("kind") == "done"
        journal = run_dir / "journal.md"
        in_run: dict[tuple[str, str], dict[str, Any]] = {}
        if (run_dir / "unexpected.jsonl").is_file():
            for ln in (run_dir / "unexpected.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    u = json.loads(ln)
                    in_run.setdefault((u["area"], u["detail"]), u)
                except (ValueError, KeyError):
                    continue
        return {
            "schema": REPORT_SCHEMA, "run": self.run_id, "rehearsal": self.index, "dir": str(self.dir),
            "answers_mode": self.mode, "ok": reached and not self.log.refusals, "reached": self.log.stop,
            "elapsed_s": round(elapsed, 3),
            "stages": per_stage,
            "operations": operations,
            "refusals": self.log.refusals,
            "unexpected": sorted(self.log.unexpected.values(), key=lambda u: (u["area"], u["detail"])),
            "unexpected_in_run": sorted(in_run.values(), key=lambda u: (u["area"], u["detail"])),
            "tasks": self.log.tasks,
            "answers": {"by_source": sources, "rows": self.log.answers},
            "resumed": self.log.resumed,
            "journal": str(journal) if journal.is_file() else None,
            "notes": ["every destination was its sandbox twin; hooks ran in dry_run form or were skipped",
                      "sources replayed the run's recorded records; no auth check and no live read ran"],
        }

    def run(self) -> dict[str, Any]:
        from docket.sources.recorded import RecordedReplay, replaying
        from docket.store.blobs import BlobStore
        t0 = time.perf_counter()
        if self.answers_file is not None:
            self.file_rows = _answer_rows(self.answers_file)
        self.dir.mkdir(parents=True)
        copy_state(self.live_root, self.run_id, self.root, drop_answers=self.mode == "recommended")
        ans_path = self.root / "runs" / self.run_id / "answers.json"
        recorded = len(json.loads(ans_path.read_text(encoding="utf-8"))) if ans_path.is_file() else 0
        rep = RecordedReplay.from_run(self.root / "runs" / self.run_id, BlobStore(self.root / "blobs"))
        with replaying(rep):
            self.drive()
        out = self.report(time.perf_counter() - t0, recorded)
        out["replay"] = {"served": rep.served, "local_fallback": rep.fell_through, "missing": rep.missing[:20]}
        (self.dir / "report.json").write_text(json.dumps(out, indent=2, sort_keys=True, default=str) + "\n",
                                              encoding="utf-8")
        out["report"] = str(self.dir / "report.json")
        return out


def _answer_rows(path: Path) -> dict[str, dict[str, Any]]:
    from docket.cli.errors import InputError
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InputError("input.bad_json", f"--answers file {path}: {e}", hint="docket help ask answer") from None
    if isinstance(data, dict) and isinstance(data.get("answers"), list):
        data = data["answers"]
    rows: dict[str, dict[str, Any]] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            rows[k] = v if isinstance(v, dict) else {"option": v}
    elif isinstance(data, list):
        for v in data:
            if isinstance(v, dict) and v.get("question"):
                rows[str(v["question"])] = {k: x for k, x in v.items() if k != "question"}
    return rows


def _latest_artifact(run_dir: Path, stage: str) -> dict[str, Any] | None:
    p = run_dir / "artifacts" / stage / "latest"
    if not p.is_file():
        return None
    art = run_dir / "artifacts" / stage / f"{p.read_text().strip()}.json"
    return json.loads(art.read_text(encoding="utf-8")).get("data") if art.is_file() else None


def _receipt_rows(run_dir: Path) -> list[dict[str, Any]]:
    p = run_dir / "receipts.jsonl"
    if not p.is_file():
        return []
    latest: dict[str, dict[str, Any]] = {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        latest[r.get("op_id")] = r
    return list(latest.values())


def _what(op: dict[str, Any]) -> str:
    if op.get("line"):
        return str(op["line"])
    if op.get("subject"):
        return f"draft: {op['subject']}"
    if op.get("start"):
        return f"{op.get('title', '')} {op.get('start')}-{op.get('end')}".strip()
    return str(op.get("key") or op.get("anchor") or "")


def latest_run(live_root: Path) -> str | None:
    """The most recent open run, else the most recent run, read without opening the store for writing."""
    db = live_root / "docket.db"
    if not db.is_file():
        return None
    conn = sqlite3.connect(str(db), timeout=10.0)
    try:
        row = conn.execute("SELECT run_id FROM runs WHERE status='open' ORDER BY created_at DESC, rowid DESC "
                           "LIMIT 1").fetchone() or \
            conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row[0] if row else None
