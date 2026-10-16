"""The Markdown adapters: ``markdown_tasks``, ``markdown_agenda`` and ``journal`` (TD §11.3).

Ownership (TD §11.2): an update replaces exactly one line whose digest equals
the operation's ``old_digest`` inside the anchor section. Zero or several
matches refuse with ``dest.line_changed`` and the section's current text; a
line that changed since the plan was built is never overwritten. Appends go to
the end of the anchor section. Unknown sections, dated history and lines the
grammar cannot parse are left exactly as they are.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docket.cli.errors import IntegrityError
from docket.config.defaults import DEFAULT_SECTION, DEFAULT_TASK_LINE
from docket.config.digest import digest
from docket.destinations import quality as quality_mod
from docket.destinations.base import (
    Acceptance, Action, BaseDestination, DestEnv, DestState, Operation, Receipt, VerifyResult, make_op,
)
from docket.destinations.markdown_io import Doc, FileIO, TwinIO, line_digest
from docket.shapes.unexpected import unexpected
from docket.text import templates
from docket.text.grammar import Grammar, TaskLine
from docket.text.redact import Redactor

DEFAULT_AGENDA_SECTION = "## Next"
DEFAULT_AGENDA_LINE = "- {title}"
DEFAULT_JOURNAL_HEADING = "## {run.date} {run.id}"
_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s")


class MarkdownBase(BaseDestination):
    def __init__(self, dest_id: str, cfg: Any, denv: DestEnv, *, io: FileIO | None = None, twin: bool = False) -> None:
        super().__init__(dest_id, cfg, denv, twin=twin)
        self.io = io or FileIO(denv.root)
        self._redactor: Redactor | None = None

    def sandbox(self) -> "MarkdownBase":
        base = self.denv.sandbox_dir
        if base is None:
            import tempfile
            base = Path(tempfile.mkdtemp(prefix="docket-twin-"))
        live = self.io.live if isinstance(self.io, TwinIO) else self.io
        return type(self)(self.id, self.cfg, self.denv, io=TwinIO(base / self.id, live), twin=True)

    @property
    def redactor(self) -> Redactor:
        if self._redactor is None:
            self._redactor = Redactor.from_config(self.denv.profile, self.cfg.redact)
        return self._redactor

    def shared_text(self, text: str, where: str) -> str:
        """Redact and leak-check text for a shared destination; private text passes through."""
        if self.visibility != "shared":
            return text
        out = self.redactor.redact(text)
        self.redactor.check(out, where=f"destinations.{self.id}.{where}")
        return out

    def doc(self, rel: str) -> Doc:
        return Doc.parse(self.io.read(rel))

    def save(self, rel: str, doc: Doc) -> None:
        self.io.write(rel, doc.render())

    def line_changed(self, op_or_path: Any, anchor: str, doc: Doc, matches: int, message: str) -> IntegrityError:
        rows = doc.body(anchor) or []
        current = "\n".join(ln for _, ln in rows).strip("\n")
        path = op_or_path.path if isinstance(op_or_path, Operation) else op_or_path
        return IntegrityError(
            "dest.line_changed", f"{self.id}: {message}", hint="docket plan build",
            details={"destination": self.id, "path": path, "anchor": anchor, "matches": matches, "current": current})

    def own_path(self, op: Operation, allowed: set[str]) -> None:
        if op.path not in allowed:
            raise IntegrityError("dest.unowned", f"{self.id}: {op.path} is not a file this destination owns",
                                 hint=f"docket config explain destinations.{self.id}",
                                 details={"destination": self.id, "path": op.path, "owned": sorted(allowed)[:20]})

    def ext(self, path: str, line: str) -> str:
        return f"{path}#{line_digest(line)[:12]}"

    # --- shared apply/verify for append and replace --------------------------
    def _append(self, op: Operation, capacity: int | None = None) -> Receipt:
        doc = self.doc(op.path)
        if doc.count_in(op.anchor, op.line):
            return self.receipt(op, "already_present", self.ext(op.path, op.line))
        if doc.section(op.anchor) is None:
            unexpected("dest.markdown", "anchor section missing", sample=op.anchor,
                       reading="append the section at the end of the file",
                       extend=f"destinations.{self.id}.section")
        elif capacity is not None and self.items(doc, op.anchor) >= capacity:
            raise IntegrityError("dest.not_accepted", f"{self.id}: {op.path} {op.anchor} is full ({capacity})",
                                 hint="docket plan build",
                                 details={"destination": self.id, "path": op.path, "capacity": capacity})
        doc.append_in(op.anchor, op.line)
        self.save(op.path, doc)
        return self.receipt(op, "applied", self.ext(op.path, op.line))

    def _replace(self, op: Operation) -> Receipt:
        doc = self.doc(op.path)
        m = doc.matches(op.anchor, op.old_digest or "")
        if len(m) == 1:
            doc.lines[m[0]] = op.line
            self.save(op.path, doc)
            return self.receipt(op, "applied", self.ext(op.path, op.line))
        if not m and doc.count_in(op.anchor, op.line) == 1:
            return self.receipt(op, "already_present", self.ext(op.path, op.line))
        raise self.line_changed(op, op.anchor, doc, len(m),
                                "the line to replace changed since the plan was built" if not m
                                else f"{len(m)} lines match the line to replace")

    def _verify_line(self, op: Operation) -> VerifyResult:
        doc = self.doc(op.path)
        if doc.section(op.anchor) is None:
            return VerifyResult(ok=False, status="missing", detail=f"section {op.anchor} not found")
        if doc.count_in(op.anchor, op.line) < 1:
            return VerifyResult(ok=False, status="missing", detail="line not found in its section")
        if op.verb == "replace" and op.old_line is not None and op.old_line.rstrip() != op.line.rstrip() \
                and doc.matches(op.anchor, op.old_digest or ""):
            return VerifyResult(ok=False, status="mismatch", detail="the old line is still present")
        return VerifyResult(ok=True)

    @staticmethod
    def items(doc: Doc, anchor: str) -> int:
        return sum(1 for _, ln in (doc.body(anchor) or []) if _LIST_ITEM.match(ln))


# --- markdown_tasks -----------------------------------------------------------


@dataclass(frozen=True)
class OpenTask:
    """An open task line as the adapter reads it. ``fields`` are keyed by their name on the line."""
    id: str
    title: str
    line: str
    index: int
    section: str
    fields: dict[str, str]
    tier: int = 0


class MarkdownTasks(MarkdownBase):
    """A Markdown task store with a configurable grammar, tiers and quality rules."""
    adapter = "markdown_tasks"
    capabilities = frozenset({"task.add", "task.update", "task.close", "task.carry"})
    verbs = frozenset({"append", "replace"})

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.grammar = Grammar.from_config(self.cfg.grammar)

    @property
    def rel(self) -> str:
        return self.cfg.path or f"{self.id}.md"

    def anchor_for(self, ctx: dict[str, Any]) -> str:
        tier = ctx.get("tier")
        if tier and tier in self.cfg.sections:
            return self.cfg.sections[tier]
        if tier:
            unexpected("dest.markdown", "unknown tier", sample=str(tier), reading="use the default section",
                       extend=f"destinations.{self.id}.sections")
        return self.cfg.section or DEFAULT_SECTION

    def snapshot(self) -> DestState:
        return DestState(destination=self.id, adapter=self.adapter, data={"path": self.rel, "text": self.io.read(self.rel)})

    def tasks(self, state: DestState) -> list[dict[str, Any]]:
        """Every task line with its section; lines the grammar cannot parse are skipped and recorded."""
        doc = Doc.parse(state.data.get("text"))
        out = []
        for i, ln in enumerate(doc.lines):
            t = self.grammar.parse(ln)
            if t is None:
                if re.match(r"^\s*[-*+] \[.\]", ln) or ln.lstrip().startswith(self.grammar.open.strip()):
                    unexpected("dest.grammar", "task-like line does not parse", sample=ln,
                               reading="leave the line untouched", extend=f"destinations.{self.id}.grammar")
                continue
            out.append({"index": i, "line": ln, "section": doc.section_of(i), "task": t})
        return out

    def task_id(self, t: TaskLine, id_field: str = "id") -> str:
        """A task's identity: its id field, else the destination and its normalized title.

        Field edits (``set_field``, ``close``, carry) leave the title alone, so the line an
        answer appends and the same line read on a later run share one id."""
        return t.get(id_field) or "t_" + digest([self.id, t.title.casefold()])[:16]

    def read_line(self, line: str, *, id_field: str = "id") -> OpenTask | None:
        """An open task line under this destination's grammar, or None."""
        t = self.grammar.parse(line)
        if t is None or t.status != "open":
            return None
        return OpenTask(self.task_id(t, id_field), t.title, line, -1, "", dict(t.fields))

    def open_tasks(self, state: DestState, *, sections: list[str] | None = None,
                   id_field: str = "id") -> list[OpenTask]:
        """Open tasks in file order. With ``sections``, only those headings are read and a task's
        tier is its section's position in the list."""
        wanted = [s.strip() for s in sections] if sections else None
        out = []
        for r in self.tasks(state):
            t = r["task"]
            section = r["section"] or ""  # the heading line as written: it is the anchor of a later edit
            if t.status != "open" or (wanted is not None and section.strip() not in wanted):
                continue
            out.append(OpenTask(self.task_id(t, id_field), t.title, r["line"], r["index"], section,
                                dict(t.fields), wanted.index(section.strip()) if wanted is not None else 0))
        return out

    def preview(self, action: Action, state: DestState, *, fix: quality_mod.Fix | None = None) -> list[Operation]:
        self.require(action)
        ctx = action.fields
        prov = {"question_id": action.question_id, "item_id": action.item_id, "depends_on": action.depends_on or None}
        if action.kind == "task.add":
            line = templates.render(self.cfg.line_template or DEFAULT_TASK_LINE, ctx)
            line = quality_mod.check_line(self.cfg.quality, self.grammar, line, ctx, dest_id=self.id, fix=fix)
            line = self.shared_text(line, "line_template")
            anchor = self.anchor_for(ctx)
            return [make_op(self.id, "append", ident=anchor, path=self.rel, anchor=anchor, line=line, **prov)]
        old, anchor = self._locate(action, state)
        if action.kind == "task.close":
            new = self.grammar.close(old)
        elif action.kind == "task.carry":
            name = self.cfg.carry_field or "carry"
            t = self.grammar.parse(old)
            try:
                n = int((t.get(name) if t else None) or 0)
            except ValueError:
                unexpected("dest.grammar", "carry field is not a number", sample=old, reading="restart the count at 1",
                           extend=f"destinations.{self.id}.carry_field")
                n = 0
            new = self.grammar.set_field(old, name, str(n + 1))
        else:
            new = old
            for k, v in sorted((ctx.get("set") or {}).items()):
                new = self.grammar.set_field(new, k, str(v))
            if ctx.get("title"):
                t = self.grammar.parse(new)
                new = self.grammar.render(t.with_text(str(ctx["title"])))
            new = self.shared_text(new, "line")
        return [make_op(self.id, "replace", ident=anchor, path=self.rel, anchor=anchor, old_digest=line_digest(old),
                        old_line=old, line=new, **prov)]

    def _locate(self, action: Action, state: DestState) -> tuple[str, str]:
        target = action.target or ""
        doc = Doc.parse(state.data.get("text"))
        want = action.fields.get("anchor")
        hits = [(i, ln) for i, ln in enumerate(doc.lines) if ln.rstrip() == target.rstrip() and target.strip()
                and self.grammar.parse(ln) is not None]
        sections = {doc.section_of(i) for i, _ in hits}
        anchor = want or (next(iter(sections)) if len(sections) == 1 else None) or self.cfg.section or DEFAULT_SECTION
        in_anchor = [i for i, _ in hits if doc.section_of(i) == anchor]
        if len(in_anchor) != 1:
            raise self.line_changed(self.rel, anchor, doc, len(in_anchor),
                                    "the task line to change was not found exactly once in its section")
        return doc.lines[in_anchor[0]], anchor

    def apply(self, op: Operation) -> Receipt:
        self.require_verb(op)
        self.own_path(op, {self.rel})
        return self._append(op) if op.verb == "append" else self._replace(op)

    def verify(self, op: Operation, receipt: Receipt) -> VerifyResult:
        return self._verify_line(op)


