#!/usr/bin/env python3
"""A stub calendar bridge speaking the Docket exec protocol (TD §17.2), for tests only.

State lives in a JSON file (``--state``). ``hello`` picks the target from its
``mode``: ``live`` uses the state file, ``sandbox`` a copy of it, ``scratch`` a
fresh fixture with one plain meeting and one marker-carrying event that has an
attendee (so it is never owned). Flags: ``--no-sandbox``, ``--no-scratch``,
``--caps a,b`` (declared capabilities), ``--fail VERB`` (that verb returns an
error), ``--calls FILE`` (append each request op to FILE).
All data is invented.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ALL = ["list", "busy", "create", "move", "update", "delete", "rekey", "mirror", "status"]
SCRATCH = {"n": 0, "events": [
    {"id": "ext-1", "title": "Team standup", "start": "2030-01-07T09:00:00Z", "end": "2030-01-07T09:30:00Z",
     "notes": "", "attendees": ["a@team.example"]},
    {"id": "ext-2", "title": "Lookalike", "start": "2030-01-07T11:00:00Z", "end": "2030-01-07T12:00:00Z",
     "notes": "[docket:dk_aaaaaaaaaaaa]", "attendees": ["b@team.example"]},
]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--no-sandbox", action="store_true")
    ap.add_argument("--no-scratch", action="store_true")
    ap.add_argument("--caps", default=",".join(ALL))
    ap.add_argument("--fail", action="append", default=[])
    ap.add_argument("--calls")
    a = ap.parse_args()
    caps = [c for c in a.caps.split(",") if c]
    path = a.state

    def load() -> dict:
        if not os.path.exists(path):
            return {"n": 0, "events": []}
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def save(st: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)

    def find(st: dict, eid: str) -> dict | None:
        return next((e for e in st["events"] if e["id"] == eid), None)

    for raw in sys.stdin:
        if not raw.strip():
            continue
        req = json.loads(raw)
        op, args, rid = req.get("op"), req.get("args") or {}, req.get("id")
        if a.calls:
            with open(a.calls, "a", encoding="utf-8") as fh:
                fh.write(op + "\n")

        def reply(result=None, error=None) -> None:
            body = {"v": 1, "id": rid, "ok": error is None}
            body["result" if error is None else "error"] = result if error is None else error
            sys.stdout.write(json.dumps(body) + "\n")
            sys.stdout.flush()

        if op == "hello":
            mode = args.get("mode", "live")
            if mode == "sandbox" and not a.no_sandbox:
                live = path + ".scratch.json" if args.get("base") == "scratch" else path
                path = live + ".sandbox.json"
                if os.path.exists(live):
                    shutil.copyfile(live, path)
                elif os.path.exists(path):
                    os.remove(path)
            elif mode == "scratch" and not a.no_scratch:
                path = path + ".scratch.json"
                save(json.loads(json.dumps(SCRATCH)))
            reply({"name": "calbridge-stub", "protocol": 1, "capabilities": caps,
                   "sandbox": not a.no_sandbox, "scratch": not a.no_scratch})
            continue
        if op in a.fail:
            reply(error={"code": "permission", "message": f"stub refuses {op}", "retryable": True})
            continue
        if op not in caps:
            reply(error={"code": "unsupported", "message": f"no verb {op}", "retryable": False})
            continue
        st = load()
        if op == "list":
            lo, hi = args.get("from"), args.get("to")
            rows = [e for e in st["events"] if (not lo or e["end"] > lo) and (not hi or e["start"] < hi)]
            reply({"events": rows})
        elif op == "busy":
            reply({"busy": [[e["start"], e["end"]] for e in st["events"]]})
        elif op == "create":
            st["n"] += 1
            eid = f"ev-{st['n']}"
            st["events"].append({"id": eid, "title": args["title"], "start": args["start"], "end": args["end"],
                                 "notes": args.get("notes", ""), "calendar": args.get("calendar")})
            save(st)
            reply({"external_id": eid})
        elif op in ("move", "update", "delete", "rekey"):
            ev = find(st, args.get("id", ""))
            if ev is None:
                reply(error={"code": "not_found", "message": f"no event {args.get('id')}", "retryable": False})
                continue
            if op == "move":
                ev["start"], ev["end"] = args["start"], args["end"]
            elif op == "update":
                ev["title"], ev["notes"] = args["title"], args["notes"]
            elif op == "rekey":
                ev["notes"] = args["notes"]
            else:
                st["events"].remove(ev)
            save(st)
            reply({"ok": True})
        elif op == "mirror":
            reply({"planned": []})
        elif op == "status":
            reply({"access": "granted"})
        else:
            reply(error={"code": "unknown_op", "message": f"unknown op {op}", "retryable": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
