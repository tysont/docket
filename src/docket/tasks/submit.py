"""Task submission and validation (TD §9.4).

``validate`` checks a result in order:

1. **Schema**: the kind's output model, with its declared tolerant coercions.
   An open vocabulary value outside the declared set takes the field's safe
   reading here and records an unexpected shape (step 4 of TD §9.4; it never
   refuses). Unknown fields are ignored and recorded.
2. **Identity partition**: exactly the pack's item ids. Integrity: refuses.
3. **Per-row integrity**: copied identity fields (``source``) equal the pack
   item's; an item reference names another pack item; every evidence quote
   appears verbatim, after whitespace normalization only, in the decoded raw
   record of its item, and names one of the item's line ids when it names one.
4. **Cross-field rules** declared on the kind (for example ``keep: true``
   requires ``owner``, ``ask`` and ``evidence``).

A refused row is reported with its field path, the received value, the rule,
the fix, and the submit command to rerun; the submission is refused as a whole
so the result on disk always covers the whole pack. Shapes recorded while
validating reach the envelope only when the result is accepted.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, ValidationError

from docket.cli.errors import InputError, IntegrityError
from docket.shapes.unexpected import Shape, collecting, replay, unexpected
from docket.tasks.evidence import normalize

if TYPE_CHECKING:
    from docket.tasks.kinds import FieldSpec, TaskKind

MAX_ERRORS = 50
RawText = Callable[[str], "str | None"]


@dataclass
class Accepted:
    body: dict[str, Any]
    shapes: list[Shape] = field(default_factory=list)


# --- helpers -------------------------------------------------------------------


def _path(loc: tuple[Any, ...] | list[Any]) -> str:
    out = ""
    for p in loc:
        out += f"[{p}]" if isinstance(p, int) else (("." if out else "") + str(p))
    return out


def _spec_at(kind: "TaskKind", loc: tuple[Any, ...]) -> "FieldSpec | None":
    fields = kind.fields
    found = None
    for p in loc[2:]:
        if isinstance(p, int):
            continue
        found = next((f for f in fields if f.name == p), None)
        if found is None:
            return None
        fields = found.rows
        if found.type == "evidence":
            break
    return found


def _fix(spec: "FieldSpec | None", err: dict[str, Any]) -> str:
    from docket.tasks.prompts import type_text
    t = err.get("type", "")
    if spec is None:
        if t == "missing":
            return f"add `{err['loc'][-1]}`"
        return "match the output specification in prompt.md"
    if t == "missing":
        return f"add `{spec.name}` ({type_text(spec)}): {spec.description}"
    if spec.type == "enum":
        return "use one of: " + ", ".join(spec.value_names())
    if spec.type == "evidence":
        if t == "too_long":
            return f"keep at most {spec.max_items} quotes"
        if t == "string_too_long":
            return f"keep each quote to at most {spec.max_quote} characters"
        return "give quotes as {\"line\": \"L1\", \"quote\": \"words copied exactly\"}"
    if t == "string_too_long":
        return f"shorten `{spec.name}` to at most {spec.max_length} characters"
    return f"give `{spec.name}` as: {type_text(spec)}"


def _row_id(payload: Any, loc: tuple[Any, ...]) -> Any:
    if len(loc) >= 2 and loc[0] == "items" and isinstance(loc[1], int) and isinstance(payload, dict):
        rows = payload.get("items")
        if isinstance(rows, list) and 0 <= loc[1] < len(rows) and isinstance(rows[loc[1]], dict):
            return rows[loc[1]].get("id")
    return None


def _clean(model: BaseModel) -> dict[str, Any]:
    """The declared fields only (by alias), recursively; extra fields are not part of a result."""
    out: dict[str, Any] = {}
    for name, info in type(model).model_fields.items():
        v = getattr(model, name)
        key = info.alias or name
        if v is None:
            continue
        if isinstance(v, BaseModel):
            v = _clean(v)
        elif isinstance(v, list):
            v = [_clean(x) if isinstance(x, BaseModel) else x for x in v]
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[key] = v
    return out


def _extras(kind: "TaskKind", model: BaseModel, where: str) -> None:
    for k in sorted((model.model_extra or {})):
        unexpected("task.extra_field", f"{kind.name}.{where}{k}", reading="ignore the field",
                   extend=f"the {kind.name} output model ({'tasks.kinds.' + kind.name if not kind.core else 'docket/tasks/kinds.py'})")
    for name, info in type(model).model_fields.items():
        v = getattr(model, name)
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, BaseModel) and x.model_extra is not None and name != "items":
                _extras(kind, x, f"{where}{info.alias or name}.")


# --- validation ------------------------------------------------------------------


def schema_errors(kind: "TaskKind", payload: Any, e: ValidationError) -> list[dict[str, Any]]:
    rows = []
    for err in e.errors():
        loc = tuple(err["loc"])
        spec = _spec_at(kind, loc)
        row = {"path": _path(loc), "received": err.get("input") if err.get("type") != "missing" else None,
               "rule": err["msg"], "fix": _fix(spec, err), "code": "task.invalid_result"}
        rid = _row_id(payload, loc)
        if rid is not None:
            row["id"] = rid
        rows.append(row)
    return rows


def _evidence_fields(fields: tuple["FieldSpec", ...], row: dict[str, Any], base: str) -> list[tuple[str, list[Any]]]:
    out = []
    for f in fields:
        v = row.get(f.name)
        if f.type == "evidence" and isinstance(v, list):
            out.append((f"{base}.{f.name}", v))
        elif f.type == "rows" and isinstance(v, list):
            for j, sub in enumerate(v):
                if isinstance(sub, dict):
                    out += _evidence_fields(f.rows, sub, f"{base}.{f.name}[{j}]")
    return out


def row_errors(kind: "TaskKind", pack: dict[str, Any], rows: list[dict[str, Any]],
               raw_text: RawText) -> list[dict[str, Any]]:
    items = {it["id"]: it for it in pack["items"]}
    errors: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        rid = row["id"]
        item = items[rid]
        base = f"items[{i}]"
        # identity fields copied from the pack item
        for f in kind.fields:
            if f.echo and f.name in row and row[f.name] != item.get(f.echo):
                errors.append({"id": rid, "path": f"{base}.{f.name}", "received": row[f.name],
                               "code": "task.identity_mismatch",
                               "rule": f"`{f.name}` must equal the pack item's `{f.echo}`",
                               "fix": f"copy {item.get(f.echo)!r} from the pack item"})
            if f.type == "item_ref" and row.get(f.name) is not None:
                ref = row[f.name]
                if ref == rid or ref not in items:
                    errors.append({"id": rid, "path": f"{base}.{f.name}", "received": ref, "code": "task.unknown_ref",
                                   "rule": f"`{f.name}` must name another item of this pack",
                                   "fix": "copy the id of the other pack item exactly, or change the verdict"})
        # verbatim evidence
        quotes = _evidence_fields(kind.fields, row, base)
        if quotes:
            raw = raw_text(rid)
            if raw is None:
                unexpected("task.evidence", f"{kind.name} item has no raw record",
                           reading="verify quotes against the item's pack lines",
                           extend="docket/tasks/evidence.py: record_ids")
                raw = normalize("\n".join(ln["text"] for ln in item.get("lines", [])))
            line_ids = {ln["line"] for ln in item.get("lines", [])}
            for path, evs in quotes:
                for j, ev in enumerate(evs):
                    line, quote = ev.get("line"), ev.get("quote", "")
                    if line is not None and line not in line_ids:
                        errors.append({"id": rid, "path": f"{path}[{j}].line", "received": line,
                                       "code": "task.evidence_unknown_line",
                                       "rule": "a quote's line id must be one of the item's line ids",
                                       "fix": "use one of: " + ", ".join(sorted(line_ids, key=lambda s: int(s[1:])))})
                    nq = normalize(quote)
                    if not nq or nq not in raw:
                        errors.append({"id": rid, "path": f"{path}[{j}].quote", "received": quote,
                                       "code": "task.evidence_not_verbatim",
                                       "rule": "a quote must appear word for word in the item's raw record "
                                               "(only spacing may differ)",
                                       "fix": "copy the words exactly from one of the item's lines; do not "
                                              "paraphrase, shorten inside the quote or fix spelling"})
        # cross-field rules
        for r in kind.rules:
            for name in r.missing(row):
                f = kind.field(name)
                errors.append({"id": rid, "path": f"{base}.{name}", "received": row.get(name),
                               "code": "task.cross_field", "rule": r.describe(),
                               "fix": f"add `{name}`: {f.description}" if f else f"add `{name}`"})
    return errors


def validate(kind: "TaskKind", pack: dict[str, Any], payload: Any, *, raw_text: RawText,
             rerun: str) -> Accepted:
    """Validate ``payload`` as the result of ``pack``. Raises a refusal or returns the accepted body."""
    ids = [it["id"] for it in pack["items"]]
    with collecting() as col:
        try:
            model = kind.result_model.model_validate(payload)
        except ValidationError as e:
            rows = schema_errors(kind, payload, e)
            raise InputError(
                "task.invalid_result", f"The {kind.name} result does not match its output model "
                                       f"({len(rows)} error(s)); every error names the row, the rule and the fix.",
                hint=rerun, details={"errors": rows[:MAX_ERRORS], "schema": kind.output_schema, "rerun": rerun},
            ) from None
        _extras(kind, model, "")
        for r in model.items:  # type: ignore[attr-defined]
            _extras(kind, r, "")
        rows = [_clean(r) for r in model.items]  # type: ignore[attr-defined]
        got = [r["id"] for r in rows]
        counts = Counter(got)
        dup = sorted(i for i, c in counts.items() if c > 1)
        missing = sorted(set(ids) - set(got))
        extra = sorted(set(got) - set(ids))
        if dup or missing or extra:
            raise IntegrityError(
                "integrity.partition",
                f"The result must cover exactly the pack's {len(ids)} item ids "
                f"(missing {len(missing)}, extra {len(extra)}, duplicated {len(dup)}).",
                hint="Return one row per pack item id, copied exactly from the pack; then rerun: " + rerun,
                details={"missing": missing, "extra": extra, "duplicates": dup, "rerun": rerun},
            )
        errs = row_errors(kind, pack, rows, raw_text)
        if errs:
            refused = sorted({e["id"] for e in errs})
            raise IntegrityError(
                "task.rows_refused",
                f"{len(refused)} of {len(rows)} row(s) refused; fix the rows listed in details.rows and rerun "
                f"the submit command.",
                hint=rerun,
                details={"rows": errs[:MAX_ERRORS], "refused": refused, "accepted_rows": len(rows) - len(refused),
                         "rerun": rerun},
            )
        shapes = col.drain()
    for s in shapes:
        replay(s)
    return Accepted({"items": rows}, shapes)
