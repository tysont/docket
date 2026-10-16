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
4. **Derived state is a pure function.** Every derived artifact is a function of immutable raw records, configuration, answers and a pinned clock. Its identity is a digest of those inputs. Refresh, resume and a mid-run change are one operation: recompute what is stale.
5. **One implementation per rule.** A rule (a constraint, a grammar, a vocabulary, a schema) lives in one module that both builds and checks. Prompts are rendered from the same schema the validator uses.
6. **The CLI owns the procedure.** `docket next` computes the next step from state. No document of command ladders exists for the agent to follow.
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
| Engine        stage graph, staleness, next-step, lease, rehearse     |
+---------------------------------------------------------------------+
| Stages        collect | assess | merge | review | decide | plan |    |
|               approve | apply | close                                |
+---------------------------------------------------------------------+
| Domain        items, signals, tasks, questions, answers, schedule,   |
|               constraint engine, templates, redaction, dispositions  |
+---------------------------------------------------------------------+
| Ports         Source | Transport | Destination | Hook  (+ sandboxes) |
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
| Concurrency | `asyncio` inside one process | Collection is I/O-bound; no process per item (AE-10). |

Runtime dependencies are Pydantic v2, `ruamel.yaml`, `httpx`, `tzdata` and the official `mcp` Python SDK v2. Development dependencies are `pytest`, Hypothesis and `pytest-xdist`. A dependency joins only when it removes more code than it adds.

### 2.3 Package layout

```
src/docket/
  apply/          approval, freshness, apply boundary and executor
  cli/            entry point, command registry, envelopes and errors
  close/          cursors, journal, ledgers, hooks and dispositions
  config/         loader, models, presets, validation, origins and digests
  decide/         questions, options, answers, reconciliation and verification
  destinations/   built-in adapters, ownership, conformance and sandbox twins
  engine/         stages, next-step calculation, rehearsal and pinned clock
  items/          normalized item model, mapping, identity and records
  plan/           calendar providers, constraints, scheduler, mirrors and task-store reads
  presets/        shipped YAML presets with no user data
  shapes/         unexpected-shape recording
  signals/        rules, scoring, merge, replay and explanations
  sources/        collection, cursors, generations, contracts and unit registry
  store/          database, migrations, runs, blobs, paths and leases
  tasks/          task kinds, packs, prompts, submission and result index
  text/           templates, redaction, grammar, dates and durations
  transports/     MCP, code-mode, exec, HTTP, files and replay transports
```

## 3. Workspace and configuration

### 3.1 Files

A workspace is a directory containing `docket.yaml` and any number of further YAML files it includes. The conventional split is:

