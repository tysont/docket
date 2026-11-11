"""Configuration loading, validation, merge, origins and digests (TD §3)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from docket.config import check, explain, load_workspace
from docket.config import presets as preset_mod
from docket.config.loader import load

pytestmark = pytest.mark.smoke

MINIMAL = """\
version: 1
graphs:
  daily:
    gather: { read: inbox }
    decide: { ask: default, from: [gather] }
    plan: { plan: [decide] }
    approve: { approve: plan }
    apply: { apply: approve }
sources:
  inbox: { kind: files, path: inbox.jsonl }
profile:
  timezone: UTC
  me:
    name: Dana Reyes
destinations:
  backlog:
    adapter: markdown_tasks
    path: tasks.md
"""


def write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def codes(issues):
    return [i.code for i in issues]


@pytest.mark.parametrize("name", ["reference-like", "support-lead"])
def test_personas_validate(persona, cli, name):
    ws = persona(name)
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 0, env
    assert env["data"]["valid"] is True
    assert env["workspace"] == str(ws)


BROKEN = """\
version: 1
graphs:
  daily:
    decide: { ask: director }
profile:
  timezone: UTC
  timezon: UTC
  me:
    name: Dana Reyes
destinations:
  backlog:
    adapter: markdown_tasks
    path: tasks.md
    line_template: "- [ ] {titel}"
