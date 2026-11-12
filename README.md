# Docket

Docket is a local, agent-driven CLI for turning signals from configured sources into reviewed, scheduled and recorded work. It stores raw evidence, issues typed judgment tasks, asks structured questions, builds a concrete plan and applies only an approved operation set. Docket does not call a model.

## Status

Milestones M0 through M9 are built. The reference workspace port, live shadow period and cutover are not started because they require an operator's live systems.

## Install

Requires Python 3.12 or later and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
uv run docket --help
```

## Quickstart

Create and validate a minimal workspace:

```sh
uv run docket init ./workspace
uv run docket config validate --workspace ./workspace
uv run docket run start daily --sandbox --workspace ./workspace
```

Drive the run by repeating:

```sh
uv run docket next --workspace ./workspace --json
```

`data.steps` lists every step that is ready now. Steps of nodes that do not depend on each other are ready together; do them together, then ask again. Each step names its node and has one of four kinds:

| Kind | Action |
|---|---|
| `run` | Run `step.command`. |
| `dispatch` | Complete each task from its pack and prompt, then run its submit command. |
| `ask` | Ask the questions and record answers with the provided template. |
| `wait` | Report the reason. This node and the nodes after it wait until its condition is met. |

When the run is sealed, `data.done` is true and `data.summary` describes the run.

Use `uv run docket next --workspace ./workspace --full --json` when an ask step needs full evidence, refused options and exact commands for every option.

## Graphs

Every run is a run of one graph. A graph is a set of named nodes, and each node does one thing, named by its verb:

```yaml
graphs:
  daily:
    read-mail: { read: mail }
    assess:    { judge: assess, from: [read-mail] }
    merge:     { merge: [assess] }
    decide:    { ask: default, from: [merge] }
    plan:      { plan: [decide] }
    approve:   { approve: plan }
    apply:     { apply: approve }
```

| Verb | The node | Done by |
|---|---|---|
| `read: <source>` | Reads one source from its cursor | Docket |
| `run: [argv]` | Runs a command once | Docket |
| `judge: <task kind>` | Has an agent judge what it works on | the agent |
| `merge: [nodes]` | Combines rules, scores and judgments into what is worth showing | Docket |
| `ask: <option set>` | Asks the operator to decide each item | the operator |
| `plan: [nodes]` | Turns decisions into operations | Docket |
| `approve: <plan node>` | Asks the operator to approve the plan, bound to its digest | the operator |
| `apply: <approve node>` | Writes the approved operations | Docket |
| `write: <action>` | Writes operations that need no approval, to a private destination | Docket |

`from` names the nodes whose output a node works on; `after` names nodes it must follow and that hand it nothing. A node is ready when every node it names is done. Every graph ends with a seal, which is not written in it: cursors advance, dispositions and the ledger are written, and the journal entry is made. `docket graph show daily` draws a graph: each node, who does its work, and what it waits for; `--run` marks a run's progress on it.

## Safety Model

- Docket makes no model calls. An agent completes self-contained task packs and submits typed results.
- Message actions create drafts. Sending is deferred.
- Approval binds to the exact plan digest and destination snapshots.
- Apply is idempotent. Every operation has a receipt and read-back verification.
- `docket run start <graph> --sandbox` binds every destination to its sandbox twin and writes nothing outside the run: no cursors, dispositions or ledger entries.
- Once an `apply` or `write` node begins writing, the nodes upstream of it are read as pinned. A later upstream change waits for the operator instead of moving the run backward.
- A `write` node needs no approval, so it may only target a private destination.
- Docket mutates only destination objects carrying its ownership marker.

## Configuration

A workspace contains `docket.yaml` and may include more YAML files. A conventional split is:

| File | Purpose |
|---|---|
| `docket.yaml` | Version, includes, presets and graphs |
| `profile.yaml` | Identity, relationships, topics, forums and calendar vocabulary |
| `sources.yaml` | Sources and transports |
| `signals.yaml` | Rules, scoring, budgets and unit classification |
| `destinations.yaml` | Adapters, templates, ownership and redaction |
| `planning.yaml` | Calendar provider, task-store mapping and scheduling policy |

`docket config validate` checks structure, references, semantics and capabilities. For example, `docket config explain profile.timezone` reports the resolved timezone and its origin.

## Extensibility

Docket supports three extension tiers:

1. Workspace configuration and shipped presets.
2. External programs that implement the versioned JSON-lines exec protocol.
3. Python plugins registered through the `docket.plugins` entry-point group.

Run `docket plugin check markdown_tasks` to exercise a built-in adapter against a disposable scratch target. Pass a configured destination id to check an external destination.

## Testing

```sh
make test-smoke
make test-fast
make test
make privacy
```

## Design Documents

- [Product Requirements](docs/Docket%20Product%20Requirements.md)
- [Technical Design](docs/Docket%20Technical%20Design.md)
- [Test Plan](docs/Docket%20Test%20Plan.md)
- [Implementation Plan](docs/Docket%20Implementation%20Plan.md)