# --- markdown_agenda ------------------------------------------------------------


class MarkdownAgenda(MarkdownBase):
    """Agendas discovered under a path pattern or listed per forum, with a capacity-limited section."""
    adapter = "markdown_agenda"
    capabilities = frozenset({"agenda.add"})
    verbs = frozenset({"append"})

    @property
    def section(self) -> str:
        return self.cfg.section or DEFAULT_AGENDA_SECTION

    def forum_paths(self) -> dict[str, str]:
        out = {}
        for f in getattr(self.denv.profile, "forums", []) or []:
            ag = f.agenda or {}
            if ag.get("path") and ag.get("destination") in (None, self.id):
                out[f.id] = ag["path"]
        return out

    def discover(self) -> list[str]:
        found = set(self.io.glob(self.cfg.path or "", self.cfg.pattern or "*.md"))
        found |= {p for p in self.forum_paths().values() if self.io.exists(p)}
        return sorted(found)

    def target(self, action: Action) -> str | None:
        ctx = action.fields
        forum = (ctx.get("forum") or {}).get("id") if isinstance(ctx.get("forum"), dict) else ctx.get("forum")
        if forum and forum in self.forum_paths():
            return self.forum_paths()[forum]
        name = ctx.get("agenda") or action.target
        if not name:
            return None
        files = self.discover()
        for f in files:
            if f == name or Path(f).stem == name:
                return f
        return None

    def snapshot(self) -> DestState:
        return DestState(destination=self.id, adapter=self.adapter,
                         data={"section": self.section, "files": {p: self.io.read(p) for p in self.discover()}})

    def _accept(self, action: Action, files: dict[str, str | None]) -> Acceptance:
        base = super().can_accept(action)
        if not base.ok:
            return base
        path = self.target(action)
        if path is None or files.get(path) is None:
            return Acceptance(ok=False, reason=f"{self.id}: no agenda found for this item", details={"path": path})
        cap = self.cfg.capacity
        n = self.items(Doc.parse(files[path]), self.section)
        if cap is not None and n >= cap:
            return Acceptance(ok=False, reason=f"{self.id}: {path} {self.section} is full ({n}/{cap})",
                              details={"path": path, "items": n, "capacity": cap})
        return Acceptance(ok=True, details={"path": path, "items": n, "capacity": cap})

    def can_accept(self, action: Action) -> Acceptance:
        files = {p: self.io.read(p) for p in self.discover()}
        return self._accept(action, files)

    def preview(self, action: Action, state: DestState, **_: Any) -> list[Operation]:
        acc = self._accept(action, state.data.get("files") or {})
        if not acc.ok:
            raise IntegrityError("dest.not_accepted", acc.reason or "not accepted", hint="docket plan build",
                                 details={"destination": self.id, **acc.details})
        line = templates.render(self.cfg.line_template or DEFAULT_AGENDA_LINE, action.fields)
        line = self.shared_text(line, "line_template")
        return [make_op(self.id, "append", ident=self.section, path=acc.details["path"], anchor=self.section,
                        line=line, question_id=action.question_id, item_id=action.item_id,
                        depends_on=action.depends_on or None)]

    def apply(self, op: Operation) -> Receipt:
        self.require_verb(op)
        self.own_path(op, set(self.discover()))
        return self._append(op, capacity=self.cfg.capacity)

    def verify(self, op: Operation, receipt: Receipt) -> VerifyResult:
        return self._verify_line(op)