```
<workspace>/
  docket.yaml          version, includes, presets, workflows
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
| `workflows` | Named workflows (§5.6) |
| `profile` | `me`, `people`, `groups`, `topics`, `entities`, `forums`, `calendar`, `timezone`, `workdays` |
| `sources` | Source instances keyed by id (§6) |
| `transports` | Shared transport instances keyed by id (§6.3) |
| `signals` | `rules`, `scoring`, `budgets`, `units` classification (§8) |
| `tasks` | Agent task kinds and their overrides (§9) |
| `decisions` | Option sets, label templates, reconcile policy (§10) |
| `destinations` | Destination instances keyed by id (§11) |
| `planning` | Scheduling policy, mirrors (§12) |
| `journal` | Journal sections, ledgers (§13.5) |
| `hooks` | Named hooks bound to close and other events (§13.6) |
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

1. **Syntax and structure.** YAML parse, unknown keys, types. Each error carries file, line, column, the offending key, the expected type and, for unknown keys, the closest valid key.
2. **References.** Every id a value names (a person, group, topic, source, destination, forum, template, rule, task kind, hook) exists.
3. **Semantics.** Regexes compile; templates parse and reference only fields of their context; durations and windows are well-formed; scoring weights are finite; a workflow's stages respect the pipeline order (§5.1); a shared destination has redaction templates for every sensitivity class the rules can assign.
4. **Capabilities.** Each source's transport and each destination's adapter declare the operations the workflow needs (for example, a planning workflow needs a calendar destination that supports `create`, `move`, `update`, `delete` and `list`).

A run never starts on invalid configuration (CFG-02). Pass 4 is the only pass that needs a preset's capability declarations; it never contacts a system. `docket doctor` contacts systems (§15.4).

### 3.5 Configuration digest and partial digests

The resolved configuration serializes to canonical JSON (sorted keys, no insignificant whitespace, normalized numbers). Its SHA-256 is the configuration digest. Each stage declares which configuration sections it reads; its stage digest (§4.4) includes only the digest of those sections. Editing a planning policy therefore stales `plan` and later stages, not `assess` (CFG-06).

### 3.6 Template language

Templates are strings with `{field}` and `{field|filter|filter:arg}` substitutions, plus two block forms: `{?field}...{/field}` (render if present and non-empty) and `{#list}...{/list}` (repeat). There are no expressions, loops beyond lists, or calls. Filters are a closed set: `upper`, `lower`, `title`, `trim`, `truncate:N`, `date:FORMAT`, `duration`, `join:SEP`, `default:TEXT`, `redact`, `slug`, `first`, `count`. Each template site declares its context model; validation rejects a field the context does not have (CFG-05). Rendering is total: a missing optional field renders empty, never raises.

## 4. State

### 4.1 Locations

```
<state_root>/                          default ~/.local/state/docket/<workspace-slug>/
  docket.db                            workspace database
  blobs/<sha256[0:2]>/<sha256>         content-addressed raw payloads
  runs/<run-id>/
    run.json                           run header: workflow, date, clock pin, status
    gen-<n>/records/<source>.jsonl     record index for generation n (payloads in blobs)
    artifacts/<stage>/<digest>.json    derived artifacts
    tasks/<task-id>/pack.json          task input pack
    tasks/<task-id>/prompt.md          rendered prompt
    tasks/<task-id>/result.json        submitted result (accepted only)
    questions.json, answers.json       current decision state
    plan/<digest>/plan.json, diff.md   plans by digest
    receipts.jsonl                     apply receipts, append-only
    journal.md                         rendered journal for this run
    unexpected.jsonl                   unexpected shapes recorded in this run
```

The state root is outside the workspace so configuration repositories never commit state. `docket` never writes inside the workspace except through a destination that points there.

### 4.2 Workspace database

SQLite in WAL mode. Tables:

| Table | Purpose |
|---|---|
| `runs` | Run id, workflow, date, status, current generation, lease owner, timestamps |
| `cursors` | `(source, unit) -> position, coverage_end, updated_run` |
| `cursor_proposals` | Positions proven by a run, applied at close (§13.4) |
| `units` | Unit registry: `(source, unit_id) -> name, kind, class, tier, first_seen, last_seen, classified_by` |
| `processed_ids` | For *id-scan* sources: ids processed, by run |
| `dispositions` | `(topic_id) -> decision, run, item, until, note` (FR-APP-05) |
| `receipts` | Operation id, destination, outcome, external id, verify status, run |
| `unexpected` | Shape records across runs, aggregated for `docket unexpected` |
| `unexpected_accepted` | Reviewed shape acceptances recorded by `docket unexpected accept` |
| `ledgers` | Generic append-only rows for journal ledgers (coverage, surfaced, carries) |
| `leases` | Run lease rows (§5.5) |
| `task_results` | Accepted task results indexed by task input digest (§9.6) |
| `step_receipts` | Completed close steps and hooks, so resume does not repeat them |
| `schema_meta` | Database schema version, migrations applied |

Migrations are forward-only and numbered. Docket refuses to open a database newer than it understands and names the version it needs (NFR-09).

### 4.3 Records

A record is one raw read: the transport response body exactly as received, stored once in `blobs/`, plus an index row:

```json
{"record_id":"r_7f3c...","source":"chat","unit":"spaces/AAA","generation":0,
 "query":{"tool":"...","args":{"...":"..."}},"interval":{"from":"...","to":"..."},
 "page":3,"outcome":"read","blob":"sha256:...","received_at":"..."}
```

Records are immutable (FR-SRC-05). Everything downstream references record ids, and verbatim evidence checks run against the decoded blob.

### 4.4 Artifacts and stage digests

Every derived artifact is named by its stage digest:

```
stage_digest = sha256(canonical_json({
  "stage": name,
  "schema": stage_schema_version,
  "config": partial_config_digest(stage),
  "inputs": [digest of each upstream artifact or record set],
  "answers": digest of answers the stage reads (decide onward),
  "clock": run clock pin (date and timezone; time only for stages that read it)
}))
```

An artifact on disk whose name equals the freshly computed digest is current. Any other is stale. This one rule replaces generation bookkeeping: an artifact built from generation 0 inputs cannot be mistaken for one built from generation 1, because its digest differs (PRD §2.2, "state from one generation checked against another").

`schema` increments when a stage's code changes its output in a way that must invalidate old artifacts. A code upgrade mid-run therefore recomputes exactly the affected stages (FR-ROB-04).

### 4.5 Generations

A generation is one collection pass. Generation 0 is the first. A refresh opens generation n+1, reading incrementally from generation n's coverage end without moving the durable cursor. `docket collect --refresh --sources a,b` refreshes the named sources. With no `--sources`, it refreshes the workflow's `refresh_on_resume` list when present, otherwise its volatile sources. Downstream stages read the union of all generations' records, de-duplicated by the source's record identity. The durable cursor moves only at close (§13.4).

### 4.6 Clock

A run pins its clock at creation: date, timezone and a reference instant. Stages that need "now" (planning, freshness notes) read the pinned instant, or an explicit `--now` in rehearsal and tests. Nothing derived reads the wall clock, which makes NFR-03 testable.

## 5. Engine

### 5.1 The pipeline

The stage order is fixed:

```
collect -> assess -> merge -> review -> decide -> plan -> approve -> apply -> close
```

A workflow may omit stages but may not reorder them. `collect` must be first and `close` last. `apply` requires `approve`, `approve` requires `plan`, and `plan` requires `decide`. `decide`, `review` and `merge` require `assess`. These rules keep approval bound to a concrete plan. Each stage is a module implementing:

```python
class Stage(Protocol):
    name: str
    schema_version: int
    config_sections: tuple[str, ...]
    def inputs(self, run: Run) -> list[InputRef]: ...
    def status(self, run: Run) -> StageStatus: ...   # current | stale | blocked(reason) | waiting(kind)
    def step(self, run: Run) -> Step: ...            # the next concrete step for this stage
```

`collect`, `merge`, `plan`, `apply` and `close` are *computational*: Docket does the work when the agent runs the named command. `assess` and `review` are *delegated*: they produce dispatch steps and complete when every task has an accepted submission. `decide` and `approve` are *interactive*: they produce ask steps and complete when every question has an answer.

### 5.2 `docket next`

`docket next` loads the run, computes each stage's status in order, and returns the first non-current stage's step. It writes nothing except a heartbeat on the lease. Step kinds (PRD §6.4, AE-01):

| Kind | Fields |
|---|---|
| `run` | `command` (argv array and a shell string), `why`, `expect` (what the command will produce), `timeout_s` |
| `dispatch` | `tasks[]` each with `task_id`, `kind`, `prompt_path`, `pack_path`, `submit` (exact argv), `concurrency`, `why` |
| `ask` | `questions[]`, one `docket ask answer {question} {option} ...` template, one `show` template, `burst`, `why` |
| `wait` | `reason` (code), `instructions`, `resume_at` or `resume_when`, and a concrete command when one applies |
| `done` | `summary` (counts, journal path, unexpected count) |

Every command in a step is concrete except the compact ask step's documented `{question}` and `{option}` answer template (AE-07). `docket next --full --json` expands ask details and gives each option its exact answer command. Dispatch steps always carry real run ids, task ids and paths. `docket next --json` output for a dispatch of 40 tasks stays small because prompts and packs are paths, not contents (AE-08).

`wait` reasons include `paused` (the workflow pauses before a configured stage), `sources_unavailable` (with per-source auth instructions), `lease_held`, `post_apply_change`, and `operator_approval` when the agent has shown the plan and is waiting for an answer. A paused step carries `docket run resume`. When the workflow sets `rehearse_at_pause: true`, it also carries `docket run rehearse`.

### 5.3 Commands that advance stages

The computational stages run through explicit commands so the agent's transcript shows each effect:

- `docket collect [--refresh] [--sources a,b]`
- `docket task issue`
- `docket merge`
- `docket plan build`
- `docket apply`
- `docket close`

Delegated stages first return a `run` step for `docket task issue`. That command writes each pack and prompt. Only then can `docket next` return the `dispatch` step that points at those files. `docket next` remains read-only except for its lease heartbeat.

Each advancing command checks that its stage is next or stale, does the work, and returns the following step in its envelope's `next` field. Running a command for a stage that is already current is a no-op that reports `current` (AE-06).

### 5.4 Staleness and recomputation

When an upstream digest changes (a refresh, an answer change, a configuration edit, a code upgrade), downstream artifacts become stale by §4.4 with no explicit invalidation. The engine recomputes computational stages on demand. For delegated stages, it reuses accepted results whose task input digest is unchanged (§9.6), and issues tasks only for the rest. For interactive stages, it keeps answers bound to unchanged item identities and re-asks the others (FR-DEC-06).

Two boundaries are never crossed backward. When `apply` starts, it pins every prior stage digest and sets the apply boundary before the first operation. Later upstream configuration or code drift produces a `wait` with reason `post_apply_change`, inspection commands and `docket run continue`. Continue records that the operator saw the drift and lets the run finish on its approved plan. Once `close` advances cursors, the run is sealed.

### 5.5 Leases

A run lease is a row `(run_id, owner, pid, host, acquired_at, heartbeat_at)`. Mutating commands acquire or renew it in the same transaction as their first write. An owner is *dead* when its pid is gone on the same host, or its heartbeat is older than the configured timeout (default 15 minutes). A dead lease is adopted automatically and the adoption is reported. A live foreign lease fails with `lease.held`, naming the owner and the steal command `docket run steal <run-id>` (FR-ROB-05). The owner identity is `DOCKET_SESSION` when set, otherwise the run's session token. Docket writes that token to the run directory on first use, so separate shell calls share it without an environment variable (AE-05).

### 5.6 Workflows

```yaml
workflows:
  daily:
    stages: [collect, assess, merge, review, decide, plan, approve, apply, close]
    sources: [chat, dms, mail, transcripts, incidents, tickets, health, calendar, tasks]
    pause_before: decide
    rehearse_at_pause: true
    refresh_on_resume: [chat, dms, mail, calendar, tasks]
    review: { pool: 40 }
    questions: { kinds: [reconcile, finding], bursts: 1, budget: 15 }
    options: people-manager-default
    journal: daily
    hooks: { after_close: [notes-commit, search-reindex] }
  triage:
    stages: [collect, assess, merge, decide, plan, approve, apply, close]
    sources: [chat, dms, mail]
    questions: { kinds: [finding], budget: 8 }
    options: triage
```

`docket run start <workflow> [--date D] [--sandbox] [--new]` creates a run. Repeating it for the same workflow, date and sandbox mode returns the existing run unless `--new` is present. `--sandbox` binds every destination to its sandbox twin for the whole run. Sandbox runs may read live sources but write no durable cursors, dispositions or ledgers. Their hooks run only in declared `dry_run` form, or are skipped. `docket next` with no run open reports `done` with the command to start one. A paused run advances only through `docket run resume`.

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
    volatile: true
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
| `mcp_stdio` | Tool name and arguments | Spawns the server once per collect, reuses the session. |
| `mcp_http` | Tool name and arguments | Streamable HTTP; bearer token read from a file or an env var name, never from configuration text. |
| `codemode` | Script template and parameters | For servers that expose one code-execution tool. The preset ships a script that pages, projects to declared fields and returns compact JSON under the server's response cap. |
| `exec_jsonl` | One JSON object on stdin | A long-lived child process speaking the Docket protocol (§17.2). One process per source per collect, not per item. |
| `http_json` | Method, URL template, headers, body | For plain JSON APIs. |
| `files` | Glob and parser | Local files: Markdown, JSON, ICS, CSV. |

Transport instances are declared once under `transports:` and shared by sources (`chat-gateway`, `docs-gateway`). Credentials are referenced by environment variable name or file path; the loader rejects a literal that looks like a secret (high-entropy strings in credential-named fields).

### 6.4 Cursor models

| Model | Read | Coverage proof |
|---|---|---|
| `snapshot` | The whole collection each generation | Complete when paging reached the end within budget. |
| `incremental` | Records with ordering key greater than the cursor, up to the run's interval end | Complete when paging reached the end and the last page's ordering key is at or past the interval end, or the source reports no more. |
| `units` | Discover units, then `incremental` per unit | Per unit. A unit with a gap holds its own cursor; others advance. |
| `id-scan` | List ids, read those not in `processed_ids` | Each id processed individually. |
| `cadence` | Any of the above, only on configured weekdays or every N days | A skipped day reports `skipped`, never a gap. |

The ordering key, page token field and "no more" signal are contract fields in the preset, so a server that pages by token, by offset or by time window is configuration (FR-SRC-02).

Coverage is computed from what was read, never from counts the server claims. The rule `returned == read` that failed in the predecessor does not exist here: a page with fewer rows than requested is complete if the contract's end-of-data signal says so, and otherwise the next page is requested.

### 6.5 Paging and step-down

The pager requests `page_size` rows. On a truncation signal (a contract-declared marker, a response over the transport's size cap, or a transport error the contract classifies as `too_large`), it halves `page_size` and retries the same position, down to `min_page_size`. Below that, the unit is `partial` with its retry position. Budgets (`max_pages`, `max_seconds`) end collection as `partial`, never as `failed` (FR-SRC-08).

### 6.6 Outcomes and isolation

Each source, and each unit, ends with one outcome: `read`, `empty`, `partial(position)`, `failed(cause)`, `unavailable(reason, instructions)` or `skipped(policy)`. Sources run under a fixed global collection limit of 8. `transports.<id>.concurrency` limits a shared transport, and `sources.<id>.budget.concurrency` limits work within a source. An exception in one source is caught at its boundary, recorded with its raw message preserved, and never cancels another (FR-SRC-06). Errors keep the original message and status; Docket never replaces a transport error with a generic one.

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

`check_auth` runs the configured check command or token probe with no TTY, a closed stdin and a timeout (default 10 s). Docket never runs a command that can open a browser or prompt; `doctor` flags a check or refresh command that is interactive by running it with `DOCKET_NONINTERACTIVE=1` and a closed stdin and treating a timeout as interactive. An auth failure makes the source `unavailable` with the instructions from configuration, and the run continues (FR-SRC-12). The next step becomes `wait` with reason `sources_unavailable` only when `sources.<id>.required` is true.

### 6.9 Unit registry and classification

For `units` sources, discovered units are upserted into the `units` table. Classification rules in `signals.units` assign `kind`, `class` and `tier` from name patterns, membership counts and unit type. `docket units list` shows the durable registry and its classifications. The `classify` task kind ships, but automatically issuing it for unclassified units is deferred with FR-SRC-11.

### 6.10 Resolve

`resolve(ref)` maps a URL or key from a task line to the source that owns it (by the source's declared `resolve.patterns`) and returns `state`, `last_activity`, `last_actor` and `title`. Task verification (§10.5) uses it. A ref no source claims is reported as `unresolvable`, not as an error.

### 6.11 Context records

A source with the `context` capability emits records whose mapping targets `context` instead of `item`. Context records feed journal templates and never enter the item pipeline (FR-SRC-09).

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

- **Item id**: `sha256(source, unit, record identity)[:16]`, stable across generations and runs.
- **Thread id**: the source's thread key, or the item id for unthreaded records.
- **Topic id**: the merge key across sources. Computed from, in order of preference, an explicit reference the item carries (a ticket key, an incident id, a document URL), a thread id, or an agent-assigned topic from the `assess` task. References are extracted by `signals.references` patterns from configuration.

### 7.3 Merge

`merge` groups items by topic id, keeps the strongest assessment, unions evidence and sources, and records each merge in the item's trace. Suppression runs here: a topic with a live disposition (`dismiss` until a date, `watch` with no new activity, `done`) is suppressed with the disposition as its reason; a topic whose latest record is from the operator is marked `answered`; a topic already present in the task store (matched by its references) is marked `tracked` and routed to task verification rather than a finding (FR-SIG-07).

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
| `unanswered` | No reply from the operator in the thread for a duration |
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
    budget: { questions: 15, review_pool: 40 }
```

`score = sum(weight_k * component_k)` with components clamped to 0..100. `importance` and `urgency` are the rule contributions on a base of 50; `agent` is the assess task's score. Items below `floor` drop unless `surface: always`; items at or above `always_keep` bypass caps; per-source caps keep the top N by score. All numbers are configuration (FR-SIG-04, FR-SIG-05). The reference workspace sets the predecessor's values.

### 8.3 Coverage cross-references

A rule effect `expect_in: <source>` with a reference pattern records that the item names an identifier the home source should contain. After collection, Docket checks each expected identifier against the home source's records and adds a coverage note to the journal for every miss, with the naming item (FR-SIG-08). It does not mark the home source incomplete.

### 8.4 Traces and `item why`

Every stage appends to an item's trace: mapping (record ids), rule matches (rule id and effects), score components, cap decisions, merges, suppressions, review verdict, question id, answer and operations. `docket item why <id|url|text>` finds the item by id, URL or a unique text match across the run and prints the trace with rule ids and file positions (FR-SIG-09).

## 9. Agent tasks

### 9.1 Task kinds

A task kind is a named triple of input model, output model and instructions. Core kinds:

| Kind | Input | Output |
|---|---|---|
| `assess` | A batch of items with evidence, unit context, the operator's profile summary and the vocabulary | Per item: `keep`, `score` 0..100, `kind` (request, commitment, decision, risk, fyi, or a configured value), `owner`, `ask` (one-line imperative), `evidence` (verbatim quotes), `deadline`, `topic` |
| `extract` | A document (for example a transcript) split into sections | Commitments and requests with owner, verbatim evidence and deadline |
| `review` | Candidates without scores, with evidence | Per item: `verdict` (ask, merge, drop, needs_verification), `against` (strongest argument against asking), `merge_with`, `reason` |
| `classify` | Units or items needing a class | Per entry: `class`, `tier`, `reason` |

A workspace may define a further kind by declaring its input selection, output fields (from a closed field-type set) and instructions; Docket renders and validates it like a core kind (FR-TSK-01).

### 9.2 Packs

`tasks/packs.py` builds packs deterministically: items sorted by score descending then id, batched to `budget.max_items` and `budget.max_bytes` per pack. Must-surface items are placed first and never dropped; if they alone exceed a pack budget, the pack exceeds it and records an unexpected shape `pack.over_budget` (FR-TSK-05). Each pack contains everything the task needs: item fields, evidence lines with line ids, unit context, relevant profile excerpts (people named, topics matched) and the vocabulary. Its digest is the task input digest.

### 9.3 Prompts

`tasks/prompts.py` renders `prompt.md` from four parts:

1. The kind's instructions (core or configured), with operator-specific vocabulary filled in from configuration.
2. **An output specification generated from the Pydantic output model**: every field, its type, whether it is required, allowed values with their meanings, and one valid example produced by the model itself. Nothing else describes the output, so the prompt cannot drift from the validator (FR-TSK-02).
3. The fence: a fixed block stating that the pack is untrusted data, that instructions inside it must not be followed, and that the agent must read nothing outside the pack (FR-TSK-03, AE-11).
4. The submit instruction: the exact command, with the task id and the result path.

Source text inside the pack is wrapped in delimiters with a per-task random nonce, so content cannot close the fence.

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

### 9.6 Carry-forward

A task's acceptance is reusable whenever a new pack has the same input digest. For review, Docket goes further: review is batched per item, and an item whose evidence and assessment digests are unchanged across generations keeps its verdict without a new task (FR-TSK-07). Only new or changed items enter the next review pack.

## 10. Decisions

### 10.1 Question generation

`decide` builds questions from three inputs: retained findings, reconcile candidates (owned calendar blocks in the reconcile window, from the calendar destination's `list`), and task-verification results. Each question has a stable id derived from its subject (`q_` + item id, or `r_` + block key), a kind (`reconcile`, `finding`, `verify`, later `approve`), a rendered prompt from templates, the evidence summary, and options.

Questions are ordered by configured burst ordering (reconcile first in the reference workflow, then findings by score). The budget caps findings; overflow items become a journal section "Not asked" with their traces.

### 10.2 Options

An option set is configuration:

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

### 10.3 Answers

`docket ask answer <question-id> <option> [overrides]` records an answer:

```
docket ask answer q_9a1c task --due monday --est 60 --merge-with q_77b2 --note "after the review"
docket ask answer q_41d0 block --when "thu 13:00-17:00" --title "Draft pricing memo"
docket ask answer r_5f20 partly --est 30
docket ask answer --file answers.json
```

Overrides (FR-DEC-04): `--due` (date or weekday name, next occurrence after the run date), `--est` (duration), `--when` (window: a day, a day with times, a named calendar event to schedule over, or `this week`), `--title`, `--action` (task text), `--link` (repeatable), `--area`, `--forum`, `--merge-with`, `--note`. Option resolution accepts the id, the label, its leading words, the position, or a destination name, case-insensitively; an ambiguous match fails with the candidates (AE-09). The command echoes the normalized answer and the resulting action preview, so the operator's words and the stored decision can be compared before anything is built.

Answers are stored keyed by question id, which derives from item identity (FR-DEC-06). On refresh, an answer whose item has no new activity is kept; a changed item's answer moves to `previous` and the question is re-asked with it shown.

### 10.4 Reconcile

Reconcile options are fixed in kind and configurable in label: `done`, `partly` (requires `--est` for the remainder), `not_done`, `to_agenda`, `drop`. Each maps to task-store and calendar operations. Carry counts are a task field maintained by the task-store adapter. At `decisions.reconcile.carry_limit` (reference: 3), `not_done` is replaced by `rescope` and `drop` (FR-DEC-05).

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

Sandbox twins are the real adapter with its I/O layer swapped: Markdown adapters write to a copy of the files under the run's rehearse directory; calendar adapters operate on an in-memory calendar seeded from `snapshot()`. Because the twin runs the same preview, apply and verify code, a rehearsal exercises the code path the live run will take (FR-ACT-03).

### 11.2 Ownership

- **Calendar.** Every Docket block carries the marker from `destinations.<id>.marker`. `docket.destinations.calendar.owned_key()` is the single implementation that extracts and validates ownership from destination notes or properties. Planning and destination writes use this same scheme. Docket moves, updates and deletes only events carrying a valid marker for this workspace. It never sets attendees or recurrence; the adapter's operation model has no field for them (FR-ACT-04).
- **Markdown.** An update replaces exactly one line matching the operation's old-line digest within the anchor section. Zero matches or more than one match refuses that operation with `dest.line_changed` and the current text. Appends go to the end of the anchor section.

### 11.3 Built-in adapters

| Adapter | Behavior |
|---|---|
| `markdown_tasks` | A task store in Markdown. A configurable line grammar (`text/grammar.py`) parses and renders lines: checkbox, text, inline fields (`key:: value` or bracketed), links and tags. Sections and tiers are configured headings. Planning, reconciliation and task verification read open tasks through this adapter and its grammar. No second task parser exists. Quality rules (§11.5) run on every rendered line. |
| `markdown_agenda` | Agendas discovered by a path pattern or listed per forum. A configured section (for example `## Next`) with a capacity; dated history sections and unknown sections are preserved and tolerated. |
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

Deterministic greedy placement with bounded repair:

1. Gather open tasks through the configured `markdown_tasks` destination adapter and its line grammar, plus new tasks from answers.
2. Order: tasks with a deadline inside `imminent_days`, then overdue, then by due date, tier and size (configurable key list).
3. Batch tasks at or under `batch.max_minutes` into one daily batch block per day, filling in order (FR-PLN-02).
4. Keep existing owned blocks that are still legal (`check` returns nothing). Each kept block is an explicit `keep` operation.
5. Place each remaining task with `search`: earliest legal slot on or after its earliest start, before its due date if possible, otherwise the earliest legal slot in the horizon flagged `late`.
6. Repair: if a higher-priority task is unschedulable and moving at most `move_cap` lower-priority unlocked blocks frees a slot, move them. Each move is an explicit `move` operation.
7. Release owned blocks whose task is done, dropped or rescheduled, as explicit `release` operations. No owned block is left without an operation (FR-PLN-06).

Planning 200 tasks over 42 days runs in well under 5 seconds because `free_intervals` is computed once per day and updated incrementally on each placement (NFR-06).

### 12.4 Scheduling windows

An answer's `--when` becomes a window: a day, a day and time range, or a named event (resolved by title match within the horizon). Inside a window, `search` takes the estimate's length in the first stretch free of meetings, and treats the operator's own soft holds and out-of-office as available, which is how "schedule it over my flight" works without a hand edit (FR-PLN-04). A window that cannot fit the estimate fails at plan build with `plan.window_too_small`, naming the window's free minutes and the answer command that changes it.

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

## 13. Approve, apply and close

### 13.1 Approval

The approve question shows the plan diff path, the counts and the digest. `docket ask answer approve yes --digest <plan_digest>` binds the approval to that digest. Any change to an input digest makes the plan stale (§4.4) and the approval with it (FR-APP-01). Wall-clock build and approval times are appended to the `approvals` ledger outside the run artifacts. This keeps digest-bound artifacts deterministic.

### 13.2 Freshness

At approval, Docket snapshots each destination the plan touches and compares the snapshot digest with the plan's input digest for that destination. Equal: approved. Different: the approval is refused with `apply.stale_inputs`, the per-destination change (for example "2 calendar events added since build"), and `docket plan build` as the next command. No clock check exists; a delay of hours is fine if nothing changed. `journal.approval_delay_note` reads build and approval times from the `approvals` ledger (FR-APP-02).

### 13.3 Executor

`docket apply` executes operations in a fixed order (task store, agendas, calendar, messages, journal), each in its own step:

1. Look up `op.id` in receipts. A receipt with `verified: true` skips the operation.
2. `apply(op)` writes and returns a receipt with the external id. The receipt is committed before the next operation starts.
3. `verify(op, receipt)` reads back and records the result.

A crash at any point resumes from receipts without repeating a completed operation (FR-APP-03). An operation that fails is recorded with its raw error, later operations that do not depend on it continue, and the step returned is `wait` with reason `apply_partial` and the retry command. Dependencies are explicit in the operation (a calendar block for a new task depends on that task's line).

### 13.4 Cursor advancement

During collection, each source and unit writes a `cursor_proposal` with the contiguous coverage it proved, from the durable cursor forward. A proposal with a gap ends at the gap. `docket close` applies proposals in one transaction. Positions only move forward; a proposal behind the durable cursor is ignored and recorded (FR-APP-04). A run abandoned before close advances nothing, so the next run re-reads.

### 13.5 Journal and ledgers

The journal is rendered from configured sections, each a template over a closed context: `run`, `sources` (outcomes and coverage), `findings`, `questions`, `answers`, `operations`, `not_asked`, `dropped` (with reasons), `coverage_notes`, `context` (context records), `unexpected`. Ledgers are generic rows (`ledger`, `run`, `key`, `json`) that templates can aggregate across runs, for example a carries ledger or a surfaced-items audit.

### 13.6 Hooks

A hook is a command with arguments, a working directory, a timeout and a failure policy (`warn` or `fail`). Hooks run with a closed stdin, no TTY and a scrubbed environment that passes only declared variables. Events: `after_collect`, `after_apply`, `after_close`. A hook failure under `warn` is recorded and close completes. Each hook records a receipt, so a resumed close never repeats one (FR-APP-06).

### 13.7 Dispositions

Close writes a disposition per decided topic: `done`, `routed` (with the destination and operation id), `watch` (re-surface on new activity), `dismiss` (with an optional until date), `now` (with the recorded outcome). Merge (§7.3) reads them in the next run (FR-APP-05).

## 14. Robustness

### 14.1 Integrity checks and shape checks

Every check in Docket belongs to one of two classes, and the class is declared in code by the function used to raise it.

| Integrity (refuse: `integrity.*`) | Shape (tolerate: `unexpected()`) |
|---|---|
| Stage digest mismatch on an artifact a command consumes | A response field missing, renamed, or of a new value |
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

Each record has an area, a detail key (the shape, not the instance, so repeats aggregate), a redacted sample of at most 500 bytes, the reading taken and the extension point. Records go to the run's `unexpected.jsonl`, the `unexpected` table, the envelope's `unexpected` array of the command that hit them, the journal and rehearsal output. `docket unexpected [--since 7d]` aggregates unaccepted shapes by area and detail, most frequent first, with first and last seen and the extension point (FR-ROB-01, FR-ROB-02). `docket unexpected --all` includes accepted shapes. `docket unexpected accept <area> <detail> --reason ...` writes the `unexpected_accepted` table and prints the matching `shapes.accepted` configuration entry. Committed `shapes.accepted` entries make review durable and portable.

### 14.3 Rehearsal

`docket run rehearse [--run <id>] [--answers recommended|recorded|file:<path>]` copies the whole workspace database and the run directory into `runs/<id>/rehearse-<n>/state/`. It hard-links immutable blobs when the filesystem permits and copies them otherwise. It then swaps every destination for its sandbox twin and every source for replay over the run's recorded records, and drives the engine loop itself:

- delegated stages use accepted results where they exist, and otherwise build schema-valid conservative results from the task kind's own field and cross-field-rule declarations, labelled as synthetic;
- interactive stages use recorded answers, then each question's recommended option, then the first option;
- apply runs against twins, including verify;
- close runs against the copy, including hooks in `--dry-run` form where the hook declares one, and skipping others with a note.

The report lists each stage's outcome, every operation the live run would perform, every refusal with its code, every unexpected shape, and the elapsed time (FR-ROB-03). Nothing outside the rehearse directory is written. The reference workflow runs a rehearsal automatically at the evening pause, so a defect surfaces in the evening, not in the operator's morning.

### 14.4 Crash, refresh and change

All three reduce to §5.4: read the state, compute digests, recompute what is stale, reuse what is current, never cross the apply boundary backward. There is no separate resume code path to fall out of date (FR-ROB-04).

### 14.5 Unattended operation

Every subprocess Docket starts (transports, auth checks, adapters, hooks) runs with stdin closed, no controlling TTY, a timeout, and `DOCKET_NONINTERACTIVE=1`. A timeout reports the command and the elapsed time. `docket doctor` lists each configured subprocess and checks it in this mode (FR-ROB-06, NFR-05).

## 15. CLI

### 15.1 Command surface

| Group | Commands |
|---|---|
| Loop | `next [--full]` |
| Runs | `run abandon`, `run continue`, `run list`, `run rehearse`, `run resume`, `run start`, `run status`, `run steal` |
| Stages | `apply`, `close`, `collect`, `merge`, `plan build`, `plan diff`, `plan explain`, `plan load` |
| Tasks | `task issue`, `task kinds`, `task list`, `task show`, `task submit` |
| Decisions | `ask answer`, `ask list`, `ask resolve`, `ask show`, `ask undo` |
| Inspection | `item list`, `item why`, `unexpected`, `unexpected accept`, `units list` |
| Config | `config explain`, `config test`, `config validate`, `init`, `presets list`, `presets show` |
| Sources | `source list`, `source test` |
| Health | `doctor [--workflow <name>]` |
| Plugins and import | `import cursors`, `plugin check`, `plugin list` |
| Self-description | `help`, `schema`, `schema list`, `version` |

This table lists all 48 commands returned by `docket help --json`. `schema answers-file` is the self-described schema for `ask answer --file`; task kinds also add `<kind>-input` and `<kind>-output` schema names.

Every command takes `--json`, `--workspace` and `--run` where it applies. Every mutating command takes `--dry-run` and reports what it would do (AE-06).

### 15.2 Envelope

```json
{
  "ok": true,
  "command": "ask answer",
  "workspace": "/workspace",
  "run": "2026-10-16-daily",
  "data": { "...": "..." },
  "warnings": [ { "code": "decide.option_removed", "message": "..." } ],
  "unexpected": [ { "area": "...", "detail": "...", "reading": "...", "extend": "..." } ],
  "next": { "command": ["docket", "next", "--json"], "why": "3 questions remain" }
}
```

On failure, `ok` is false and `error` replaces `data`:

```json
{ "ok": false,
  "error": { "code": "apply.stale_inputs", "kind": "integrity",
             "message": "Calendar changed since the plan was built (2 events added).",
             "hint": "docket plan build", "retryable": true,
             "details": { "destination": "work-calendar", "added": 2 } },
  "next": { "command": ["docket", "plan", "build", "--json"], "why": "Rebuild against current calendar" } }
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
| 5 | external | `source.unavailable`, `transport.timeout`, `dest.apply_failed` |
| 6 | lease | `lease.held` |
| 7 | state | `run.none_open`, `run.sealed`, `stage.not_ready` |
| 70 | internal | `internal.bug` (with a traceback path, never a traceback on stdout) |

### 15.4 Doctor

`docket doctor` writes nothing and checks: configuration validity; state root writability and database version; each transport's binary or endpoint; each auth check in unattended mode; each destination's location, permissions and capability declarations; each hook's command; platform permissions an adapter declares. Each failure names the fix. `docket doctor --workflow <name>` limits source, transport, destination and hook checks to those used by that workflow.

### 15.5 Input normalization

`text/dates.py` and `text/durations.py` are the only parsers for dates, weekdays, windows and durations, and every command and answer uses them. Weekday names resolve to the next occurrence strictly after the run date, unless `today` or a date is given. Durations accept `45`, `45m`, `1h`, `1.5h`, `1h30m`. Normalized values are echoed (AE-09).

### 15.6 Speed

`docket next` and read-only commands load configuration from a cached resolved form keyed by file modification times and sizes, open the database read-only, and compute digests from stored input digests rather than rehashing blobs. The target is under 300 ms warm (AE-10). Import time is kept down by lazy imports of transports and adapters.

## 16. Security and privacy

- **Credentials.** Configuration names credential sources (an environment variable name, a file path, a command that prints a token). Values are read at use, passed to transports only, and never logged, stored in records, artifacts, envelopes or unexpected samples. A redaction filter on all output paths masks any value read from a credential source (NFR-01).
- **Untrusted content.** Source text appears to agents only inside fenced, nonce-delimited pack sections (§9.3). Docket itself never interprets source text as instructions: templates do not evaluate content, and a source field cannot name a command, path or destination.
- **Least effect.** Sources have no write path. Destinations write only owned objects (§11.2). Message destinations create drafts; sending is not implemented.
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
| Collection | Network-bound | `asyncio`, per-source and global concurrency, long-lived transport sessions |
| Assess pack build for 500 items | under 1 s | In-process, no subprocess |
| Plan build, 200 tasks, 42 days | under 5 s | Incremental free intervals per day (§12.3) |
| Full synthetic run in tests | under 60 s | Recorded transports, sandbox twins, synthetic task results |

No command launches a process per item (AE-10).

## 19. The reference workspace

The reference workspace models the predecessor as configuration. It lives in the operator's private configuration repository, outside the Docket repository (PRD §14.2), and uses only presets and configuration. PRD Appendix B maps every element. Three elements need care:

- **Chat over an MCP gateway's code-mode tool.** The `mcp/chat-codemode` preset ships the paging and projection script with no deployment-specific host or tool names; the workspace sets the server and tool names.
- **The native calendar bridge.** Wrapped through `calendar_exec` (§11.3). The bridge's existing verbs map one to one onto the exec protocol; a small shim translates its argument shapes. The shim lives with the workspace.
- **External state tools.** The predecessor kept watermarks in an external tool. Docket's database replaces it; a one-time `docket import cursors --from <json>` seeds cursors so the first Docket run does not re-read weeks of history.

## 20. Decisions and open questions

### 20.1 Decisions taken in this design

| Decision | Alternatives rejected | Reason |
|---|---|---|
| Digest-named artifacts replace generation bookkeeping | Generation numbers on every artifact | One rule detects every cross-generation mix (PRD §2.2). |
| Pydantic models as the single schema source | Hand-written JSON Schema, prompt text | Prompt, validator and `docket schema` cannot drift. |
| Closed predicate and effect sets with a plugin escape hatch | An expression language | Validatable, explainable, and resistant to creep (PRD §14.1). |
| A restricted template language | Jinja | Total rendering, no code execution, one page of docs. |
| Explicit stage commands plus `next` | `next` executing computational stages itself | The agent's transcript shows every effect; `next` stays read-only and fast. |
| Destinations build operations in core; programs only execute | Programs build their own operations | Ownership, redaction and quality rules stay in one place. |
| Synthetic conservative task results in rehearsal | Skipping delegated stages in rehearsal | Rehearsal must exercise decide, plan and apply on realistic shapes. |

### 20.2 Open questions

1. Should `docket next` be allowed to execute computational stages itself behind a flag (`--auto`), to halve round trips for agents that trust it?
2. Should the review task batch by topic cluster rather than by score order, to help the reviewer spot merges?
3. Should accepted unexpected shapes expire after a period, so a forgotten acceptance resurfaces?
4. Is SQLite enough for a later multi-workspace mode, or does that mode need a shared store?

## Appendix A: Requirement traceability

| PRD area | Design sections |
|---|---|
| FR-SRC-01..12 | §6 |
| FR-SIG-01..09 | §7, §8 |
| FR-TSK-01..07 | §9 |
| FR-DEC-01..08 | §10 |
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
| M1 | Built | Engine, stages, leases, workflows and the agent loop |
| M2 | Built | Collection, transports, cursors, contracts and generations |
| M3 | Built | Items, signals, scoring, merge and explanations |
| M4 | Built | Agent task kinds, packs, prompts and validation |
| M5 | Built | Questions, answers, reconciliation and verification |
| M6 | Built | Destination adapters, ownership, twins and conformance |
| M7 | Built | Planning, calendar classification, constraints and scheduling |
| M8 | Built | Approval, apply, receipts, close, journals and hooks |
| M9 | Built | Rehearsal, unexpected-shape review, doctor and agent polish |
| M10 | Not started | Requires the operator's live workspace and accounts |
| M11 | Not started | Requires shadow runs against the operator's live systems |
| M12 | Not started | Requires live cutover of the operator's accounts and calendars |
