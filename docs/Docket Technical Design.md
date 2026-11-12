# Docket: Technical Design

**Status:** M0-M9 built
**Date:** 2026-10-16
**Scope:** this document is normative for *how* Docket works. `Docket Product Requirements.md` (the PRD) is normative for *what* and *for whom*, and wins on intent. Requirement ids (FR-, AE-, CFG-, NFR-) refer to the PRD.

---

## 1. Design principles

Seven rules decide every trade-off below. Each comes from a predecessor failure class (PRD §2.2).

1. **Code holds mechanism; configuration holds the world.** No name, handle, domain, folder, calendar word, glyph, host, tool name, weight or label appears in `src/`. The litmus test (PRD §12) enforces it.
2. **Strict configuration, tolerant data.** Configuration is validated completely before a run starts and refuses on any error. Data from sources and agents is read through declared contracts: a known shape is used, an unknown shape takes the contract's safe reading and is recorded as an unexpected shape.
3. **Integrity refuses; shape tolerates.** Digests, bindings, identity partitions, receipts and verbatim evidence are exact. Everything else is a shape.
4. **Derived state is a pure function.** Every derived artifact is a function of immutable raw records, configuration, answers and a pinned clock. Its identity is a digest of those inputs. A changed answer, a configuration edit and a crash mid-run are one operation: recompute what is stale.
5. **One implementation per rule.** A rule (a constraint, a grammar, a vocabulary, a schema) lives in one module that both builds and checks. Prompts are rendered from the same schema the validator uses.
6. **The CLI owns the procedure.** `docket next` computes every ready step from state. No document of command ladders exists for the agent to follow.
7. **Every effect is previewed, approved, receipted and verified.** Adapters have no write path that bypasses this.

## 2. Architecture

### 2.1 Layers

```
            agent (any)                          operator
               |  docket next / commands --json      ^
               v                                     | questions, plan diff
+---------------------------------------------------------------------+
| CLI           argument parsing, envelope, errors, input normalization|
+---------------------------------------------------------------------+
| Engine        graph evaluation, staleness, ready steps, lease        |
+---------------------------------------------------------------------+
| Nodes         read | run | judge | merge | ask | plan | approve |    |
|               apply | write | seal                                   |
+---------------------------------------------------------------------+
| Domain        items, signals, tasks, questions, answers, schedule,   |
|               constraint engine, templates, redaction, dispositions  |
+---------------------------------------------------------------------+
| Ports         Source | Transport | Destination  (+ sandbox twins)    |
+---------------------------------------------------------------------+
| Store         workspace state DB, run directories, artifacts, blobs  |
+---------------------------------------------------------------------+
| Config        loader, presets, schema, origins, digest               |
+---------------------------------------------------------------------+
```

Dependencies point downward only. Domain code never imports a port implementation; it receives ports from the engine. Ports never import domain code beyond the record and operation types.

### 2.2 Implementation choices

| Choice | Decision | Reason |
|---|---|---|
| Language | Python 3.12+ | Matches the supported local tooling and the predecessor's proven libraries; fast enough for the stated budgets (NFR-06). |
| Packaging | `uv`, single package `docket`, console script `docket` | One install, no system Python dependence. |
| Schemas | Pydantic v2 models as the single source of truth; JSON Schema exported from them | Validation, prompt rendering and `docket schema` share one definition (principle 5). |
| YAML | `ruamel.yaml` round-trip loader | Line and column numbers for every key (CFG-02). |
| State | SQLite (WAL) per workspace, plus content-addressed files | Transactions for cursors, receipts and leases; files for large artifacts the agent reads. |
| MCP | Official `mcp` Python SDK v2, stdio and streamable HTTP clients | Generic transport; quirks in presets. |
| HTTP | `httpx` | Timeouts, retries, HTTP/2. |
| Templates | A restricted template language (§3.6), not Jinja | Safe, total, small, and documentable in one page. |
| Concurrency | `asyncio` inside one process | Reading is I/O-bound; no process per item (AE-10). |

Runtime dependencies are Pydantic v2, `ruamel.yaml`, `httpx`, `tzdata` and the official `mcp` Python SDK v2. Development dependencies are `pytest`, Hypothesis and `pytest-xdist`. A dependency joins only when it removes more code than it adds.

### 2.3 Package layout

```
src/docket/
  apply/          approval, freshness, apply boundary and executor
  cli/            entry point, command registry, envelopes and errors
  config/         loader, models, presets, validation, origins and digests
  decide/         questions, options, answers, reconciliation and verification
  destinations/   built-in adapters, ownership, conformance and sandbox twins
  engine/         nodes, graph evaluation, ready steps, doctor and pinned clock
  items/          normalized item model, mapping, identity and the item ledger
  plan/           calendar providers, constraints, scheduler, mirrors and task-store reads
  presets/        shipped YAML presets with no user data
  seal/           cursors, journal, ledgers, dispositions and seal receipts
  shapes/         unexpected-shape recording
  signals/        rules, scoring, merge, replay and explanations
  sources/        reading, records, cursors, contracts and unit registry
  store/          database, runs, blobs, paths and leases
  tasks/          task kinds, packs, prompts, submission and result index
  text/           templates, redaction, grammar, dates and durations
  transports/     MCP, code-mode, exec, HTTP, files and replay transports
```

## 3. Workspace and configuration

### 3.1 Files

A workspace is a directory containing `docket.yaml` and any number of further YAML files it includes. The conventional split is:

```
<workspace>/
  docket.yaml          version, includes, presets, graphs
  profile.yaml         me, people, groups, topics, entities, forums, calendar vocabulary
  sources.yaml         source instances
  signals.yaml         rules, scoring, budgets
  destinations.yaml    destination instances, templates, redaction
  planning.yaml        policy, mirrors
  templates/           optional template files referenced by name
```

Every top-level section may sit in any file; the loader merges by section and refuses a section defined twice outside a preset (CFG-01). `docket` finds the workspace by walking up from the working directory to a `docket.yaml`, or from `--workspace`, or from `DOCKET_WORKSPACE`. Commands echo the resolved workspace in every envelope, so an agent that lost an environment variable sees it at once (AE-05).

### 3.2 Configuration model

Top-level keys, each a Pydantic model with `extra="forbid"`:

| Key | Contents |
|---|---|
| `version` | Configuration schema version, `1` |
| `includes` | Further YAML files, resolved relative to `docket.yaml` |
| `presets` | Ordered list of preset ids to extend |
| `graphs` | Named graphs of nodes (§5) |
| `profile` | `me`, `people`, `groups`, `topics`, `entities`, `forums`, `calendar`, `timezone`, `workdays` |
| `sources` | Source instances keyed by id (§6) |
| `transports` | Shared transport instances keyed by id (§6.3) |
| `signals` | `rules`, `scoring`, `budgets`, `units` classification (§8) |
| `tasks` | Agent task kinds and their overrides (§9) |
| `decisions` | Option sets, label templates, reconcile and covered policy, the order of question kinds (§10) |
| `destinations` | Destination instances keyed by id (§11) |
| `planning` | Scheduling policy, mirrors (§12) |
| `journal` | Journal sections, ledgers (§13.5) |
| `shapes` | Reviewable accepted unexpected shapes (§14.2) |

The profile is the only place people and words of the operator's world live. A short example of its shape, with invented data:

```yaml
profile:
  timezone: America/Chicago
  workdays: [mon, tue, wed, thu, fri]
  me:
    name: Workspace User
    aliases: [User]
    handles: { chat: "users/1001", mail: user@example.org, tickets: user }
  people:
    - id: member
      name: Team Member
      handles: { chat: "users/2002", mail: member@example.org }
      reports_to: me
      roles: [report]
    - id: manager
      name: Team Manager
      roles: [manager]
      reports_to: null
  groups:
    - id: platform
      members: [member]
      forum: platform-sync
  topics:
    - id: ingest
      aliases: [ingest service, ingestion]
      area: Platform
      owned: true
  entities:
    - id: acme
      aliases: [Acme Corp]
      protected: true
      strategic: true
  forums:
    - id: platform-sync
      kind: group
      agenda: { destination: agendas, path: "Syncs/Platform.md" }
      calendar_match: { title_regex: "^Platform Sync" }
  calendar:
    classify:
      - { match: { title_regex: "^Focus" }, kind: focus }
      - { match: { title_regex: "\\bOOO\\b" }, kind: out_of_office }
      - { match: { property: { transparency: transparent } }, kind: informational }
```

Relationship sets (`chain`, `reports`, `skip_reports`, `peers`, `group:<id>`, `external`) are computed in `config/models.py` from `reports_to`, `roles` and mail domains. Rules reference the sets, never people by code (FR-SIG-03).

### 3.3 Presets

Presets are YAML files under `src/docket/presets/`, addressed by id. Preset metadata declares `scope: workspace` or `scope: source`. Workspace presets belong in the top-level `presets` list. Source presets belong in `sources.<id>.extends` and hold a `source` template. A preset may define any section except `profile.me`, `profile.people` and `profile.entities`. The litmus test scans presets as well as code.

`docket init --preset <id>` merges workspace presets and scaffolds named sources for source presets from their metadata. It writes `docket.yaml` only when the complete result validates, so it never leaves an invalid workspace.

Merge order is: core defaults, then each preset in list order, then workspace files. Maps merge deeply; lists replace unless the workspace uses the explicit `{ extend: [...] }` or `{ remove: [...] }` forms. Each resolved value keeps its origin (`core`, `preset:<id>@<file>:<line>`, `workspace:<file>:<line>`), which `docket config explain <path>` prints (CFG-03, AE-04).

### 3.4 Validation

`docket config validate` runs four passes and reports every error, not the first:

1. **Syntax and structure.** YAML parse, unknown keys, types, and the shape of each graph (§5.1). Each error carries file, line, column, the offending key, the expected type and, for unknown keys, the closest valid key. A graph's shape problem is `config.bad_graph` at the node.
2. **References.** Every id a value names (a person, group, topic, source, destination, forum, template, rule, task kind, option set) exists.
3. **Semantics.** Regexes compile; templates parse and reference only fields of their context, including the argv of a `run` node (§5.8); durations and windows are well-formed; scoring weights are finite; a shared destination has redaction templates for every sensitivity class the rules can assign. Two settings of one graph that ask for opposite things are `config.conflicting_settings`: a roll that assumes a meeting with no record covered its agenda, beside an ask node that asks about the same meetings (§12.8).
4. **Capabilities.** Each source's transport serves the capabilities the source uses. Every action an `ask` node offers in a graph that applies has a destination that accepts it. A `write` node's destination is private. A calendar read through a destination is a calendar adapter. The destination an ask node's covered questions roll is an agenda.

A run never starts on invalid configuration (CFG-02). Pass 4 is the only pass that needs a preset's capability declarations; it never contacts a system. `docket doctor` contacts systems (§15.4).

### 3.5 Configuration digest and partial digests

The resolved configuration serializes to canonical JSON (sorted keys, no insignificant whitespace, normalized numbers). Its SHA-256 is the configuration digest. Each verb declares which configuration sections it reads; a node's digest (§4.4) includes only the digest of those sections and the node's own settings. Editing a planning policy therefore stales the `plan` node and the nodes downstream of it, not the `judge` node that assesses (CFG-06).

### 3.6 Template language

Templates are strings with `{field}` and `{field|filter|filter:arg}` substitutions, plus three block forms: `{?field}...{/field}` (render if present and non-empty), `{!field}...{/field}` (render if absent or empty) and `{#list}...{/list}` (repeat). There are no expressions, loops beyond lists, or calls. Filters are a closed set: `upper`, `lower`, `title`, `trim`, `truncate:N`, `date:FORMAT`, `duration`, `join:SEP`, `default:TEXT`, `redact`, `slug`, `first`, `count`, `sentence` (the first sentence of a text, without its full stop). Each template site declares its context model; validation rejects a field the context does not have (CFG-05). Rendering is total: a missing optional field renders empty, never raises.

## 4. State

### 4.1 Locations

