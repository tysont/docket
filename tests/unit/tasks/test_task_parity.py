"""Prompt and validator parity (Test Plan §5.3), for every kind including each persona's own kind.

- every field and enumerated value named in the prompt's output specification is accepted;
- every field the validator accepts is named in the prompt;
- the example embedded in the prompt validates;
- a result generated from the JSON Schema either validates or is refused with an input or
  integrity code, never an internal error.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from docket.cli.errors import DocketError
from docket.shapes.unexpected import collecting
from docket.tasks import submit
from docket.tasks.prompts import render_prompt
from tests.task_helpers import PERSONA_NAMES, item, packs_for, persona_config, persona_kinds, raw_from_lines

pytestmark = pytest.mark.smoke

CASES = [(p, k) for p in PERSONA_NAMES for k in persona_kinds(p)]
CUSTOM = {"reference-like": "tone", "support-lead": "sla_check"}
FIELD_LINE = re.compile(r"^(?P<indent> *)- `(?P<name>[^`]+)` \(")
VALUE_LINE = re.compile(r"^(?P<indent> *)- `(?P<value>[^`]+)`: ")


def test_every_persona_declares_a_workspace_kind():
    for p, name in CUSTOM.items():
        assert name in persona_kinds(p) and not persona_kinds(p)[name].core


def _items(kind) -> list[dict[str, Any]]:
    rows = [item(i, source="chat", keep=True, score=60,
                 assessment={"kind": "request", "owner": "me", "ask": "Send the report",
                             "evidence": [{"line": "L1", "quote": "Please send"}]}) for i in range(3)]
    return rows


def _pack(persona: str, kind_name: str):
    cfg = persona_config(persona)
    kind = persona_kinds(persona)[kind_name]
    pack = packs_for(kind, _items(kind), cfg)[0]
    prompt = render_prompt(kind, pack, config=cfg, result_path="/tmp/result.json",
                           submit_shell=f"docket task submit {pack['task_id']} /tmp/result.json")
    return cfg, kind, pack, prompt


def spec_section(prompt: str) -> str:
    return prompt.split("## Output specification", 1)[1].split("## Data", 1)[0]


def named(prompt: str) -> tuple[dict[str, set[str]], set[str]]:
    """(field path -> enum values named, every field path named) from the output specification."""
    spec = spec_section(prompt).split("Rules:", 1)[0].split("Other fields are ignored", 1)[0]
    values: dict[str, set[str]] = {}
    stack: list[tuple[int, str]] = []
    for ln in spec.splitlines():
        m = FIELD_LINE.match(ln)
        if m:
            depth = len(m["indent"])
            while stack and stack[-1][0] >= depth:
                stack.pop()
            path = ".".join([s[1] for s in stack] + [m["name"]])
            stack.append((depth, m["name"]))
            values[path] = set()
            continue
        v = VALUE_LINE.match(ln)
        if v and stack:
            path = ".".join(s[1] for s in stack)
            values[path].add(v["value"])
    return values, set(values)


def accepted_fields(model, prefix: str = "") -> set[str]:
    out = set()
    for name, info in model.model_fields.items():
        key = info.alias or name
        out.add(prefix + key)
        sub = getattr(model, "__docket_fields__", ())
        spec = next((f for f in sub if f.name == key), None)
        if spec is not None and spec.type == "rows":
            inner = info.annotation
            args = getattr(inner, "__args__", ())
            for a in args:
                for b in getattr(a, "__args__", (a,)):
                    if hasattr(b, "model_fields"):
                        out |= accepted_fields(b, prefix + key + ".")
    return out


@pytest.mark.parametrize("persona,kind_name", CASES)
def test_prompt_names_exactly_the_accepted_fields(persona, kind_name):
    _, kind, _, prompt = _pack(persona, kind_name)
    _, fields = named(prompt)
    assert fields == accepted_fields(kind.row_model)


@pytest.mark.parametrize("persona,kind_name", CASES)
def test_every_named_value_is_accepted_without_a_safe_reading(persona, kind_name):
    _, kind, _, prompt = _pack(persona, kind_name)
    values, _ = named(prompt)
    assert any(values.values())  # every kind has an enumerated field
    for path, vals in values.items():
        parts = path.split(".")
        spec = kind.field(parts[0])
        parent = None
        if len(parts) == 2:
            parent, spec = spec, next(f for f in spec.rows if f.name == parts[1])
        if spec is None or spec.type != "enum":
            assert not vals, path
            continue
        assert vals == set(spec.value_names())
        for v in vals:
            row = _minimal_row(kind, "x")
            if parent is None:
                row[spec.name] = v
            else:
                row[parent.name] = [{**row[parent.name][0], spec.name: v}]
            with collecting() as col:
                got = submit._clean(kind.row_model.model_validate(row))
            assert (got[spec.name] if parent is None else got[parent.name][0][spec.name]) == v
            assert [s for s in col.peek() if s.area == "task.vocabulary"] == []


def _minimal_row(kind, rid: str) -> dict[str, Any]:
    from docket.tasks.prompts import _example_value
    pack = {"items": [{"id": rid, "source": "chat", "lines": [{"line": "L1", "text": "t"}]}]}
    return {"id": rid, **{f.name: _example_value(f, pack, rid) for f in kind.fields if f.required}}


@pytest.mark.parametrize("persona,kind_name", CASES)
def test_rules_in_the_prompt_name_accepted_fields(persona, kind_name):
    _, kind, _, prompt = _pack(persona, kind_name)
    spec = spec_section(prompt)
    if "Rules:" not in spec:
        assert not kind.rules
        return
    rules = spec.split("Rules:", 1)[1].split("Other fields are ignored", 1)[0]
    accepted = accepted_fields(kind.row_model)
    enum_values = {v for f in kind.fields for v in f.value_names()}
    for word in re.findall(r"`([^`]+)`", rules):
        assert word in accepted or word in enum_values, word
    assert len([ln for ln in rules.splitlines() if ln.startswith("- ")]) == len(kind.rules)


@pytest.mark.parametrize("persona,kind_name", CASES)
def test_the_embedded_example_validates(persona, kind_name):
    _, kind, pack, prompt = _pack(persona, kind_name)
    block = spec_section(prompt).split("```json", 1)[1].split("```", 1)[0]
    example = json.loads(block)
    kind.result_model.model_validate(example)
    one = {**pack, "items": [it for it in pack["items"] if it["id"] == example["items"][0]["id"]]}
    rows = [submit._clean(r) for r in kind.result_model.model_validate(example).items]
    errs = submit.row_errors(kind, one, rows, raw_from_lines(one))
    # only the quote text is a placeholder: an example never copies untrusted content out of the fence
    assert {e["code"] for e in errs} <= {"task.evidence_not_verbatim"}


# --- results generated from the JSON Schema --------------------------------------------


def from_schema(schema: dict[str, Any], defs: dict[str, Any], ids: list[str]) -> st.SearchStrategy[Any]:
    if "$ref" in schema:
        return from_schema(defs[schema["$ref"].split("/")[-1]], defs, ids)
    if "anyOf" in schema:
        return st.one_of([from_schema(s, defs, ids) for s in schema["anyOf"]])
    if "enum" in schema:
        return st.one_of(st.sampled_from(schema["enum"]), st.text(max_size=12))
    t = schema.get("type")
    if t == "object":
        props = schema.get("properties", {})
        req = set(schema.get("required", []))
        strat = {k: from_schema(v, defs, ids) for k, v in props.items()}
        if "id" in props:
            strat["id"] = st.one_of(st.sampled_from(ids), st.text(max_size=6))
        return st.fixed_dictionaries({k: v for k, v in strat.items() if k in req},
                                     optional={k: v for k, v in strat.items() if k not in req})
    if t == "array":
        return st.lists(from_schema(schema["items"], defs, ids), max_size=4)
    if t == "string":
        if schema.get("format") == "date":
            return st.one_of(st.dates().map(lambda d: d.isoformat()), st.text(max_size=12))
        return st.text(min_size=0, max_size=int(schema.get("maxLength", 40)) + 5)
    if t == "integer":
        return st.integers(min_value=int(schema.get("minimum", -10)) - 5, max_value=int(schema.get("maximum", 200)) + 5)
    if t == "boolean":
        return st.booleans()
    if t == "null":
        return st.none()
    return st.none()


def _payloads(kind, pack):
    sch = kind.result_model.model_json_schema()
    ids = [it["id"] for it in pack["items"]]
    rows = st.lists(from_schema(sch["$defs"][sch["properties"]["items"]["items"]["$ref"].split("/")[-1]],
                                sch["$defs"], ids), min_size=0, max_size=len(ids) + 1)
    covering = st.permutations(ids).flatmap(lambda order: st.tuples(*[
        from_schema(sch["$defs"][sch["properties"]["items"]["items"]["$ref"].split("/")[-1]], sch["$defs"], [i])
        for i in order]).map(lambda rs: [{**r, "id": i} for r, i in zip(rs, order)]))
    junk = st.recursive(st.none() | st.booleans() | st.integers() | st.text(max_size=5),
                        lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=4), c, max_size=3),
                        max_leaves=8)
    return st.one_of(st.one_of(rows, covering).map(lambda r: {"items": r}), junk)


@pytest.mark.parametrize("persona,kind_name", CASES)
def test_generated_results_validate_or_refuse_with_a_documented_code(persona, kind_name):
    _, kind, pack, _ = _pack(persona, kind_name)
    raw = raw_from_lines(pack)
    seen: set[str] = set()

    @settings(max_examples=60, deadline=None, suppress_health_check=list(HealthCheck), derandomize=True)
    @given(_payloads(kind, pack))
    def check(payload):
        try:
            submit.validate(kind, pack, payload, raw_text=raw, rerun="docket task submit t x")
            seen.add("accepted")
        except DocketError as e:
            assert e.kind in ("input", "integrity"), e.code
            seen.add(e.code)

    check()
    assert seen  # the property ran