"""


def test_unknown_key_bad_reference_bad_template_all_reported(tmp_path, cli):
    ws = write(tmp_path / "ws", {"docket.yaml": BROKEN})
    code, env = cli("config", "validate", "--workspace", str(ws))
    assert code == 3
    err = env["error"]
    assert err["code"] == "config.invalid" and err["kind"] == "config"
    rows = {r["code"]: r for r in err["details"]["errors"]}
    assert set(rows) == {"config.unknown_key", "config.bad_reference", "config.bad_template"}
    unk = rows["config.unknown_key"]
    assert (unk["file"], unk["line"], unk["column"]) == ("docket.yaml", 7, 3)
    assert unk["suggestion"] == "timezone" and unk["path"] == "profile.timezon"
    ref = rows["config.bad_reference"]
    assert (ref["path"], ref["file"], ref["line"]) == ("graphs.daily.decide.ask", "docket.yaml", 4)
    tpl = rows["config.bad_template"]
    assert (tpl["path"], tpl["line"], tpl["suggestion"]) == ("destinations.backlog.line_template", 14, "title")
    # human output names file, line and the fix
    code, _ = cli("config", "validate", "--workspace", str(ws), json_out=False)
    assert code == 3
    assert "docket.yaml:7:3: config.unknown_key" in cli.last_stderr
    assert "did you mean 'timezone'?" in cli.last_stderr
    # a run never starts on invalid configuration
    code, env = cli("run", "start", "daily", "--workspace", str(ws))
    assert code == 3 and env["error"]["code"] == "config.invalid"


def test_yaml_syntax_error_has_position(tmp_path):
    ws = write(tmp_path / "ws", {"docket.yaml": "version: 1\nprofile: [unclosed\n"})
    _, issues, _ = check(ws)
    assert issues[0].code == "config.yaml_syntax" and issues[0].file == "docket.yaml" and issues[0].line


def test_includes_and_duplicate_section(tmp_path):
    ws = write(tmp_path / "ws", {
        "docket.yaml": "version: 1\nincludes: [profile.yaml, more.yaml, missing.yaml]\n",
        "profile.yaml": "profile:\n  me:\n    name: Dana Reyes\n",
        "more.yaml": "profile:\n  me:\n    name: Other\n",
    })
    _, issues, _ = check(ws)
    found = {i.code: i for i in issues}
    assert found["config.duplicate_section"].file == "more.yaml" and found["config.duplicate_section"].line == 1
    assert found["config.include_missing"].file == "docket.yaml" and found["config.include_missing"].line == 2


def test_unknown_preset_suggests(tmp_path):
    ws = write(tmp_path / "ws", {"docket.yaml": MINIMAL + "presets: [markdown/task-basic]\n"})
    _, issues, _ = check(ws)
    i = next(x for x in issues if x.code == "config.unknown_preset")
    assert i.suggestion == "markdown/tasks-basic" and i.line == MINIMAL.count("\n") + 1


def test_semantic_checks(tmp_path):
    ws = write(tmp_path / "ws", {"docket.yaml": MINIMAL.replace("timezone: UTC", "timezone: Mars/Base")
               .replace("  inbox: { kind: files, path: inbox.jsonl }\n",
                        "  inbox: { kind: files, path: inbox.jsonl }\n"
                        "  feed:\n    kind: filez\n    mapping: { titel: \"$.x\", id: \"x\" }\n")
               + "  agendas2:\n    adapter: markdown_agenda\n"})
    text = (ws / "docket.yaml").read_text()
    text = text.replace("profile:\n", "profile:\n  calendar:\n    classify:\n      - { match: { title_regex: \"(\" }, kind: focus }\n", 1)
    (ws / "docket.yaml").write_text(text)
    _, issues, _ = check(ws)
    cs = codes(issues)
    for c in ("config.bad_timezone", "config.bad_regex", "config.bad_mapping", "config.unknown_kind"):
        assert c in cs, (c, cs)
    kind = next(i for i in issues if i.code == "config.unknown_kind")
    assert kind.suggestion == "files"


GRAPH_CASES = [
    # a cycle through after
    ("    gather: { read: inbox, after: [apply] }\n", "gather", "the graph has a cycle: gather -> apply -> approve"),
    # a node that names a node the graph does not have
    ("    decide: { ask: default, from: [gathr] }\n", "decide", "node decide names 'gathr', which is not a node"),
    # an input of the wrong kind: approve works on a plan, not on items
    ("    approve: { approve: gather }\n", "approve", "node approve (approve) cannot work on gather (read), which "
                                                       "hands on items"),
    # the seal is not a node anyone may name
    ("    seal: { run: [\"true\"] }\n", "seal", "node name 'seal' must be a lower-case word"),
]


@pytest.mark.parametrize("line,node,message", GRAPH_CASES)
def test_graph_shape_problems_are_reported_at_the_node(tmp_path, line, node, message):
    """A graph's shape replaces the fixed order of the old model: every node may run once what it
    names is done, so the checks are names, references, cycles and what each verb can work on."""
    key = line.split(":", 1)[0]
    lines = [ln for ln in MINIMAL.splitlines(keepends=True)]
    at = next((i for i, ln in enumerate(lines) if ln.startswith(key + ":")), None)
    if at is None:
        at = lines.index("    apply: { apply: approve }\n") + 1
        lines.insert(at, line)
    else:
        lines[at] = line
    ws = write(tmp_path / "ws", {"docket.yaml": "".join(lines)})
    _, issues, _ = check(ws)
    bad = [i for i in issues if i.code == "config.bad_graph"]
    assert bad, codes(issues)
    hit = next(i for i in bad if message in i.message)
    assert hit.path == f"graphs.daily.{node}" and hit.file == "docket.yaml" and hit.line == at + 1


def test_a_node_has_exactly_one_verb_and_the_verbs_own_inputs(tmp_path):
    ws = write(tmp_path / "ws", {"docket.yaml": MINIMAL.replace(
        "    plan: { plan: [decide] }\n", "    plan: { plan: [decide], from: [gather] }\n").replace(
        "    gather: { read: inbox }\n", "    gather: { read: inbox, run: [\"true\"] }\n")})
    _, issues, _ = check(ws)
    msgs = {i.message for i in issues if i.code == "config.bad_type"}
    assert any("a node has exactly one verb" in m for m in msgs), msgs
    assert any("plan names its inputs itself; it takes no from" in m for m in msgs), msgs


def test_a_graph_with_no_nodes_is_refused(tmp_path):
    ws = write(tmp_path / "ws", {"docket.yaml": MINIMAL.replace(
        "graphs:\n  daily:\n", "graphs:\n  empty: {}\n  daily:\n")})
    _, issues, _ = check(ws)
    (bad,) = [i for i in issues if i.code == "config.bad_graph"]
    assert bad.path == "graphs.empty" and "has no nodes" in bad.message


def test_capability_missing(tmp_path):
    ws = write(tmp_path / "ws", {"docket.yaml": MINIMAL.replace("adapter: markdown_tasks", "adapter: journal")})
    _, issues, _ = check(ws)
    assert "config.capability_missing" in codes(issues)


def test_presets_merge_extend_remove_and_origins(tmp_path):
    ws = write(tmp_path / "ws", {"docket.yaml": MINIMAL.replace(
        "destinations:\n  backlog:\n    adapter: markdown_tasks\n    path: tasks.md\n",
        "destinations:\n  backlog:\n    path: tasks.md\n") + textwrap.dedent("""\
        presets: [markdown/tasks-basic, options/basic]
        decisions:
          option_sets:
            basic:
              finding:
                remove: [watch]
            default:
              finding:
                extend:
                  - { id: later, action: watch, label: "Later" }
        """)})
    w = load_workspace(ws)
    basic = [o.id for o in w.config.decisions.option_sets["basic"]["finding"]]
    assert basic == ["task", "dismiss"]
    default = [o.id for o in w.config.decisions.option_sets["default"]["finding"]]
    assert default == ["task", "dismiss", "later"]
    assert w.config.destinations["backlog"].adapter == "markdown_tasks"
    assert explain(w, "destinations.backlog.adapter")["origin"] == "preset:markdown/tasks-basic@markdown/tasks-basic.yaml:5"
    assert explain(w, "destinations.backlog.path")["origin"].startswith("workspace:docket.yaml:")
    assert explain(w, "decisions.option_sets.default.finding[2].id")["origin"].startswith("workspace:docket.yaml:")
    assert explain(w, "decisions.option_sets.default.finding[0].id")["origin"] == "core"
    assert explain(w, "decisions.option_sets.basic.finding[1].id")["origin"].startswith("preset:options/basic@")
    assert explain(w, "tasks.concurrency")["value"] == 6


def test_config_explain_cli(persona, cli):
    ws = persona("reference-like")
    code, env = cli("config", "explain", "profile.timezone", "--workspace", str(ws))
    assert code == 0 and env["data"]["value"] == "America/Chicago"
    assert env["data"]["origin"] == "workspace:profile.yaml:2"
    code, env = cli("config", "explain", "profile.timezon", "--workspace", str(ws))
    assert code == 2 and env["error"]["code"] == "input.unknown_path"
    assert env["error"]["details"]["suggestion"] == "timezone"


def test_preset_may_not_define_people(tmp_path, monkeypatch):
    root = tmp_path / "presets"
    write(root, {"bad/people.yaml": "profile:\n  people:\n    - { id: x, name: X }\n"})
    monkeypatch.setattr(preset_mod, "PRESET_ROOT", root)
    # no files source here: a files source extends a core preset, which the patched root lacks
    bare = MINIMAL.replace("    gather: { read: inbox }\n", "").replace(", from: [gather]", "") \
        .replace("sources:\n  inbox: { kind: files, path: inbox.jsonl }\n", "")
    ws = write(tmp_path / "ws", {"docket.yaml": bare + "presets: [bad/people]\n"})
    loaded = load(ws)
    assert [i.code for i in loaded.issues] == ["config.preset_forbidden"]


def test_digest_ignores_file_split_and_partials_move_per_section(tmp_path):
    one = write(tmp_path / "one", {"docket.yaml": MINIMAL})
    head, _, rest = MINIMAL.partition("profile:")
    two = write(tmp_path / "two", {"docket.yaml": head + "includes: [p.yaml]\n", "p.yaml": "profile:" + rest})
    a, b = load_workspace(one), load_workspace(two)
    assert a.digest == b.digest
    (one / "docket.yaml").write_text(MINIMAL + "planning:\n  policy: { granularity: 30m }\n")
    c = load_workspace(one)
    changed = {k for k in c.sections if c.sections[k] != a.sections[k]}
    assert changed == {"planning"} and c.digest != a.digest


def test_relationship_sets(persona):
    w = load_workspace(persona("reference-like"))
    sets = w.relationship_sets()
    assert sets["chain"] == ["lee"]
    assert sets["reports"] == ["kim", "sam"]
    assert sets["skip_reports"] == ["ola"]
    assert sets["peers"] == ["jordan"]
    assert sets["group:platform"] == ["kim", "ola", "sam"]
    assert sets["external"] == ["casey"]
    assert sets["role:manager"] == ["lee"]


def test_init_writes_valid_workspace(tmp_path, cli):
    code, env = cli("init", str(tmp_path / "new"), "--preset", "markdown/tasks-basic")
    assert code == 0 and env["data"]["valid"] is True, env
    code, env = cli("config", "validate", "--workspace", str(tmp_path / "new"))
    assert code == 0
    code, env = cli("init", str(tmp_path / "new"))
    assert code == 2 and env["error"]["code"] == "input.not_empty"
    code, env = cli("init", str(tmp_path / "x"), "--preset", "nope/nothing")
    assert code == 2 and env["error"]["code"] == "input.unknown_preset"
    code, env = cli("init", str(tmp_path / "dry"), "--dry-run")
    assert code == 0 and not (tmp_path / "dry").exists()


def test_presets_list_and_show(cli):
    code, env = cli("presets", "list")
    ids = [p["id"] for p in env["data"]["presets"]]
    assert "markdown/tasks-basic" in ids and all(p["description"] for p in env["data"]["presets"])
    code, env = cli("presets", "show", "options/basic")
    assert code == 0 and env["data"]["sections"] == ["decisions"]


def test_workspace_discovery(persona, cli, tmp_path):
    ws = persona("reference-like")
    code, env = cli("config", "validate", cwd=ws / "data")
    assert code == 0 and env["workspace"] == str(ws)
    code, env = cli("config", "validate", cwd=tmp_path, env={"DOCKET_WORKSPACE": str(ws)})
    assert code == 0 and env["workspace"] == str(ws)
    code, env = cli("config", "validate", cwd=tmp_path)
    assert code == 3 and env["error"]["code"] == "workspace.not_found"