```
<state_root>/                          default ~/.local/state/docket/<workspace-slug>/
  docket.db                            workspace database
  blobs/<sha256[0:2]>/<sha256>         content-addressed raw payloads
  runs/<run-id>/
    run.json                           run header: graph, clock pin, sandbox, status, write pins
    session                            the run's session token (§5.5)
    read/<source>/records.jsonl        record index of one read (payloads in blobs)
    read/<source>/items.jsonl          mapped items (context.jsonl for a context source)
    read/<source>/outcome.json         the source's outcome and each unit's
    artifacts/<node>/<digest>.json     node outputs, named by node digest
    artifacts/<node>/latest            digest of the node's latest artifact
    tasks/<task-id>/pack.json          task input pack
    tasks/<task-id>/prompt.md          rendered prompt
    tasks/<task-id>/result.json        submitted result (accepted only)
    answers.json                       the operator's answers
    decide/resolutions.json            task references resolved for verification
    plan/<digest>/plan.json, diff.md   plans by digest
    receipts.jsonl                     write receipts, append-only
    journal.md                         rendered journal for this run
    unexpected.jsonl                   unexpected shapes recorded in this run
    sandbox/                           sandbox twins of destinations (sandbox runs)
    logs/run/<node>.log                output of a run node's command
```

The state root is outside the workspace so configuration repositories never commit state. `docket` never writes inside the workspace except through a destination that points there.

### 4.2 Workspace database

SQLite in WAL mode. Tables:

| Table | Purpose |
|---|---|
| `runs` | Run id, graph, date, status, sandbox, lease owner, timestamps |
| `cursors` | `(source, unit) -> position, coverage_end, updated_run` |
| `cursor_proposals` | Positions proven by a run, applied at the seal (§13.4) |
| `units` | Unit registry: `(source, unit_id) -> name, kind, class, tier, first_seen, last_seen, classified_by` |
| `processed_ids` | For *id-scan* sources: ids processed, by run |
| `dispositions` | `(topic_id) -> decision, run, item, until, note` (FR-APP-05) |
| `receipts` | Operation id, destination, outcome, external id, verify status, run |
| `item_ledger` | Every item put in front of the operator, with its state and placements (§13.8) |
| `unexpected` | Shape records across runs, aggregated for `docket unexpected` |
| `unexpected_accepted` | Reviewed shape acceptances recorded by `docket unexpected accept` |
| `ledgers` | Generic append-only rows for journal ledgers (coverage, surfaced, carries) |
| `leases` | Run lease rows (§5.5) |
| `task_results` | Accepted task results indexed by task input digest (§9.6) |
| `task_labels` | Labels written for task lines, with the digest of the words each was written for |
| `step_receipts` | Completed seal steps, so a repeated seal does not repeat them |
| `schema_meta` | Database schema version |

The database has one schema with one version. Docket creates a new database at that version and refuses to open any other: a newer one with `store.db_too_new`, an older one with `state.too_old`. Each error names both versions.

### 4.3 Records

A record is one raw read: the transport response body exactly as received, stored once in `blobs/`, plus an index row:

```json
{"record_id":"r_7f3c...","source":"chat","unit":"spaces/AAA",
 "query":{"tool":"...","args":{"...":"..."}},"interval":{"from":"...","to":"..."},
 "page":3,"entry":0,"outcome":"read","blob":"sha256:...","received_at":"..."}
```

Records are immutable (FR-SRC-05). Everything downstream references record ids, and verbatim evidence checks run against the decoded blob.

### 4.4 Artifacts and node digests

Every node output is an artifact named by the node's digest:

```
node_digest = sha256(canonical_json({
  "verb": verb,
  "schema": the verb's schema version,
  "config": digest(partial_config_digest(sections the verb reads), the node's settings),
  "inputs": [output digest of each node it works on],
  "answers": digest of the answers it reads (ask and approve),
  "clock": run clock pin (date and timezone; the instant only for nodes that read it),
  "extra": anything else the verb reads (the calendar and task store a plan reads,
           the questions an ask builds, whether the run is a sandbox)
}))
```

An artifact on disk whose name equals the freshly computed digest is current. Any other is stale. Nothing else tracks staleness. `artifacts/<node>/latest` names the node's most recent artifact. A judged or asked node's output is a pure function of its accepted results or answers: `docket next` computes it in memory, and the next command that changes the run writes it.

`schema` increments when a verb's code changes its output in a way that must invalidate old artifacts. A code upgrade mid-run therefore recomputes exactly the affected nodes (FR-ROB-04).

### 4.5 Reads

A read node reads its source once per run. Its digest covers the source's configuration and the run clock, not what the source holds, so a source that changes during the run leaves the node current; the next run reads the change. Each read keeps its own folder, `read/<source>/`, so reads that run at the same time share nothing. A record the read met twice counts once, as its later entry. A record the contract marks deleted (`contract.<kind>.tombstone`) removes the item it names. The read writes a cursor proposal per unit; the durable cursor moves only at the seal (§13.4).

### 4.6 Clock

A run pins its clock at creation: date, timezone and a reference instant. Nodes that need "now" (planning, freshness notes) read the pinned instant, or the instant `docket run start --now` pinned for tests. Nothing derived reads the wall clock, which makes NFR-03 testable.

## 5. Graphs and runs

### 5.1 Graphs and nodes

Everything Docket does is a run of a graph. A graph is a set of named nodes. A node does one thing, named by its verb, and says what it needs: `from` names the nodes whose output it works on, and `after` names nodes that must be done first and hand it nothing.

```yaml
graphs:
  morning:
    gather-chat:    { read: chat }
    gather-mail:    { read: mail }
    assess-chat:    { judge: assess, from: [gather-chat] }
    assess-mail:    { judge: assess, from: [gather-mail] }
    merge-items:    { merge: [assess-chat, assess-mail] }
    review-items:   { judge: review, from: [merge-items] }
    triage-items:   { ask: default, from: [review-items] }
    plan-writes:    { plan: [triage-items] }
    approve-writes: { approve: plan-writes }
    apply-writes:   { apply: approve-writes }
```

| Verb | What the node does | How its work is done | Hands on |
|---|---|---|---|
| `read: <source>` | Reads one source from its cursor (§6) | computed | items |
| `run: [argv]` | Runs a command once; its `dry_run` form in a sandbox run (§5.8) | computed | nothing |
| `judge: <task>` | Has an agent judge what it works on, one task kind per node (§9) | judged | items, or labels for the `label` task |
| `merge: [nodes]` | Combines rules, scores and judgments into what is worth showing (§7.3, §8) | computed | items |
| `ask: <option set>` | The operator decides each item it works on (§10) | asked | decisions |
| `plan: [nodes]` | Turns decisions into operations; with `schedule_tasks` also places open tasks on the calendar (§10.3, §12) | computed | a plan |
| `approve: <plan>` | The operator approves the plan, bound to its digest (§13.1) | asked | an approval |
| `apply: <approve>` | Writes the approved operations (§13.3) | computed | nothing |
| `write: <action>` | Plans and writes operations that need no approval; only to a private destination (§12.8) | computed | nothing |

A node is ready when every node it names is current. Nodes that name nothing are ready together, and nodes that do not depend on each other never wait for each other: mail is assessed while chat is still being read. A node that waits (on an agent, on the operator, on a source that is unavailable) holds back only the nodes downstream of it.

Every graph ends with a seal, which is not written in it. The seal is ready when every node is current. Sealing advances cursors, writes dispositions and the item ledger, and makes the journal entry (§13.4); then the run is sealed and cannot change. A sandbox run seals without moving a cursor or writing dispositions and ledgers.

The shape of a graph is checked when the workspace loads (§3.4). A graph has at least one node. Node names are lower-case words (`[a-z][a-z0-9-]*`, at most 40 characters) other than `seal`. Every name a node uses is a node of the graph, and there is no cycle. Each node works on the kind of thing its inputs hand on: a `merge`, an `ask` or a `write` on items, a `judge` on items or decisions, a `plan` on decisions or items (the decisions of at most one `ask` node), an `approve` on a plan, an `apply` on an approval. `merge`, `plan`, `approve` and `apply` name their inputs in the verb and take no `from`. `read` and `run` work on nothing another node hands on and take only `after`. The node an ask node's covered questions take their meetings from is a `read` node. A shape problem is `config.bad_graph`, reported at the node with its file and line.

A node's settings sit beside its verb: a `read` may have a `schedule` (§5.7), a `run` its `dry_run`, `env` and `timeout_s` (§5.8), an `ask` its `questions` (§10.1), a `plan` `schedule_tasks`, a `write` its destination `to`, and a `write: roll` its `no_record`, `trailer` and `commitments` (§12.8). An ask node's `questions.covered` names the read node of the meetings it asks about, the agenda destination, and the suffix of the heading (§10.4). Naming that read node makes the ask node wait for it, as `after` does. Any node may carry a `description`, which `docket graph show` prints.

Where several nodes hand on to one, their outputs are joined: items by id (a later node adds to what an earlier one said of the same item), lists end to end, maps key by key.

### 5.2 `docket next`

`docket next` returns every step that is ready now, computed from state. It writes nothing but the lease heartbeat. Each step names its node; a `lease_held` wait, which concerns the whole run, names none (PRD §6.4, AE-01):

| Step | Meaning | Fields |
|---|---|---|
| `run` | Run this command: `docket do <node>`, `docket task issue`, `docket ask resolve` or `docket seal` | `command` (argv array and shell string), `why`, `expect` (what the command will produce), `timeout_s` |
| `dispatch` | These agent tasks need results | `tasks[]`, each with `task_id`, `kind`, `pack_path`, `prompt_path`, `result_path`, `result_schema` and `submit` (exact argv); `concurrency`, `why` |
| `ask` | Put these questions to the operator and record each answer | `questions[]`, one `docket ask answer {question} {option} ...` template, one `show` template, `burst`, `why` |
| `wait` | Nothing can be done for this node until something outside changes | `reason` (code), `instructions`, `ready_when`, and the command to run then |

`wait` reasons are `source_unavailable` (a required source could not be read; §6.8), `apply_partial` (operations failed; `docket do <node> --again` repeats the ones that did not complete), `plan_rejected` (the operator rejected the plan), `post_apply_change` (§5.4) and `lease_held` (§5.5).

The driver does the ready steps together and asks again. The loop is the same whatever the graph: the agent needs no knowledge of the graph, only of the four kinds of step. Beside the steps, `nodes` gives each node's verb, how its work is done, its state (`current`, `stale`, `pending`, `waiting` or `blocked`), the reason and the nodes it needs. When the run is sealed, `done` is true and `summary` describes the run. With no run open, `done` is true, `steps` is empty and `start` carries the command that starts one.

Every command in a step is concrete except the compact ask step's documented `{question}` and `{option}` answer template (AE-07). `docket next --full --json` expands ask details and gives each option its exact answer command. Dispatch steps always carry real run ids, task ids and paths. `docket next --json` output for a dispatch of 40 tasks stays small because prompts and packs are paths, not contents (AE-08).

### 5.3 Commands that do a node's work

Docket does a computed node's work only when the agent runs its command, so the agent's transcript shows each effect. `docket do <node>` does the work of a `read`, `run`, `merge`, `plan`, `apply` or `write` node and returns the steps that are ready next. It refuses a node that is not ready with `node.not_ready`, naming the nodes it waits for, and a judged or asked node with `node.not_computed`. `--again` repeats a node held on a failure that may pass: a required source that was unavailable, or operations that failed.

`docket task issue` writes the pack and prompt of each task a ready `judge` node needs; `docket do` and `docket task submit` also write them for any judge node they make ready. Until they exist, `docket next` returns a `run` step for `docket task issue`; then it returns the `dispatch` step that points at those files. `docket task submit` gives a judged node a result, `docket ask answer` gives an asked node an answer, and `docket seal` seals the run. Every command that moves a run returns the steps ready next in `data.steps`. A command for a node that is already current is a no-op that reports `current` (AE-06).

### 5.4 Staleness, recomputation and the apply boundary

A node's digest is a function of its own settings, the configuration sections it reads, the output digests of the nodes it works on, the answers it reads and the run clock (§4.4). A node named only in `after` orders it and does not enter its digest. An artifact whose file name equals the fresh digest is current; any other is stale. Nothing else tracks staleness, so changing an answer, a rule or a source's records stales exactly the nodes downstream of the change. A computed node is recomputed when its command runs. A `judge` node reuses accepted results whose task input digest is unchanged (§9.6) and issues tasks only for the rest. An `ask` node keeps answers bound to unchanged item identities and re-asks the others, each with its previous answer (FR-DEC-06).

A run never goes back across a write. Each writing node (`apply`, `write`) has its own boundary. Before its first operation it pins the digest of every node upstream of it, in `run.json` under `applied`. From then on those nodes are read from their pinned artifacts: a later change upstream does not stale the plan, and the node's own writes to a task store, an agenda or a calendar that an upstream node read cannot push the run backward. The answers of a pinned `ask` or `approve` node cannot change (`node.post_apply_change`). Nodes that are not upstream of a write are unaffected by it.

