"""Reference and template checks for the ``tasks`` section (TD §3.4 passes 2 and 3).

Structure is checked by ``docket.config.task_models``. Here: a kind's
``select.sources`` names configured sources, and instructions and guidance use
only the declared instruction context (``me.name``, ``kind``, ``stage``).
"""
from __future__ import annotations

from typing import Any

from docket.config.loader import Issue, Loaded, closest, dotted, origin_for
from docket.text import templates


def _issue(loaded: Loaded, code: str, msg: str, path: tuple[Any, ...], pass_no: int, **kw: Any) -> Issue:
    org = origin_for(loaded.origins, path)
    return Issue(code, msg, path=dotted(path), file=org.file if org else None,
                 line=org.line if org else None, column=org.column if org else None, pass_no=pass_no, **kw)


def check(loaded: Loaded) -> list[Issue]:
    from docket.tasks.kinds import INSTRUCTIONS_CONTEXT
    d = loaded.data if isinstance(loaded.data, dict) else {}
    tasks = d.get("tasks") if isinstance(d.get("tasks"), dict) else {}
    sources = sorted((d.get("sources") or {}).keys()) if isinstance(d.get("sources"), dict) else []
    out: list[Issue] = []

    def sel(path: tuple[Any, ...], select: Any) -> None:
        if not isinstance(select, dict):
            return
        for i, sid in enumerate(select.get("sources") or []):
            if isinstance(sid, str) and sid not in sources:
                out.append(_issue(loaded, "config.bad_reference",
                                  f"{dotted(path + ('sources', i))} names source {sid!r}, which is not defined",
                                  path + ("sources", i), 2, suggestion=closest(sid, sources)))

    def tpl(path: tuple[Any, ...], text: Any) -> None:
        if not isinstance(text, str):
            return
        for err in templates.validate(text, INSTRUCTIONS_CONTEXT):
            where = f" at character {err.pos + 1}" if err.pos is not None else ""
            sugg = closest(err.field_name, INSTRUCTIONS_CONTEXT.names()) if err.field_name else None
            out.append(_issue(loaded, "config.bad_template", f"{dotted(path)}: {err.message}{where}", path, 3,
                              suggestion=sugg))

    for core in ("assess", "review", "extract", "classify"):
        sect = tasks.get(core)
        if isinstance(sect, dict):
            sel(("tasks", core, "select"), sect.get("select"))
            tpl(("tasks", core, "guidance"), sect.get("guidance"))
    kinds = tasks.get("kinds")
    if isinstance(kinds, dict):
        for name, kc in kinds.items():
            if isinstance(kc, dict):
                sel(("tasks", "kinds", name, "select"), kc.get("select"))
                tpl(("tasks", "kinds", name, "instructions"), kc.get("instructions"))
    return out
