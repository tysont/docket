"""Response contracts (TD §6.7, TP §5.6): each variation takes its safe reading and records
exactly one unexpected shape per distinct shape; unaffected units are unchanged."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from docket.config.source_models import ContractKind, Source
from docket.shapes.unexpected import collecting
from docket.sources.base import Cursor
from docket.sources.collect import aggregate
from docket.sources.contracts import ContractChecker
from docket.sources.declarative import DeclarativeSource, Limits
from docket.transports.base import Response, Secrets

pytestmark = pytest.mark.smoke

CONTRACT = {
    "message": ContractKind.model_validate({
        "required": {"id": "str", "ts": "timestamp"},
        "optional": {"text": "str", "sender.display": "str", "sender.name": "str"},
        "enums": {"kind": ["note", "alert"]},
        "variations": {
            "sender.display": {"missing": {"reading": "use sender.name", "use": "sender.name", "record": False}},
            "kind": {"unknown": {"reading": "treat as a note", "value": "note"}},
        },
        "tombstone": "$.deleted",
    }),
}
GOOD = {"id": "m1", "ts": "2026-10-15T08:00:00Z", "text": "hello", "kind": "note",
        "sender": {"name": "users/1", "display": "Pat"}}


def check(raw):
    with collecting() as col:
        res = ContractChecker("feed", CONTRACT, extend_base="preset demo: contract").check("message", raw)
        return res, col.drain()


def test_known_shape_is_read_with_no_record():
    res, shapes = check(GOOD)
    assert res.verdict == "ok" and res.view == GOOD and shapes == []


def test_missing_optional_is_not_drift_and_takes_the_declared_reading():
    raw = copy.deepcopy(GOOD)
    del raw["sender"]["display"]
    res, shapes = check(raw)
    assert res.verdict == "ok" and res.view["sender"]["display"] == "users/1" and shapes == []
    del raw["text"]
    res, shapes = check(raw)
    assert res.verdict == "ok" and shapes == []


def test_unknown_enum_takes_the_reading_and_records_once():
    raw = {**GOOD, "kind": "pinned"}
    res, shapes = check(raw)
    assert res.verdict == "ok" and res.view["kind"] == "note" and raw["kind"] == "pinned"
    assert [(s.area, s.detail, s.reading) for s in shapes] == [("source.contract", "feed.message.kind=pinned",
                                                                 "treat as a note")]
    assert shapes[0].extend == "preset demo: contract.message.enums.kind"


def test_missing_required_fails_the_record_only():
    raw = {k: v for k, v in GOOD.items() if k != "ts"}
    res, shapes = check(raw)
    assert res.verdict == "record_failed" and res.view is None
    assert [s.detail for s in shapes] == ["feed.message.ts missing"]


def test_type_change_on_required_fails_the_unit_with_the_sample():
    res, shapes = check({**GOOD, "id": 17})
    assert res.verdict == "type_changed" and res.detail == "feed.message.id type str->int"
    assert '"id": 17' in res.sample
    assert [s.detail for s in shapes] == ["feed.message.id type str->int"]


def test_optional_type_change_reads_the_field_as_absent():
    res, shapes = check({**GOOD, "text": ["a", "list"]})
    assert res.verdict == "ok" and res.view["text"] is None
    assert [s.detail for s in shapes] == ["feed.message.text type str->list"]


def test_tombstone_and_non_object():
    res, shapes = check({"id": "m1", "deleted": True})
    assert res.verdict == "tombstone" and shapes == []
    res, shapes = check("not an object")
    assert res.verdict == "record_failed" and [s.detail for s in shapes] == ["feed.message entry is not an object"]


def test_unknown_fields_are_ignored_or_recorded():
    ck = ContractKind.model_validate({"required": {"id": "str"}, "unknown_fields": "record"})
    with collecting() as col:
        ContractChecker("feed", {"m": ck}, extend_base="x").check("m", {"id": "a", "extra": 1})
        assert [s.detail for s in col.drain()] == ["feed.m field extra not in contract"]


def test_credentials_are_masked_in_samples():
    secrets = Secrets()
    secrets.register("s3cr3t-value-1234")
    with collecting() as col:
        ContractChecker("feed", CONTRACT, extend_base="x", secrets=secrets).check(
            "message", {**GOOD, "id": 5, "note": "s3cr3t-value-1234"})
        sample = col.drain()[0].sample
    assert "s3cr3t" not in sample and "[redacted]" in sample


# --- through a source: one variant per unit, other units unchanged ------------------------

SPACES = {"u-ok": [GOOD, {**GOOD, "id": "m2"}],
          "u-enum": [{**GOOD, "id": "e1", "kind": "pinned"}, {**GOOD, "id": "e2", "kind": "pinned"}],
          "u-type": [{**GOOD, "id": "t1"}, {**GOOD, "id": 99}],
          "u-missing": [{**GOOD, "id": "r1"}, {"id": "r2", "text": "no ts"}],
          "u-empty": [],
          "u-tomb": [{**GOOD, "id": "d1"}, {"id": "d2", "deleted": True}]}
UNITS = [{"id": k, "name": k.upper()} for k in SPACES] + [{"id": "u-nameless"}]
SOURCE = Source.model_validate({
    "transport": "t", "cursor": {"model": "units"},
    "request": {"discover": {"op": "discover"}, "read": {"op": "read", "args": {"unit": "{unit.id}"}}},
    "discover": {"entries": "$.units", "id": "$.id", "name": "$.name"},
    "paging": {"kind": "message", "entries": "$.items", "next": "$.next"},
    "contract": {"message": CONTRACT["message"].model_dump()},
})


class Scripted:
    kind = "scripted"
    id = "t"
    info: dict = {}

    def __init__(self, units, spaces):
        self.units, self.spaces = units, spaces

    async def open(self):
        return None

    async def request(self, req):
        p = req.payload
        if p["op"] == "discover":
            return Response(body=json.dumps({"units": self.units}).encode())
        return Response(body=json.dumps({"items": self.spaces.get(p["args"]["unit"], []), "next": None}).encode())

    async def close(self):
        return None


def read_all(units, spaces):
    async def go():
        src = DeclarativeSource("feed", SOURCE, Scripted(units, spaces), secrets=Secrets(),
                                limits=Limits(asyncio.Semaphore(4), asyncio.Semaphore(4)), env={})
        out = {}
        for u in await src.discover():
            r = src.read(None, u, Cursor("units"))
            pages = [p async for p in r]
            out[u.id] = (r.outcome, [e.view for p in pages for e in p.entries if e.view is not None], u)
        return out
    with collecting() as col:
        res = asyncio.run(go())
        return res, col.drain()


def test_variants_through_a_source_isolate_per_unit():
    res, shapes = read_all(UNITS, SPACES)
    clean, clean_shapes = read_all([{"id": "u-ok", "name": "U-OK"}], {"u-ok": SPACES["u-ok"]})
    assert res["u-ok"][1] == clean["u-ok"][1] and clean_shapes == []  # the unaffected unit is unchanged
    assert res["u-enum"][0].status == "read" and [v["kind"] for v in res["u-enum"][1]] == ["note", "note"]
    assert res["u-type"][0].status == "failed"
    assert res["u-type"][0].cause["code"] == "source.contract.type_changed"
    assert res["u-missing"][0].status == "read" and res["u-missing"][0].failed_records == 1
    assert [v["id"] for v in res["u-missing"][1]] == ["r1"]
    assert res["u-empty"][0].status == "empty"
    assert res["u-tomb"][0].tombstones == 1 and [v["id"] for v in res["u-tomb"][1]] == ["d1"]
    assert res["u-nameless"][2].name == "u-nameless"
    details = sorted(s.detail for s in shapes)
    assert details == sorted(["feed.message.kind=pinned", "feed.message.id type str->int",
                              "feed.message.ts missing", "feed unit without name"])  # one per distinct shape
    assert aggregate([]).status == "empty"


def test_unit_without_id_is_skipped_and_recorded():
    res, shapes = read_all([{"name": "no id"}, {"id": "u-ok", "name": "ok"}], {"u-ok": SPACES["u-ok"]})
    assert list(res) == ["u-ok"] and res["u-ok"][0].status == "read"
    assert [s.detail for s in shapes] == ["feed unit without id"]