A pinned node whose configuration or code schema has since changed has drifted. `docket next` then returns `wait` with reason `post_apply_change`, the commands to inspect the change, and `docket run continue`, which finishes the run on what was written; the change applies to the next run.

### 5.5 Leases

A run is driven by one session at a time. A run lease is a row `(run_id, owner, pid, host, acquired_at, heartbeat_at)`. `docket run start` takes it, every mutating command acquires or renews it before its first write, and `docket next` renews the heartbeat of a lease this session holds. An owner is *dead* when its pid is gone on the same host, or its heartbeat is older than the configured timeout (default 15 minutes). A dead lease is adopted automatically and the adoption is reported. A live foreign lease fails a mutating command with `lease.held`, naming the owner and the steal command `docket run steal <run-id>`, and makes `docket next` return `wait` with reason `lease_held` and the same command (FR-ROB-05). The owner identity is `DOCKET_SESSION` when set, otherwise the run's session token. Docket writes that token to the run directory on first use, so separate shell calls share it without an environment variable (AE-05).

### 5.6 Runs

`docket run start <graph> [--date D] [--now T] [--sandbox] [--new]` creates a run of a graph and pins its clock (§4.6). The run id is the date and the graph name, `2026-10-16-morning`, with a number added for a second run of the day. Repeating the command for the same graph, date and sandbox mode returns the existing run unless `--new` is given. An unknown graph is `input.unknown_graph`, with the closest name.

`--sandbox` binds every destination to its sandbox twin under the run directory for the whole run, so its writes stay inside the run. A sandbox run may read live sources. Its seal moves no durable cursor and writes no dispositions or ledgers, and each `run` node uses its `dry_run` form or is skipped.

`docket run status` shows a run's header, each node's state, the lease and the ready steps. `docket run list` lists runs, newest first. `docket run abandon` ends a run without advancing cursors, so the next run reads the same records again. `docket graph show <graph>` draws a graph and `docket run status --graph` draws a run on its graph (§5.9).

### 5.7 Read schedules