# --- journal ----------------------------------------------------------------------


class Journal(MarkdownBase):
    """Writes a journal entry as its own section in a Markdown file. An edited entry is never overwritten."""
    adapter = "journal"
    capabilities = frozenset({"journal.write"})
    verbs = frozenset({"write_section"})

    @property
    def rel(self) -> str:
        return self.cfg.path or f"{self.id}.md"

    def snapshot(self) -> DestState:
        return DestState(destination=self.id, adapter=self.adapter, data={"path": self.rel, "text": self.io.read(self.rel)})

    def preview(self, action: Action, state: DestState, **_: Any) -> list[Operation]:
        self.require(action)
        ctx = action.fields
        heading = ctx.get("heading") or templates.render(self.cfg.section or DEFAULT_JOURNAL_HEADING, ctx)
        body = str(ctx.get("body") or "").strip("\n")
        body = self.shared_text(body, "body")
        return [make_op(self.id, "write_section", ident=heading, path=self.rel, anchor=heading, line=body,
                        question_id=action.question_id, item_id=action.item_id)]

    @staticmethod
    def _body(doc: Doc, anchor: str) -> list[str] | None:
        rows = doc.body(anchor)
        if rows is None:
            return None
        lines = [ln for _, ln in rows]
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        return lines

    def apply(self, op: Operation) -> Receipt:
        self.require_verb(op)
        self.own_path(op, {self.rel})
        doc = self.doc(op.path)
        want = (op.line or "").split("\n")
        have = self._body(doc, op.anchor)
        if have is not None:
            if have == want:
                return self.receipt(op, "already_present", self.ext(op.path, op.anchor))
            raise self.line_changed(op, op.anchor, doc, 0, "the journal entry exists with different text")
        if doc.lines and doc.lines[-1].strip():
            doc.lines.append("")
        doc.lines += [op.anchor, *want]
        self.save(op.path, doc)
        return self.receipt(op, "applied", self.ext(op.path, op.anchor))

    def verify(self, op: Operation, receipt: Receipt) -> VerifyResult:
        have = self._body(self.doc(op.path), op.anchor)
        if have is None:
            return VerifyResult(ok=False, status="missing", detail="entry not found")
        ok = have == (op.line or "").split("\n")
        return VerifyResult(ok=ok, status="verified" if ok else "mismatch")