A `read` node reads its source on every run unless it has a `schedule`: `weekdays` (read only on those days) or `every_days` (read at most that often, counted from the source's last read). A source off its schedule reports `skipped` with the reason. A skipped day is never a gap, because the source's cursor does not move. So one graph can read chat and mail daily and a health report weekly, and another graph can read the same sources on its own terms.

### 5.8 Run nodes

A `run` node runs a program once. `run` is its argv; `dry_run` is the argv a sandbox run uses instead. Each argument is a template over `{run.id}`, `{run.date}`, `{run.graph}`, `{run.dir}`, `{run.sandbox}`, `{node}` and `{workspace}`, checked at validation (`config.bad_template`). The program runs in the workspace directory, in its own process group, with stdin closed and a scrubbed environment: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR` and `TZ`, the names listed in `env`, and `DOCKET_NONINTERACTIVE=1`, `DOCKET_RUN_ID`, `DOCKET_RUN_DIR`, `DOCKET_NODE` and `DOCKET_WORKSPACE`. Its output goes to `logs/run/<node>.log` in the run directory. A non-zero exit, a timeout (`timeout_s`, default 120 seconds) or a program that cannot start fails the node with `node.command_failed`, which is retryable and carries the argv, the exit status and the log path. `after` places a run node among the others: `notes: { run: [bin/commit-notes], after: [apply-writes] }` runs once the writes are done.

### 5.9 Drawing a graph

`docket graph show <graph>` draws a graph left to right. A node's column is the longest chain of nodes before it, so everything it waits for stands to its left. A node takes the row of the first node it waits for when that cell is free, so a chain reads along one line; otherwise it takes the first free row of its column. The seal is drawn last, one column past the deepest node, waiting for every node no other node waits for.

Each node is two lines: its name, and its verb in brackets that say who does its work: `[read]` docket computes, `{judge}` an agent judges, `(ask)` the operator is asked. An edge between neighbours on one row is a line between them. A solid line `──▶` hands on data: `from`, the verb's own inputs (`merge`, `plan`, `approve`, `apply`), or the read node an ask node's covered questions take meetings from. A dotted line `┄┄▷` only orders (`after`, and the seal). Every other edge is listed under the drawing by the node it enters, and that node carries `+N`. Names longer than 16 characters are clipped. Columns that do not fit the width (`--width`, default 80) wrap into bands, and a row that goes on in the next band ends in `»`:

```
      5              6              7              8
  »─▶ merge ───────▶ review ──────▶ ask ─────────▶ label ─────»
      [merge] +3     {judge}        (ask) +2       {judge}

edges not drawn in the rows:
  merge ◀── judge-b, judge-c, judge-d
  ask   ◀┄┄ write-a, prep
```

Listing the joins between rows, rather than drawing them, keeps a graph of 20 nodes in 12 columns readable in 80 characters: one line per row and band, no crossing lines, and every edge stated once. `--ascii` draws the same with plain ASCII. `--list` prints the flat list: each node, its verb, who does it and what it waits for. `--format mermaid` prints a Mermaid flowchart with data edges solid, order edges dotted and one class per worker (`docket`, `agent`, `operator`). The JSON output gives each node its verb, who does it, `from`, `after`, `description`, `column` and `row`, and the seal's `column`, `row` and `after`.

`--run [<id>]` draws a run on its graph; with no id it takes the most recent unsealed run. A run of another graph is `run.graph_mismatch`. Each node is marked done (`✓`, current), ready now (`●`: everything it needs is done and its work can be done now, by docket, an agent or the operator), waiting (`○`, on the nodes before it that are not done) or blocked (`✗`: an unavailable source, failed operations, a rejected plan or a change past the apply boundary). A legend line names the marks, and lines under the drawing name what is ready, what each waiting node waits on, and why each blocked node is held. In JSON each node adds `mark`, `state`, `reason` and `waiting_on`. `docket run status --graph` prints the same drawing.

## 6. Sources

### 6.1 Source model

A source instance binds a transport, a cursor model, a request plan, a response contract and a record-to-item mapping:

```yaml
sources:
  chat:
    extends: preset:mcp/chat-codemode
    transport: { server: chat-gateway }          # transport instance, see §6.3
    cursor: { model: units, unit_kind: space, lookback: 3d }
    auth: { check: ["tokencheck", "--server", "chat-gateway"], refresh: ["tokenrefresh"] }
    budget: { max_pages: 400, max_seconds: 180, concurrency: 6 }
```

The preset supplies the request plan (tool names, argument templates, pagination fields), the response contract and the default mapping. The workspace supplies the instance, auth commands, budgets and overrides (FR-SRC-01, FR-SRC-04).

### 6.2 Source interface

```python
class Source(Protocol):
    id: str
    capabilities: set[Literal["read", "discover", "resolve", "context"]]
    async def discover(self, ctx) -> list[Unit]: ...                 # units model only
    async def read(self, ctx, unit: Unit | None, cursor: Cursor) -> AsyncIterator[Page]: ...
    async def resolve(self, ctx, ref: str) -> Resolution | None: ...  # FR-SRC-10
    def check_auth(self, ctx) -> AuthStatus: ...
```

Most sources use the generic `DeclarativeSource`, which interprets the request plan against a transport. A source that configuration cannot express is a plugin (§17).

### 6.3 Transports

A transport sends one request and returns one response body plus metadata. Transports know nothing about records, cursors or items.

| Transport | Request | Notes |
|---|---|---|
| `mcp_stdio` | Tool name and arguments | Spawns the server once per read, reuses the session. |
| `mcp_http` | Tool name and arguments | Streamable HTTP; bearer token read from a file or an env var name on every request, never from configuration text. |
| `codemode` | Script template and parameters | For servers that expose one code-execution tool. The preset ships a script that pages, projects to declared fields and returns compact JSON under the server's response cap. |
| `exec_jsonl` | One JSON object on stdin | A long-lived child process speaking the Docket protocol (§17.2). One process per read, not per item. |
| `http_json` | Method, URL template, headers, body | For plain JSON APIs. |
| `files` | Glob and parser | Local files: Markdown, JSON, ICS, CSV. |

Transport instances are declared once under `transports:` and shared by sources (`chat-gateway`, `docs-gateway`). Credentials are referenced by environment variable name or file path. A file reference may add `json_path`, a list of keys and list positions, to take one string from inside a JSON file such as another tool's token store; the path is a list so a key may hold any character. A reference may also name a `refresh` command for a credential that expires. Credential headers are read again on every request, so a value another program renewed is picked up. When a server answers 401, the transport runs the command once, with no terminal, a closed stdin, a scrubbed environment and a timeout, reads the credential again and repeats the request; concurrent rejections share one run. A rejection that survives is reported as `unavailable` with `authentication rejected (HTTP 401)` and the reason the refresh did not help. The loader rejects a literal that looks like a secret (high-entropy strings in credential-named fields).

### 6.4 Cursor models

| Model | Read | Coverage proof |
|---|---|---|
| `snapshot` | Everything the source holds, on each read | Complete when paging reached the end within budget. |
| `incremental` | Records with ordering key greater than the cursor, up to the run's interval end | Complete when paging reached the end and the last page's ordering key is at or past the interval end, or the source reports no more. |
| `units` | Discover units, then `incremental` per unit | Per unit. A unit with a gap holds its own cursor; others advance. |
| `id-scan` | List ids, read those not in `processed_ids` | Each id processed individually. |
| `cadence` | Any of the above, only on configured weekdays or every N days | A skipped day reports `skipped`, never a gap. |

The ordering key, page token field and "no more" signal are contract fields in the preset, so a server that pages by token, by offset or by time window is configuration (FR-SRC-02).

Coverage is computed from what was read, never from counts the server claims. The rule `returned == read` that failed in the predecessor does not exist here: a page with fewer rows than requested is complete if the contract's end-of-data signal says so, and otherwise the next page is requested. A unit read and found empty has no position, but the read proved coverage to the run's instant. That coverage is kept as a cursor with no position, so the next run starts where this one ended, where it would otherwise start again from the lookback and miss anything older, and so the quiet-unit rule (§6.9) can tell the unit was read. Coverage only moves forward and never erases a position.

### 6.5 Paging and step-down

The pager requests `page_size` rows. On a truncation signal (a contract-declared marker, a response over the transport's size cap, a transport error the contract classifies as `too_large`, or a body that is not JSON and ends with text matching a `too_large` pattern, which is how a gateway with a response cap reports a cut body), it halves `page_size` and retries the same position, down to `min_page_size`. Below that, the unit is `partial` with its retry position. Budgets (`max_pages`, `max_seconds`) end the read as `partial`, never as `failed` (FR-SRC-08).

### 6.6 Outcomes and isolation

Each source, and each unit, ends with one outcome: `read`, `empty`, `partial(position)`, `failed(cause)`, `unavailable(reason, instructions)` or `skipped(policy)`. A read makes at most 8 requests at a time. `transports.<id>.concurrency` limits its requests through a transport, and `sources.<id>.budget.concurrency` limits work within the source. An exception in one source is caught at its boundary, recorded with its raw message preserved, and never cancels another (FR-SRC-06). Errors keep the original message and status; Docket never replaces a transport error with a generic one. A request that fails for a passing reason is repeated: `sources.<id>.retry` sets `attempts` (default 2), `wait_s` (default 1, doubling) and `transient`, regexes over the failure message. A failure the transport marks retryable (a timeout, a 5xx, a 429, a dropped connection) is transient without a pattern. A truncation, a protocol error and an authentication rejection are never repeated. The outcome counts the repeats as `retries`, and when they run out the last failure stands with its own message. An MCP session whose call raises instead of answering is reopened once and the call repeated; concurrent calls that failed on the same session share the reopen. Discovery steps down from a cut page the way reading does, halving `discover.page_size`.

### 6.7 Response contracts

A contract declares, per response kind, the fields read and their types, which are optional, and the safe reading for each variation:

```yaml
contract:
  message:
    required: { id: str, text: str, createTime: timestamp }
    optional: { sender.displayName: str, thread.name: str, annotations: list }
    unknown_fields: ignore
    variations:
      sender.displayName: { missing: { reading: "use sender.name", record: false } }
      space.type: { unknown: { reading: "treat as group space", record: true,
                               extend: "contract.space.type values" } }
```

The contract checker runs on every page. A missing optional field is not drift. A missing required field fails that record only, records an unexpected shape, and the unit continues. A type change on a required field fails the unit with `source.contract.type_changed` and the sample, and other units continue (FR-SRC-07).

### 6.8 Authentication

`check_auth` runs the configured check command or token probe with no TTY, a closed stdin and a timeout (default 10 s). Docket never runs a command that can open a browser or prompt; `doctor` flags a check or refresh command that is interactive by running it with `DOCKET_NONINTERACTIVE=1` and a closed stdin and treating a timeout as interactive. An auth failure makes the source `unavailable` with the instructions from configuration, and the run continues (FR-SRC-12). Only when `sources.<id>.required` is true does the read node wait: `docket next` returns `wait` with reason `source_unavailable` on that node and `docket do <node> --again`, and only the nodes downstream of it wait with it.

### 6.9 Unit registry and classification

For `units` sources, discovered units are upserted into the `units` table. Classification rules in `signals.units` assign `kind`, `class` and `tier` from name patterns, membership counts and unit type. A rule may also match on the profile's vocabulary: `mentions: { topics: owned }` holds when the unit's name mentions an alias of an owned topic, and `mentions: { entities: strategic }` when it names a strategic entity (`any`, `protected` and explicit ids work the same way), so adding a topic or an entity to the profile classifies its units with no new rule. `discover.types` keeps only units of the listed types, so two sources can share one system, for example rooms in one and direct conversations in another, each with its own link template, budget and rules. `docket units list` shows the durable registry and its classifications. The registry keeps each unit's type as its system reports it beside the kind a rule may assign, so a rule that matches on type still matches after another rule renamed the kind. `docket units preview` applies the current rules to the registry and writes nothing: counts per class and tier, every unit whose classification would change, and with `--class none` the units no rule matches, which is how a rule is tuned without reading the source again and how a new kind of unit gets noticed. The registry follows the system's membership. Each complete discovery marks every listed unit current; a unit of that source it no longer lists is marked gone (`gone_at`) and is left out of `units list` and `units preview` unless `--gone` is given, and one listed again has returned. The first discovery of a source is a baseline. After it, the read's outcome `membership` reports the units that are new, gone and returned, by count and by name, and names every unit that produced records in the run and has no class (`unclassified_active`), so a new kind of unit is noticed in the run that meets it. `docket units set <source> <unit> --class ... --tier ...` records the operator's classification of one unit, which no rule overrides (`classified_by: operator`); `docket units unset` gives it back to the rules. A source in thousands of units reads only the ones that moved. When discovery maps a unit's last activity (`discover.active`), a unit with no activity after the place its cursor starts is not read, provided it was really read within `discover.verify_every` (default 7 days). Past that window it is read anyway, and a record found in a unit the rule would have skipped is recorded as an unexpected shape, because the activity time was wrong. A unit never read is read once so that a cursor exists. The outcome counts the units not read as `quiet_units`. A sample (`source test --limit`) reads the most recently active units one at a time and stops when it has enough. The `classify` task kind ships, but automatically issuing it for unclassified units is deferred with FR-SRC-11.

### 6.10 Resolve

`resolve(ref)` maps a URL or key from a task line to the source that owns it (by the source's declared `resolve.patterns`) and returns `state`, `last_activity`, `last_actor` and `title`. Task verification (§10.5) uses it. A ref no source claims is reported as `unresolvable`, not as an error.

### 6.11 Context records

A source with the `context` capability emits records whose mapping targets `context` instead of `item`. Context records feed journal templates and never become items (FR-SRC-09).

## 7. Items

### 7.1 Mapping

A source's mapping turns a record into zero or more items with JSONPath-like field paths and template expressions:

```yaml
mapping:
  item:
    kind: message
    title: "{text|truncate:120}"
    text: "$.text"
    from: { handle: "$.sender.name", display: "$.sender.displayName" }
    at: "$.createTime"
    where: { unit: "$.space.name", thread: "$.thread.name" }
    url: "https://chat.example/{unit}/{thread}"   # preset fills the host pattern
    evidence: "$.text"
    fields: { priority: "$.fields.priority" }
```

`from.handle` resolves to a profile person through `handles`, which gives the item `from.person` and `from.relation` (one of the relationship sets). An unknown handle has relation `unknown`, which is a valid value, not an error.

### 7.2 Identity

- **Item id**: `sha256(source, unit, record identity)[:16]`, stable across runs.
- **Thread id**: the source's thread key, or the item id for unthreaded records.
- **Conversation**: some systems give every message a thread of its own, so a direct or group conversation has no threads and yet holds exchanges. `signals.conversations` names such units with a unit predicate and a `gap` (default 4h); the items of a unit between two silences longer than the gap share a conversation key. Merge treats the key as the items' topic, ahead of the thread, and a task pack treats it as their thread, so one exchange is one finding and reaches a task whole.
- **Topic id**: the merge key across sources. Computed from, in order of preference, an explicit reference the item carries (a ticket key, an incident id, a document URL), a thread id, or an agent-assigned topic from the `assess` task. References are extracted by `signals.references` patterns from configuration.

### 7.3 Merge

A `merge` node groups items by topic id, keeps the strongest assessment, unions evidence and sources, and records each merge in the item's trace. Suppression runs here: a topic with a live disposition (`dismiss` until a date, `watch` with no new activity, `done`) is suppressed with the disposition as its reason; a topic whose latest record is from the operator is marked `answered`, unless the assess task kept one of the operator's own items as a commitment, because a reply that promises something leaves the topic open; a topic already present in the task store (matched by its references) is marked `tracked` and routed to task verification rather than a finding (FR-SIG-07).

## 8. Signals

### 8.1 Rule language

A rule has an id, a `when` (predicates combined with `all`, `any`, `not`) and a `then` (effects):

```yaml
signals:
  rules:
    - id: chain-on-owned-topic
      when:
        all:
          - from: { relation: chain }
          - mentions: { topics: { owned: true } }
      then:
        importance: +30
        surface: always
        reason: "Management chain on an owned topic"
    - id: dm-from-report
      when: { all: [ { unit: { kind: direct } }, { from: { relation: reports } } ] }
      then: { importance: +20, urgency: +10 }
    - id: protected-entity
      when: { mentions: { entities: { protected: true } } }
      then: { sensitivity: customer }
    - id: bot-rooms
      when: { unit: { class: bots } }
      then: { dismiss: "Automated room" }
```

Predicates are a closed set (FR-SIG-02):

| Predicate | Matches on |
|---|---|
| `source` | Source id or kind |
| `unit` | Unit kind, class, tier, id |
| `from` | `relation`, `person`, `group`, `domain`, `is_me` |
| `to` | Operator position: `direct`, `cc`, `mentioned`, `member` |
| `mentions` | Operator, people, groups, `topics` (by id or attribute), `entities` (by id or attribute) |
| `text` | Regex or phrase list over title and text, case options |
| `field` | A mapped field: equals, in, regex, range |
| `age` | Older or newer than a duration |
| `unanswered` | No reply from the operator after the item, in its thread or, where the unit has no threads, its exchange, for a duration |
| `thread` | Started by the operator, operator replied, reply count |
| `reference` | Item carries a reference of a named pattern |
| `plugin` | A named predicate from a plugin (the escape hatch) |

Effects are a closed set: `importance`, `urgency` (signed integer contributions), `surface: always`, `route` (a destination or option hint), `area`, `topic`, `sensitivity` (a configured class), `deadline` (extract with a date pattern or field), `tags`, `dismiss` (with reason), `assess: rules_only` (FR-SIG-06), and `reason` (text for `item why` and the question).

Rules evaluate in file order; every rule that matches applies its effects. `dismiss` and `surface: always` conflict resolution is explicit: `surface: always` wins and the trace records the overridden dismissal.

### 8.2 Scoring

```yaml
signals:
  scoring:
    formula: weighted          # weighted | max | plugin
    weights: { importance: 0.3, urgency: 0.4, agent: 0.3 }
    floor: 55
    always_keep: 80
    caps: { per_source: { default: 40, mail: 25 } }
    budget: { questions: 15 }
```

`score = sum(weight_k * component_k)` with components clamped to 0..100. `importance` and `urgency` are the rule contributions on a base of 50; `agent` is the assess task's score. Items below `floor` drop unless `surface: always`; items at or above `always_keep` bypass caps; per-source caps keep the top N by score. All numbers are configuration (FR-SIG-04, FR-SIG-05). `decision_floors` gives the items recommended for a decision a floor of their own: an item only to be monitored asks less of the operator than one to act on, and may be kept at a lower score. The reference workspace sets the predecessor's values.

### 8.3 Coverage cross-references

A rule effect `expect_in: <source>` with a reference pattern records that the item names an identifier the home source should contain. A `merge` node checks each expected identifier against the home source's items and adds a coverage note to the journal for every miss, with the naming item (FR-SIG-08). It does not mark the home source incomplete.

### 8.4 Traces and `item why`

Every node that touches an item appends to its trace, each entry naming its verb: mapping (record ids), rule matches (rule id and effects), score components, cap decisions, merges, suppressions, review verdict, question id, answer and operations. `docket item why <id|url|text>` finds the item by id, URL or a unique text match across the run and prints the trace with rule ids and file positions (FR-SIG-09).

## 9. Agent tasks

### 9.1 Task kinds

A task kind is a named triple of input model, output model and instructions. Core kinds:

| Kind | Input | Output |
|---|---|---|
| `assess` | A batch of items with evidence, unit context, the operator's profile summary and the vocabulary | Per item: `keep`, `score` 0..100, `kind` (request, commitment, decision, risk, fyi, or a configured value), `owner`, `ask` (one-line imperative), `evidence` (verbatim quotes), `deadline`, `topic` |
| `extract` | A document (for example a transcript) split into sections | Commitments and requests with owner, verbatim evidence and deadline |
| `review` | Candidates without scores, with evidence and the decision first recommended | Per item: `verdict` (ask, merge, drop, needs_verification), `against` (strongest argument against asking), `merge_with`, `reason`, and `decision` when the reviewer recommends a different one, which then stands as the recommendation |
| `recap` | Meetings held, each with its record and its forum's agenda | Per meeting: `covered` (notes, each naming the agenda entry it settles), `actions` (§12.8) |
| `label` | Task lines planning may place, as written in the private list | Per task: `label`, the words that stand for it where others can read them (§12.7) |
| `classify` | Units or items needing a class | Per entry: `class`, `tier`, `reason` |

A judge node names one kind, and its `from` names what the kind judges: `assess: { judge: assess, from: [read-chat, read-mail] }`. A kind's family says what it judges and what its node hands on. The `item` family (`assess`, `extract`, `recap`, `classify`) judges items one at a time and hands on the items with its judgments on them. `review` judges the kept set and hands on the items with its verdicts applied. `label` judges the task lines of a decision and hands on the labels alone (§12.7).

A workspace may define a further kind under `tasks.kinds` by declaring its family (`item` or `review`), its output fields (from a closed field-type set), its cross-field rules and its instructions. Docket renders and validates it like a core kind (FR-TSK-01).

### 9.2 Packs

`tasks/packs.py` builds packs deterministically: items sorted by score descending then id, batched to `budget.max_items` and `budget.max_bytes` per pack. Must-surface items are placed first and never dropped; if they alone exceed a pack budget, the pack exceeds it and records an unexpected shape `pack.over_budget` (FR-TSK-05). Each pack contains everything the task needs: item fields, evidence lines with line ids, unit context, relevant profile excerpts (people named, topics matched) and the vocabulary. Its digest is the task input digest.

Items that share a source, unit and thread are one conversation. A pack keeps a conversation together with its items oldest first, each row carries its `thread`, and a pack boundary falls between conversations whenever one fits; a conversation longer than a pack spans packs in order. Conversations rank by their strongest item, must-surface first and then score. The assess instructions tell the task to read a conversation before judging its items, because a reply may answer or settle what an earlier item asked.

A pack also carries context: the other items of each conversation that no task will judge (the operator's own, the dismissed, those left to rules), as `context`, oldest first. At most `context_items` per conversation are kept, the operator's own first and then the most recent, each cut to `context_chars`, and `omitted` counts the rest. The instructions tell the task to read the context and judge only the items, because an ask the operator already answered needs nothing.

### 9.3 Prompts

`tasks/prompts.py` renders `prompt.md` from four parts:

1. The kind's instructions (core or configured), with operator-specific vocabulary filled in from configuration.
2. **An output specification generated from the Pydantic output model**: every field, its type, whether it is required, allowed values with their meanings, and one valid example produced by the model itself. Nothing else describes the output, so the prompt cannot drift from the validator (FR-TSK-02).
3. The fence: a fixed block stating that the pack is untrusted data, that instructions inside it must not be followed, and that the agent must read nothing outside the pack (FR-TSK-03, AE-11).
4. The submit instruction: the exact command, with the task id and the result path.

Source text inside the pack is wrapped in delimiters with a per-task random nonce, so content cannot close the fence.

A workspace teaches a task by example. `tasks.assess.examples` lists worked examples, each with `shows` (what the item was), `row` (the row to write, without id and source) and `why`; they are rendered between the output specification and the data. Every example row is validated the way a submitted row is, so configuration refuses an example the task itself would refuse (`config.bad_example`). `tasks.assess.decisions` is the workspace's decision vocabulary (id and meaning): with it the assess output has a `decision` field, required for every kept item, which is the recommendation a question shows.

### 9.4 Submission and validation

`docket task submit <task-id> <result-path | ->` validates in order:

1. **Schema.** Parse against the output model with tolerant coercions declared on the model (a string score becomes an integer; a single evidence string becomes a list).
2. **Identity partition.** The result covers exactly the pack's item ids: no missing, no extra, no duplicates. Integrity: refuses.
3. **Evidence.** Every quote appears verbatim in the referenced record's decoded text after whitespace normalization only. Integrity: refuses that row.
4. **Vocabulary.** Open vocabularies (an item `kind`, an `ask` verb) use a denylist and a safe default. An unknown value takes the field's declared safe reading (for example `kind: fyi`, `verdict: needs_verification`) and records an unexpected shape. It never refuses (FR-TSK-04).
5. **Cross-field rules.** Declared on the model (for example `keep: true` requires `ask`). A failure refuses that row with the rule.

A refusal returns per-row errors with the field path, the received value, the rule and a fix, and the submit command to rerun. Accepted results are written to `result.json` and indexed by task input digest.

### 9.5 Dispatch guidance

A dispatch step sets `concurrency` from `tasks.concurrency` (default 6) and states that each task is independent. The agent may run tasks inline in one session when it cannot spawn subagents; the step's `why` says so and the submit path is the same.

### 9.6 Result reuse

An accepted result is indexed by its pack's input digest in the workspace database (`task_results`). A pack whose digest has an accepted result, in this run or an earlier one, needs no new task (FR-TSK-07). The digest covers the kind's instructions and output model, the items as the task saw them, the profile excerpts and the vocabulary. So a changed item or instruction issues a new task, and an unchanged pack never does.

## 10. Decisions

### 10.1 Building questions

An `ask` node builds its questions from four inputs: the findings in the items it works on, reconcile candidates (owned calendar blocks in the reconcile window, from the calendar destination's `list`), the agendas of meetings that left no record, and task-verification results. The node's `questions.kinds` says which it asks (`reconcile`, `covered`, `finding`, `verify`; default: `finding`); an unknown kind is refused. Each question has a stable id derived from its subject (`q_` + item id, `r_` + block key, `c_` + a digest of forum, meeting date and agenda entry, or `v_` + task key), a kind (`finding`, `reconcile`, `covered` or `verify`), a rendered prompt from templates, the evidence summary, and options. The approve question is the one exception: its id is the approve node's name (§13.1).

Kinds are asked in the order of `decisions.order` (default: reconcile, covered, then findings by score, then verify). The node's `questions.budget` caps findings; the items past it become the journal section "Not asked" with their traces.

The question budget ranks findings by score. A must-surface finding is lifted only to the floor, so ranking alone cuts it first. The ask node's `questions.must_surface` decides: `ranked` (the default) leaves it to its score, `first` asks must-surface findings before the rest within the budget, and `bypass` asks them first and spends none of the budget on them. Items recommended for a batch option are ranked and cut in a group of their own against the node's `questions.batch_budget`, so a list to acknowledge and the questions asked one by one do not crowd each other out.

### 10.2 Options

An option set is configuration, and an ask node names the one it offers (`ask: people-manager-default`; `ask: default` is the core set):

```yaml
decisions:
  option_sets:
    people-manager-default:
      finding:
        - { id: task,   action: task.add,     label: "Add to backlog ({due|date:%a})" }
        - { id: agenda, action: agenda.add,   label: "Raise at {forum.name}", when: has_forum }
        - { id: block,  action: calendar.place, label: "Block {est|duration} {when|default:this week}" }
        - { id: draft,  action: message.draft, label: "Draft a reply", when: has_reply_channel }
        - { id: now,    action: now,          label: "Handle now" }
        - { id: done,   action: done,         label: "Already handled" }
        - { id: watch,  action: watch,        label: "Watch" }
        - { id: drop,   action: dismiss,      label: "Dismiss" }
```

Each option is a typed action proposal with derived defaults (FR-DEC-02): due from the item's deadline or the planning policy's default lead time; estimate from the assess task or the size default; area and forum from topic and sender. `when` guards are a closed set (`has_forum`, `has_reply_channel`, `has_deadline`, `has_calendar`, `source_is`, `relation_is`). Before offering an option, Docket asks the destination adapter `can_accept(action)`; a refusal removes the option and adds its reason to the question (FR-DEC-03), for example "The Platform agenda's Next section is full (5 of 5)".

An option may fix fields of its action with `set`, so two options can share one action and differ in where it lands: a task option with `set: { tier: priority }` files under the priority heading. A finding's defaults are derived, never asked for. The area is a rule's word, else the area of the topic the item's unit is named after, else the area the item's own text points to when it points one way. The estimate is the one the assess task gave (`est`, optional in the assess output), else the configured default. A source's `label` is what templates call one of its records (`source_label`).

Every question recommends a decision for its item: the option whose id the assess task chose, when it is on offer; for an item the task did not keep and a must-surface rule kept anyway, `decisions.recommend.surfaced`; else `decisions.recommend.default`. An option marked `batch: true` is one whose items are shown together, so the operator accepts a group at once and opens only the items they want in detail; the ask step lists a question's `recommended` option and its `batch` options. `ack` is the acknowledge action: seen, nothing owed, and like `watch` it holds only until something new happens on the topic.

**An item's forum** is where it would be raised, and is the default of an `agenda.add` option (`--forum` overrides it). In order: a forum with the person who owns the item (`forums[].person` is the assessment's `owner`), else with the person who sent it; else the forum of a group the sender is in, of a mentioned group, or of the topic the item is about (`topics[].forum`). A meeting with the owner comes first because that is who can act on it.

**Extra words.** `tasks.assess.extra` names further optional fields of the assessment (name: what it should say), for destinations that want an item said another way than its ask, such as a topic for a meeting. Templates read them as `{extra.<name>}`.

### 10.3 Answers

`docket ask answer <question-id> <option> [overrides]` records an answer:

```
docket ask answer q_9a1c task --due monday --est 60 --merge-with q_77b2 --note "after the review"
docket ask answer q_41d0 block --when "thu 13:00-17:00" --title "Draft pricing memo"
docket ask answer r_5f20 partly --est 30
docket ask answer --file answers.json
```

Overrides (FR-DEC-04): `--due` (date or weekday name, next occurrence after the run date), `--est` (duration), `--when` (window: a day, a day with times, a named calendar event to schedule over, or `this week`), `--title`, `--action` (task text), `--link` (repeatable), `--area`, `--forum`, `--merge-with`, `--note`. Option resolution accepts the id, the label, its leading words, the position, or a destination name, case-insensitively; an ambiguous match fails with the candidates (AE-09). The command echoes the normalized answer and the resulting action preview, so the operator's words and the stored decision can be compared before anything is built.

Answers are stored keyed by question id, which derives from item identity (FR-DEC-06). Each answer also records the digest of its question's subject. When an ask node's questions are computed again, as after a rule edit upstream, an answer whose subject is unchanged is kept; the answer of a changed item moves to `previous`, and the question is asked again with it shown.

### 10.4 Reconcile

Reconcile options are fixed in kind and configurable in label: `done`, `partly` (requires `--est` for the remainder), `not_done`, `to_agenda`, `drop`. Each maps to task-store and calendar operations. Carry counts are a task field maintained by the task-store adapter. At `decisions.reconcile.carry_limit` (default: 3), `not_done` gives way to `rescope` and `drop` (FR-DEC-05).

A batch block (`<prefix>b_...`, §12.3) holds several small tasks. When one has elapsed, each member still open gets its own reconcile question, with the options and operations of a task block; a member already closed is not asked. The members are the task ids the plan that placed the block named. A batch key is a digest of its members, so earlier runs' plans are read newest first until the key is found; failing that, the item ledger's placements of the block (§13.8) name them. A block whose members are found nowhere is not asked about. A member's question id is `r_` + the block key + `.` + a digest of its task id, its prompt is `decisions.templates.reconcile_batch_prompt` (default: `Did "{task.title}" happen in "{block.title}" (...)?`), and its details carry `batch_block` (the block's key, title and number of open members), so a driver can show the members of one block together. Like a task block, a member is asked once: the next run's window starts after it.

**Covered.** A meeting that left no record cannot say what it covered, so an ask node can ask (FR-DEC-09). It lists `covered` in `questions.kinds` and says where the meetings come from:

```yaml
    read-meetings: { read: meetings }
    decide:
      ask: default
      from: [review]
      questions:
        kinds: [reconcile, covered, finding]
        covered: { meetings: read-meetings, to: agendas, suffix: "no record" }
```

`meetings` names the read node of the meetings source (§12.8), `to` the agenda destination, and `suffix` (default: `no record of the meeting`) the words the dated heading carries. A meeting is asked about when it happened (the roll's rule: a meeting read with a date, one a day per forum), its forum has an agenda in `to`, it left no record (its text is empty), it is newer than the agenda's latest dated section and is the latest such meeting of its forum, and the agenda section has entries. Then each entry is one question: "Did you cover this with Platform Sync on 2026-10-14?", with the entry's text and its sub-lines as evidence. The question's id is `c_` + a digest of the forum, the meeting's date and the entry's text: the same meeting and entry keep it, and a reworded entry gets a new one. The details carry `meeting` (id, date, title, forum, forum name and note path), so a driver shows one meeting's entries together.

The options are fixed in kind and configurable in label (`decisions.covered.labels`; the prompt is `decisions.templates.covered_prompt`): `covered` (the entry moves under the meeting's date), `keep` (still to raise; it stays where it is), and `drop` (removed, not recorded). `covered` is recommended and is a batch option, so the operator ticks a list and answers the rest one by one.

The plan node that takes these decisions turns the answers of one meeting into one `agenda.roll` on the agenda destination, which goes through approve and apply like any other operation. The covered entries, whole blocks with their sub-lines, become the body of a section headed with the meeting's date and the suffix. Covered and dropped entries leave the agenda section; kept ones stay. With nothing covered and something dropped, the roll only removes the dropped entries and writes no section. With everything kept there is no operation.

A covered question is asked once. Once the roll is applied the agenda has that date's section, so the meeting is no longer newer than it. A kept entry becomes an outcome of the plan, and a durable seal records it as a `keep` disposition keyed by the question id (§13.7). The same meeting does not ask about it again; the forum's next meeting has new ids and does. A sandbox run records nothing.

### 10.5 Task verification

Open tasks with references are resolved through sources (§6.10). A task whose reference is closed, or whose thread's last actor is the operator after the task date, produces a `verify` question ("Looks done: close it?") with `close`, `keep` and `update` options. The verification set is bounded by `decisions.verify.max_per_run`.

### 10.6 Day load

When an answer names a day, the echo includes that day's committed minutes, free minutes inside the work window and existing owned blocks, computed by the constraint engine (FR-DEC-08).

## 11. Destinations

### 11.1 Adapter interface

```python
class Destination(Protocol):
    id: str
    visibility: Literal["private", "shared"]
    def can_accept(self, action: Action) -> Acceptance: ...
    def snapshot(self) -> DestState: ...                       # digest-able current state
    def preview(self, action: Action, state: DestState) -> list[Operation]: ...
    def apply(self, op: Operation) -> Receipt: ...             # idempotent by op.id
    def verify(self, op: Operation, receipt: Receipt) -> VerifyResult: ...
    def sandbox(self) -> "Destination": ...                    # twin with identical behavior, no effects
```

An `Operation` is a fully concrete change: for a Markdown line, the file, the anchor (section heading), the exact old line digest (or `append`) and the new line; for a calendar block, the calendar id, the ownership key, start, end, title, notes and the verb. `op.id` is `sha256(destination, verb, ownership key or anchor, payload)`.

Sandbox twins are the real adapter with its I/O layer swapped: Markdown adapters write under the run's `sandbox/<destination>/` directory, copying each live file the first time they touch it; calendar adapters operate on an in-memory calendar seeded from the live one, or on the program's own sandbox mode when it declares one (§17.2). Because the twin runs the same preview, apply and verify code, a sandbox run (§14.3) exercises the code path a live run takes (FR-ACT-03).

### 11.2 Ownership

- **Calendar.** Every Docket block carries the marker from `destinations.<id>.marker`. `docket.destinations.calendar.owned_key()` is the single implementation that extracts and validates ownership from destination notes or properties. Planning and destination writes use this same scheme. Docket moves, updates and deletes only events carrying a valid marker for this workspace. It never sets attendees or recurrence; the adapter's operation model has no field for them (FR-ACT-04).
- **Markdown.** An update replaces exactly one line matching the operation's old-line digest within the anchor section. Zero matches or more than one match refuses that operation with `dest.line_changed` and the current text. Appends go to the end of the anchor section.

### 11.3 Built-in adapters

| Adapter | Behavior |
|---|---|
| `markdown_tasks` | A task store in Markdown. A configurable line grammar (`text/grammar.py`) parses and renders lines: checkbox, text, inline fields (`key:: value` or bracketed), links and tags. Sections and tiers are configured headings. Planning, reconciliation and task verification read open tasks through this adapter and its grammar. No second task parser exists. Quality rules (§11.5) run on every rendered line. |
| `markdown_agenda` | Agendas discovered by a path pattern or listed per forum. A configured section (for example `## Next`) with a capacity; dated history sections and unknown sections are preserved and tolerated. A `roll` writes a dated section under the agenda section and takes out the entries it covered (`remove`) and those dropped unrecorded (`drop`); a roll with no heading only drops (§12.8). |
| `calendar_exec` | Speaks the exec protocol (§17.2) to a local bridge binary with verbs `list`, `busy`, `create`, `move`, `update`, `delete`, `rekey`, `mirror`. The reference workspace wraps its existing bridge this way. |
| `calendar_file` | ICS read for availability; write to an ICS file for testing and for systems that import ICS. |
| `calendar_mcp` | An MCP calendar server, read always, write where the server's tools support it (declared in the preset). |
| `message_mcp` | Creates drafts through MCP. Sending is deferred with FR-ACT-05, and validation rejects `policy.send`. |
| `journal` | Writes the run journal into a Markdown file from templates (§13.5). |

### 11.4 Redaction

Each destination declares `visibility`. For a `shared` destination, every rendered title and note passes through `text/redact.py`: protected entity aliases, people's names (when `redact.people: true`), figures matching configured patterns, and any item with a sensitivity class render through that class's redacted template (for example `"{area} follow-up"`). Validation then scans the rendered text for any alias of any protected entity and any person name; a hit refuses the operation (`dest.redaction_leak`). The private record (the task store and the journal) keeps full detail (FR-ACT-06, NFR-02).

### 11.5 Task quality rules

```yaml
destinations:
  backlog:
    adapter: markdown_tasks
    quality:
      imperative: { deny: [fyi, note, maybe, consider], default_verb: "Follow up on" }
      max_length: 140
      require: [link, due, est, area]
      evidence: required
```

The imperative check is a denylist plus a safe default (FR-ACT-07): a line opening with a denied word is rewritten with the default verb and recorded as an unexpected shape; any other opening word is accepted. A missing required field takes a configured default where one exists (`due` from policy, `est` from size, `area` from topic) and is otherwise filled from the answer; a line that still lacks a required field refuses at preview with a fix that names the answer override.

A task list may name its fields its own way. The bracket grammar accepts a field with no key (`[:: 2026-10-16]`), `quality.fields` maps a required attribute to its name on the line (`due: ""`), and `quality.duration: minutes` writes an estimate as whole minutes. A finding's `action` is the operator's words when an answer gives them, else the ask the assess task wrote for the finding, else the item's title, so a line template that renders `{action}` starts with what to do and not with the first words of a message. `quality.optional` names required fields a task may lack: each is written in its place when the item has a value and left out when it has none.

A task with no due date can wait. `planning.policy.default_lead_days` gives such a task a due date that many working days after the run; set to null, the task has none, is placed in the earliest slot left after what is due, and is never late.

## 12. Planning

### 12.1 Calendar classification

`planning.calendar.provider` selects the calendar read path. `destination` lists through `planning.calendar.destination`; `json_file` reads the configured fixture path. Both derive ownership through the destination marker and `destinations.calendar.owned_key()`.

`plan/calendar_classify.py` maps each calendar event to one kind using `profile.calendar.classify` rules, in order: `meeting`, `focus`, `soft_hold`, `out_of_office`, `boundary`, `mirrored`, `owned`, `informational`. Owned is assigned first, from the ownership marker. An event no rule matches is `meeting` (busy) and records an unexpected shape with the title redacted to its word shape (FR-PLN-03). Recurring series expand within the horizon only; a span longer than a day is clipped per day.

### 12.2 One constraint engine

`plan/constraints.py` is the only definition of placement legality. It exposes:

```python
def free_intervals(day: Day, policy: Policy, events: list[Event], *, window: Window | None,
                   overlay: Overlay) -> list[Interval]: ...
def check(block: Block, ctx: Ctx) -> list[Violation]: ...
def search(task: Task, ctx: Ctx) -> Placement | Unschedulable: ...
```

The scheduler places blocks with `search`. The validator calls `check` on every block of a built plan and `search` on every task the plan claims is unschedulable. Because both call the same functions, a built plan always validates, and an unschedulable claim is reproducible (FR-PLN-05). Each `Unschedulable` carries the tightest violated constraint for `plan explain`.

Constraints (FR-PLN-01): workdays, day window, deep-work window (blocks of a configured kind only inside it), protected lunch, granularity, minimum and maximum block, buffer around meetings, daily cap of scheduled minutes, horizon, move cap per run, move lock (blocks inside a lock period stay put), no overlap with busy kinds. Soft holds and out-of-office are busy unless a task's window allows them (§12.4).

### 12.3 Scheduler

A plan node with `schedule_tasks: true` also places open tasks on the calendar, when `planning.calendar` is configured. Placement is deterministic and greedy, with bounded repair:

1. Gather open tasks through the configured `markdown_tasks` destination adapter and its line grammar, plus new tasks from answers.
2. Order: tasks with a deadline inside `imminent_days`, then overdue, then by due date, tier and size (configurable key list).
3. Batch tasks at or under `batch.max_minutes` into one daily batch block per day, filling in order (FR-PLN-02).
4. Keep existing owned blocks that are still legal (`check` returns nothing). Each kept block is an explicit `keep` operation.
5. Place each remaining task with `search`: earliest legal slot on or after its earliest start, before its due date if possible, otherwise the earliest legal slot in the horizon flagged `late`.
6. Repair: if a higher-priority task is unschedulable and moving at most `move_cap` lower-priority unlocked blocks frees a slot, move them. Each move is an explicit `move` operation.
7. Release owned blocks whose task is done, dropped or rescheduled, as explicit `release` operations. No owned block is left without an operation (FR-PLN-06).

Planning 200 tasks over 42 days runs in well under 5 seconds because `free_intervals` is computed once per day and updated incrementally on each placement (NFR-06).

**Batch identity.** A batch is its members: its id, and so its block's key, is the digest of its member ids. The same members planned on a later day are the same batch and keep their block; a batch whose members changed is another batch.

**Preferences.** Placement is the earliest legal slot unless the policy states a preference. `policy.prefer_window` names a part of the day to take first, and `policy.daily_target` how much planned work a day should hold. With either set, a task without a window takes, in order: the earliest on-time day under the target with a slot in the preferred window; the earliest on-time day under the target with any slot; then the lightest on-time day, preferred window first. When no on-time day has a slot the task is late, and it takes the earliest legal slot whatever the preference. So a preference spreads work, never makes a task late, and never makes a late task later. `deep_window` stays a rule for workspaces that want one.

**Splitting.** A task longer than `policy.split_over` becomes even parts on the grid (`ceil(est / split_over)` of them), each its own unit with its own stable key (`<task id>#<n>`), title suffix and block; a part goes on a later day than the parts before it. A task with a window is left whole. The split length and the batch size are defaults: a task line can carry `parts` (a number: 1 keeps the task whole, more splits it that many ways) and `batch: no` (never in the daily batch), under whatever field names `planning.tasks.fields` gives them. Title templates see `part` and `parts`, and a part carries its task's `label` and `id`. A part's block is a block of its task, so a reconcile question about an elapsed part is about the task.

### 12.4 Scheduling windows

An answer's `--when` becomes a window: a day, a day and time range, or a named event (resolved by title match within the horizon). Inside a window, `search` takes the estimate's length in the first stretch free of meetings, and treats the operator's own soft holds and out-of-office as available, which is how "schedule it over my flight" works without a hand edit (FR-PLN-04). A window that cannot fit the estimate fails the plan node with `plan.window_too_small`, naming the window's free minutes and the answer command that changes it. A window read from a stored task line whose every day is behind the run is not a failure: it cannot be met and says nothing about when to do the work now, so the task is placed like any other and the plan notes it.

### 12.5 Mirrors

```yaml
planning:
  mirrors:
    - from: personal
      to: work
      as: { title: "Busy", transparency: opaque }
      respect_deletions: true
```

Each run lists source events in the horizon, compares them with owned mirror holds by a source-event key, and plans `create`, `move` and `release` operations. A mirror hold the operator deleted is remembered in a `ledgers` row and not recreated (FR-PLN-07).

### 12.6 Plan artifact, diff and explain

A plan is `{inputs: {task_store_digest, calendar_digest, answers_digest, config_digest}, operations: [...], schedule: [...], unschedulable: [...], notes: [...]}` with digest `plan_digest`. `docket plan diff` renders operations in human terms grouped by day and destination ("Thu 13:00-14:00 new block: Draft pricing memo", "Backlog: close 'Review ingest SLO doc'"). `docket plan explain <item>` prints the task's ordering key, its candidate days and, for each rejected slot, the first violated constraint (FR-PLN-08).

### 12.7 Labels

A task line is written for the operator: what to do, who asked, why. A block on a shared calendar is read by other people. Whether a title is safe to show them is a judgment about the matter and not about its words: the same name is ordinary in one task and private in another, and no list of words or patterns separates them. So the words for a shared place are written by a judged task.

A judge node with the `label` task works on an ask node's decisions, and the plan node names both:

```yaml
    label: { judge: label, from: [decide] }
    plan:  { plan: [decide, label], schedule_tasks: true }
```

The node's candidates are the tasks planning may place: the open lines of the task store in the planned sections, and the answers that add a task or place a block. The `label` task reads each line and writes one short `label` for it, under the workspace's `tasks.label.guidance` and `tasks.label.examples`. The node hands on the labels alone, and the plan node reads them beside the decisions.

`planning.titles` templates see `label` and the task's `id` beside `title`. A workspace with a shared calendar titles its blocks with `{label}`, and can build a link back to the private line from `{id}` in the notes, so the detail stays where only the operator can open it. A task with no label renders the template's default.

A label is written once per line. A durable seal remembers each label against the digest of its line's words (`task_labels`); a later run reuses it and labels only lines that are new or changed, so titles do not drift from run to run. A sandbox run remembers nothing. The destination's exact-list redaction (§11.4) still runs over the rendered title as a backstop.

### 12.8 Rolling agendas

A forum that meets again and again keeps one agenda: what is next, then a dated section for each meeting held. After a meeting the agenda is rolled, so that what lands in it afterwards is for the meeting to come.

A source yields one item per meeting held: its title, start, the others who attended (`fields.attendees`), the record of the meeting as its text when there is one, and the record's link (`fields.record_url`). A meeting finds its forum through `forums[].calendar_match`: `attendee` when that person is the only other one present, else `title_regex` on the title. A meeting with no forum, or whose forum has no agenda, is left alone.

A graph that rolls agendas reads the meetings, recaps them and writes the rolls:

```yaml
    read-meetings: { read: meetings }
    recap: { judge: recap, from: [read-meetings] }
    roll:  { write: roll, from: [recap], to: agendas, trailer: "→ [notes]({url})" }
```

The `recap` judge reads the agendas of the destination `tasks.recap.agendas` names. A meeting is read only when it has a record and is later than its agenda's latest dated section; its pack item carries the agenda's entries (`fields.agenda`, each with an id). The task returns what the meeting covered, each note naming the agenda entry it settles when it settles one, and what people agreed to do.

The `write: roll` node turns the meetings it works on into `agenda.roll` operations, through the preview of the agenda destination `to` names, and writes them. A roll needs no approval, so `to` must name a private destination (`config.capability_missing` otherwise). The node keeps its plan and diff beside its receipts:

- A meeting with a recap gets a section of what was covered (and an action list), with `trailer` as its last line. The entries it settled leave the agenda; the rest stay.
- A meeting with no record is rolled only when the node has `no_record` and the meeting is the forum's latest: the agenda as it then stands moves down whole under a heading that carries `no_record`, on the assumption that it was all covered. With nothing on the agenda, nothing is written. Without `no_record` the node leaves a meeting with no record alone.
- Meetings are rolled oldest first, each operation waiting on the one before. A meeting on or before the agenda's latest dated section is not rolled, so the agenda file is its own record of what has been done.

A workspace chooses, per meetings source, between assuming and asking. To assume, the roll node sets `no_record` (for example `no_record: "no record, agenda assumed covered"`). To ask, the roll node drops `no_record` and an ask node lists `covered` with `questions.covered` naming the same read node (§10.4); the plan of that ask node rolls the agenda through approve and apply. A roll node with `no_record` downstream of the same source as an ask node's covered meetings is refused with `config.conflicting_settings`, at the roll's `no_record`.

With `commitments` naming a JSON-lines file, the node also appends what the operator agreed to do in the meetings recapped (the recap's actions whose `owner` is `me`), one record each and each once. A source can read the file, so a promise made in a meeting reaches triage like any other item. A sandbox run appends nothing.

At a durable seal, an item that was raised on an agenda (§13.8) is done when a roll took its entry off the agenda. The entry is recognised by its line as written, or by the item's link when the operator reworded the line.

## 13. Approve, apply and seal

### 13.1 Approval

An approve node asks one question, whose id is the node's name. It shows the plan diff path, the counts and the digest. `docket ask answer approve yes --digest <plan_digest>` (for a node named `approve`) binds the approval to that digest. Any change to an input digest makes the plan stale (§4.4) and the approval with it (FR-APP-01). Wall-clock build and approval times are appended to the `approvals` ledger outside the run artifacts. This keeps digest-bound artifacts deterministic.

### 13.2 Freshness

At approval, Docket snapshots each destination the plan touches and compares the snapshot digest with the plan's input digest for that destination. Equal: approved. Different: the approval is refused with `apply.stale_inputs`, the per-destination change (for example "2 calendar events added since build"), and `docket do <plan node>` as the next command. No clock check exists; a delay of hours is fine if nothing changed. `journal.approval_delay_note` reads build and approval times from the `approvals` ledger (FR-APP-02).

### 13.3 Executor

An `apply` node writes the operations its approve node approved, and a `write` node writes the operations it planned itself. Both run them through one executor, in a fixed destination order (task store, agendas, calendar, messages, journal) and in plan order within a destination:

1. Look up `op.id` in receipts. A receipt with `verified: true` skips the operation.
2. `apply(op)` writes and returns a receipt with the external id. The receipt is committed before the next operation starts.
3. `verify(op, receipt)` reads back and records the result.

After a crash, repeating the command continues from the receipts and repeats no completed operation (FR-APP-03). An integrity refusal (`dest.*`) is recorded as `refused` and is final. An external failure is recorded as `failed` with its raw error. Later operations that do not depend on it continue, and its dependants are `blocked`. The node then waits: `docket next` returns `wait` with reason `apply_partial` and `docket do <node> --again`. Dependencies are explicit in the operation (a calendar block for a new task depends on that task's line).

Before its first operation, the node pins every node upstream of it (§5.4). Its own writes to a task store, an agenda or a calendar that an upstream node read cannot then make the run go back.

### 13.4 Cursor advancement

A read node writes a `cursor_proposal` for each source and unit with the contiguous coverage it proved, from the durable cursor forward. A proposal with a gap ends at the gap, and a unit that proved nothing has none, so its cursor holds. The seal applies the proposals in one transaction. Positions only move forward; a proposal behind the durable cursor is ignored and recorded in the `cursor_regressions` ledger (FR-APP-04). A run abandoned before its seal advances nothing, so the next run re-reads. A sandbox run never moves a cursor.

### 13.5 Journal and ledgers

The journal is rendered from configured sections, each a template over a closed context: `run`, `sources` (outcomes and coverage), `findings`, `questions`, `answers`, `operations`, `not_asked`, `dropped` (with reasons), `coverage_notes`, `context` (context records), `unexpected`. Ledgers are generic rows (`ledger`, `run`, `key`, `json`) that templates can aggregate across runs, for example a carries ledger or a surfaced-items audit.

### 13.6 Run nodes and the seal

A `run` node runs a command once. A command that follows the writes, such as a commit or a reindex, is ordered with `after`:

```yaml
    reindex: { run: [bin/reindex, "{run.date}"], after: [apply], timeout_s: 300 }
```

Its argv templates may use `{run.id}`, `{run.date}`, `{run.graph}`, `{run.dir}`, `{run.sandbox}`, `{node}` and `{workspace}`; any other field is refused with `config.bad_template`. The command runs in the workspace directory with stdin closed, in a new session, and with a scrubbed environment: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR` and `TZ`, the variables the node lists in `env`, and `DOCKET_NONINTERACTIVE=1`, `DOCKET_RUN_ID`, `DOCKET_RUN_DIR`, `DOCKET_NODE` and `DOCKET_WORKSPACE`. Its output goes to `logs/run/<node>.log` in the run directory. A non-zero exit, a command that cannot start, or one that outlives `timeout_s` (default 120 seconds) fails with `node.command_failed`, which is retryable and names the log. The nodes that name it wait, and `docket do <node>` runs it again. A node that succeeded is current, so repeating a command never runs it twice (FR-APP-04, FR-APP-06). A sandbox run runs the node's `dry_run` form, or skips the node when it has none (§14.3).

The seal ends every run once every node is done. `docket seal` takes two steps, each with a receipt: `seal:state` applies the cursor proposals (§13.4), writes the dispositions (§13.7) and the item ledger (§13.8) and appends the configured ledgers, in one transaction; `seal:journal` renders the journal (§13.5) to `journal.md` and, when `journal.destination` names a destination, writes the entry through it. A seal that stops anywhere starts again at the first step without a receipt and repeats no completed step (FR-APP-06). `docket seal --dry-run` shows what sealing would do.

### 13.7 Dispositions

The seal writes a disposition per decided topic: `done`, `routed` (with the destination and operation id), `watch` (re-surface on new activity), `dismiss` (with an optional until date), `now` (with the recorded outcome). Merge (§7.3) reads them in the next run (FR-APP-05). A covered question answered `keep` (§10.4) is a `keep` disposition keyed by its question id; covered questions read it, and merge meets no item by that key.

### 13.8 The item ledger

The ledger is the master record of every item Docket has put in front of the operator: one row per item, in Docket's own state, keyed by a short stable key derived from the item's topic (`i` and eight characters). A row carries the item's source, title and link, its state, every decision made on it with the run that made it, and its placements: the task line, calendar block or agenda entry it became, with the operation that wrote it.

A state is where an item is; a decision is what moves it. The states are `new`, `backlog`, `scheduled`, `raised`, `monitoring`, `acknowledged`, `done` and `ignored`. Writing an item to a task store leaves it in `backlog`, to a calendar in `scheduled`, to an agenda in `raised`; a raised item is `done` when its agenda is rolled and its entry was covered (§12.8); `watch` leaves it `monitoring`, `ack` `acknowledged`, `now` and `done` `done`, `dismiss` `ignored`. An item put forward and not decided is `new`, and one seen again without a decision keeps its state. A scheduled item is not in the backlog: it has left it.

Destinations are projections of the ledger. Templates can write the item's `key`, and merge treats an item whose key is on an open task line as tracked, so an item is recognised wherever it landed and by whatever tool reads it next. No destination is the ledger's home: the same ledger serves a Markdown list today and another task store tomorrow.

The seal writes the ledger with the dispositions; a sandbox run leaves it as it was. `docket ledger list [--state S]` and `docket ledger show <key>` read it, and `docket ledger import <file>` enters items by hand from a JSON array or JSON lines, never overwriting a row. An imported item may carry the `key` already written where it lives and name that place (`destination`, `adapter`), so an item adopted from an existing list is followed there like any other.

The operator works in the destination, not in Docket, so the ledger follows it. A destination that can report the status of the keys it holds does so (`key_status`: `open` or `done` per key; the Markdown task store reads its lines). After its own writes, every durable seal reads each destination back: an item whose line is closed moves to `done`, and so does one whose line is gone after having been seen there. A destination that cannot be read, or whose lines never carried the key, moves nothing: absence is evidence only where presence was observed. `docket ledger reconcile [--dry-run]` does the same on demand.

Blocks are tracked back to their items. After a run that applies a plan, the blocks that stand (those kept and those written) are placements of the ledger items among their task ids: an item split into parts has several, each saying how many (`of`), and a batch block is a placement of each of its items, naming the others (`with`). An item's calendar placements are replaced by the present ones on every such run, so the record follows when blocks are consolidated, split up or released: an open item with a block is `scheduled`, and one whose blocks are gone returns to `backlog`. A scheduled item is still followed on its task line. `docket ledger show <block key>` gives the items a block is for.

## 14. Robustness

### 14.1 Integrity checks and shape checks

Every check in Docket belongs to one of two classes, and the class is declared in code by the function used to raise it.

| Integrity (refuse: `integrity.*`) | Shape (tolerate: `unexpected()`) |
|---|---|
| Node digest mismatch on an artifact a command consumes | A response field missing, renamed, or of a new value |
| Approval digest differs from the plan digest | A unit of a new type, or with no display name |
| Identity partition of a task result | An unknown vocabulary value in a task result |
| Verbatim evidence not found in the raw record | A calendar event no classification rule matches |
| Receipt exists with a different payload for the same operation id | A Markdown section or heading the grammar does not know |
| Markdown line changed since the plan was built | A task line that does not parse under the grammar |
| Cursor proposal behind the durable cursor | A date or duration in an unrecognized format |
| Redaction leak on a shared destination | A pack over its byte budget because of must-surface items |

A code review rule backs the table: a new `raise` on data from a source, an agent or a destination must be an `IntegrityError` with a code from §15.3, or it is a defect. The test suite enforces this with a static check over `src/` (Test Plan §5.1).

### 14.2 Recording an unexpected shape

```python
unexpected(area="source.contract", detail="space.type=THREADED_DM",
           sample=redacted_sample, reading="treat as group space",
           extend="preset mcp/chat-codemode: contract.space.type")
```

Each record has an area, a detail key (the shape, not the instance, so repeats aggregate), a redacted sample of at most 500 bytes, the reading taken and the extension point. Records go to the run's `unexpected.jsonl`, the `unexpected` table, the envelope's `unexpected` array of the command that hit them, the run's summary and the journal. `docket unexpected [--since 7d]` aggregates unaccepted shapes by area and detail, most frequent first, with first and last seen and the extension point (FR-ROB-01, FR-ROB-02). `docket unexpected --all` includes accepted shapes. `docket unexpected accept <area> <detail> --reason ...` writes the `unexpected_accepted` table and prints the matching `shapes.accepted` configuration entry. Committed `shapes.accepted` entries make review durable and portable.

### 14.3 Sandbox runs

`docket run start <graph> --sandbox` starts a run of the graph that writes nothing outside the run. It is driven by the same loop and the same commands as any run:

- every destination the run writes is its sandbox twin (§11.1), under `runs/<id>/sandbox/` for Markdown;
- a `run` node runs its `dry_run` form, or is skipped when it has none;
- a `write` node appends no commitments;
- the seal writes the journal to `journal.md` and through the journal destination's twin, and moves no cursor and writes no disposition, item ledger row, ledger row or label.

Sources are read live, since a source has no write path. Every other step is the code a live run takes: the same packs, questions, plan, approval, executor, receipts and verify. The journal and the run's summary report every operation, refusal and unexpected shape, so a defect shows up in a sandbox run before a live run meets it (FR-ROB-03, NFR-08).

### 14.4 Crash, repetition and change

A crash, a repeated command and a mid-run change of code or configuration reduce to §5.4: read the state, compute digests, recompute what is stale, reuse what is current, and read the nodes upstream of a write as pinned. There is no separate recovery code path to fall out of date (FR-ROB-04).

### 14.5 Unattended operation

Every subprocess Docket starts (transports, auth checks, adapters, run nodes) runs with stdin closed, no controlling TTY, a timeout, and `DOCKET_NONINTERACTIVE=1`. A timeout reports the command and the elapsed time. `docket doctor` lists each configured subprocess and checks it in this mode (FR-ROB-06, NFR-05).

## 15. CLI

### 15.1 Command surface

| Group | Commands |
|---|---|
| Loop | `next [--full]` |
| Runs | `run abandon`, `run continue`, `run list`, `run start`, `run status`, `run steal`, `seal` |
| Nodes | `do`, `graph show`, `plan diff`, `plan explain`, `plan load` |
| Tasks | `task issue`, `task kinds`, `task list`, `task show`, `task submit` |
| Decisions | `ask answer`, `ask list`, `ask resolve`, `ask show`, `ask undo` |
| Inspection | `item list`, `item why`, `unexpected`, `unexpected accept`, `units list`, `units preview`, `units set`, `units unset`, `ledger list`, `ledger show`, `ledger import`, `ledger reconcile` |
| Config | `config explain`, `config test`, `config validate`, `init`, `presets list`, `presets show` |
| Sources | `source list`, `source test` |
| Health | `doctor [--graph <name>] [--probe]` |
| Plugins and import | `import cursors`, `plugin check`, `plugin list` |
| Self-description | `help`, `schema`, `schema list`, `version` |

This table lists all 51 commands returned by `docket help --json`. `schema answers-file` is the self-described schema for `ask answer --file`; task kinds also add `<kind>-input` and `<kind>-output` schema names.

Every command takes `--json`, `--workspace` and `--run` where it applies. Every mutating command takes `--dry-run` and reports what it would do (AE-06).

### 15.2 Envelope

```json
{
  "ok": true,
  "command": "ask answer",
  "workspace": "/workspace",
  "run": "2026-10-16-daily",
  "data": { "...": "..." },
  "warnings": [ { "code": "lease.adopted", "message": "..." } ],
  "unexpected": [ { "area": "...", "detail": "...", "reading": "...", "extend": "..." } ],
  "next": { "command": ["docket", "next", "--json"], "shell": "docket next --json", "why": "3 questions remain" }
}
```

On failure, `ok` is false and `error` stands in place of `data`:

```json
{ "ok": false,
  "error": { "code": "apply.stale_inputs", "kind": "integrity",
             "message": "The plan is stale: work-calendar: 2 events added. Rebuild it and show the new diff before approving.",
             "hint": "docket do plan", "retryable": true,
             "details": { "plan_digest": "...", "destinations": { "work-calendar": { "...": "..." } } } },
  "next": { "command": ["docket", "do", "plan", "--json"], "shell": "docket do plan --json", "why": "Rebuild the plan against the current calendar" } }
```

Human output (without `--json`) is a short summary and the `next` line. Long payloads are files whose paths appear in `data` (AE-08).

### 15.3 Error codes and exit codes

Codes are dotted `<area>.<condition>`, stable across versions, and listed by `docket help errors --json`. Exit codes map to kinds (AE-03):

| Exit | Kind | Examples |
|---|---|---|
| 0 | ok | |
| 2 | input | `input.unknown_option`, `input.ambiguous_option`, `input.bad_duration` |
| 3 | config | `config.unknown_key`, `config.bad_reference`, `config.invalid` |
| 4 | integrity | `integrity.partition`, `integrity.evidence`, `apply.stale_inputs`, `dest.line_changed`, `dest.redaction_leak` |
| 5 | external | `source.unavailable`, `transport.timeout`, `dest.apply_failed`, `node.command_failed` |
| 6 | lease | `lease.held` |
| 7 | state | `run.none_open`, `run.sealed`, `node.not_ready`, `node.post_apply_change`, `state.too_old` |
| 70 | internal | `internal.bug` (with a traceback path, never a traceback on stdout) |

### 15.4 Doctor

`docket doctor` writes nothing and checks: configuration validity; state root writability and database version; each transport's binary or endpoint; each auth check in unattended mode; each destination's location, permissions and capability declarations; each run node's command and its `dry_run` form; platform permissions an adapter declares. Remote endpoints are reported as configured and contacted only with `--probe`. Each check names the graphs it affects, and each failure names the fix. `docket doctor --graph <name>` limits source, transport, destination and command checks to those that graph uses.

### 15.5 Input normalization

`text/dates.py` and `text/durations.py` are the only parsers for dates, weekdays, windows and durations, and every command and answer uses them. Weekday names resolve to the next occurrence strictly after the run date, unless `today` or a date is given. Durations accept `45`, `45m`, `1h`, `1.5h`, `1h30m`. Normalized values are echoed (AE-09).

### 15.6 Speed

`docket next` and read-only commands load configuration from a cached resolved form keyed by file modification times and sizes, open the database read-only, and compute digests from stored input digests rather than rehashing blobs. The target is under 300 ms warm (AE-10). Import time is kept down by lazy imports of transports and adapters.

## 16. Security and privacy

- **Credentials.** Configuration names credential sources (an environment variable name, a file path, a command that prints a token). Values are read at use, passed to transports only, and never logged, stored in records, artifacts, envelopes or unexpected samples. A redaction filter on all output paths masks any value read from a credential source (NFR-01).
- **Untrusted content.** Source text appears to agents only inside fenced, nonce-delimited pack sections (§9.3). Docket itself never interprets source text as instructions: templates do not evaluate content, and a source field cannot name a command, path or destination.
- **Least effect.** Sources have no write path. Destinations write only owned objects (§11.2). A `write` node needs no approval, so it may target only a private destination. Message destinations create drafts; sending is not implemented.
- **Samples.** Unexpected-shape samples pass through redaction (protected entities, people, figures) before storage.

## 17. Extensibility

### 17.1 Three tiers

1. **Configuration.** New source instances on existing transports, rules, option sets, templates, destinations on existing adapters, and presets (G6).
2. **External programs** speaking the exec protocol: a source, a destination adapter, or a plugin predicate in any language.
3. **Python plugins** registered through the `docket.plugins` entry point group, implementing `Source`, `Destination`, `Transport`, or `Predicate`.

### 17.2 Exec protocol

A long-lived child process reads JSON-lines requests on stdin and writes JSON-lines responses on stdout. Logs go to stderr and are captured per run.

```json
{"v":1,"id":1,"op":"hello","args":{"protocol":1,"mode":"sandbox","base":"/path/to/live-target"}}
{"v":1,"id":1,"ok":true,"result":{"name":"calbridge","protocol":1,"capabilities":["list","busy","create","move","update","delete","rekey","mirror","status"]}}
{"v":1,"id":2,"op":"create","args":{"calendar":"work","key":"dk_3f2a","start":"...","end":"...","title":"...","notes":"..."}}
{"v":1,"id":2,"ok":true,"result":{"external_id":"..."}}
{"v":1,"id":3,"op":"list","args":{"calendar":"work","from":"...","to":"..."}}
{"v":1,"id":3,"ok":false,"error":{"code":"permission","message":"...","retryable":false}}
```

`hello` carries `mode: live|sandbox|scratch`. Sandbox mode also carries `base`, the target from which the twin is seeded. The response negotiates the protocol version, capabilities and supported modes. Source programs implement `discover`, `read` (returning pages with a `next` position and an `end` flag) and optionally `resolve`. Destination programs implement `snapshot`, `apply` per verb, and `verify`; Docket builds operations and previews itself so ownership and redaction rules stay in core. A program may declare `sandbox: true` to run its own twin; otherwise Docket uses a generic in-memory twin for the verb set.

### 17.3 Conformance

`docket plugin check <name>` runs only against a disposable scratch target. The conformance suite covers protocol handshake, idempotent apply under repeated operation ids, verify after apply and twin parity. It never points a plugin check at a live or sandbox target. The same suite runs in Docket's own tests for every built-in adapter.

## 18. Performance budget

| Path | Budget | Mechanism |
|---|---|---|
| `docket next`, read-only commands | under 300 ms warm | Cached resolved configuration, stored digests, lazy imports |
| Source reads | Network-bound | `asyncio`, per-source and global concurrency, long-lived transport sessions |
| Assess pack build for 500 items | under 1 s | In-process, no subprocess |
| A plan node placing 200 tasks over 42 days | under 5 s | Incremental free intervals per day (§12.3) |
| Full synthetic run in tests | under 60 s | Recorded transports, sandbox twins, synthetic task results |

No command launches a process per item (AE-10).

## 19. The reference workspace

The reference workspace models the predecessor as configuration. It lives in the operator's private configuration repository, outside the Docket repository (PRD §14.2), and uses only presets and configuration. PRD Appendix B maps every element. Three elements need care:

- **Chat over an MCP gateway's code-mode tool.** The `mcp/chat-codemode` preset ships the paging and projection script with no deployment-specific host or tool names; the workspace sets the server and tool names.
- **The native calendar bridge.** Wrapped through `calendar_exec` (§11.3). The bridge's existing verbs map one to one onto the exec protocol; a small shim translates its argument shapes. The shim lives with the workspace.
- **External state tools.** The predecessor kept watermarks in an external tool. Docket keeps cursors in its own database, and a one-time `docket import cursors --from <json>` seeds them so the first Docket run does not re-read weeks of history.

## 20. Decisions and open questions

### 20.1 Decisions taken in this design

| Decision | Alternatives rejected | Reason |
|---|---|---|
| Digest-named artifacts | A version counter on every artifact | One rule detects every artifact built from stale inputs (PRD §2.2). |
| A graph of single-verb nodes, each naming what it needs | Scripted control flow with branches and loops | The shape is checked when the workspace loads; independent nodes are ready together; a wait holds back only what depends on it. |
| Each write pins the nodes upstream of it | Recomputing upstream after a write | A run's own writes cannot make it go back. |
| Pydantic models as the single schema source | Hand-written JSON Schema, prompt text | Prompt, validator and `docket schema` cannot drift. |
| Closed predicate and effect sets with a plugin escape hatch | An expression language | Validatable, explainable, and resistant to creep (PRD §14.1). |
| A restricted template language | Jinja | Total rendering, no code execution, one page of docs. |
| Explicit node commands plus `next` | `next` doing the work of computed nodes itself | The agent's transcript shows every effect; `next` stays read-only and fast. |
| Destinations build operations in core; programs only execute | Programs build their own operations | Ownership, redaction and quality rules stay in one place. |
| Sandbox twins swap only the adapter's I/O layer | Separate mock destinations | A sandbox run exercises the preview, apply and verify code a live run takes. |

### 20.2 Open questions

1. Should `docket next` be allowed to do the work of computed nodes itself behind a flag (`--auto`), to halve round trips for agents that trust it?
2. Should the review task batch by topic cluster rather than by score order, to help the reviewer spot merges?
3. Should accepted unexpected shapes expire after a period, so a forgotten acceptance resurfaces?
4. Is SQLite enough for a later multi-workspace mode, or does that mode need a shared store?

## Appendix A: Requirement traceability

| PRD area | Design sections |
|---|---|
| FR-SRC-01..12 | §6 |
| FR-SIG-01..09 | §7, §8 |
| FR-TSK-01..07 | §9 |
| FR-DEC-01..09 | §10, §12.8 |
| FR-ACT-01..07 | §11 |
| FR-PLN-01..08 | §12 |
| FR-APP-01..06 | §13 |
| FR-ROB-01..06 | §5.4, §5.5, §14 |
| AE-01..11 | §5.2, §9.3, §15 |
| CFG-01..07 | §3, §4.4 |
| NFR-01..09 | §4.6, §16, §18, Test Plan |

## Appendix B: Implementation status

| Milestone | Status | Result |
|---|---|---|
| M0 | Built | Foundation, configuration, schemas, CLI contracts and state store |
| M1 | Built | Engine, graphs, nodes, leases and the agent loop |
| M2 | Built | Source reads, transports, cursors and response contracts |
| M3 | Built | Items, signals, scoring, merge and explanations |
| M4 | Built | Agent task kinds, packs, prompts and validation |
| M5 | Built | Questions, answers, reconciliation and verification |
| M6 | Built | Destination adapters, ownership, twins and conformance |
| M7 | Built | Planning, calendar classification, constraints and scheduling |
| M8 | Built | Approval, apply, receipts, the seal, journals and run nodes |
| M9 | Built | Sandbox runs, unexpected-shape review, doctor and agent polish |
| M10 | Not started | Requires the operator's live workspace and accounts |
| M11 | Not started | Requires shadow runs against the operator's live systems |
| M12 | Not started | Requires live cutover of the operator's accounts and calendars |
