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

The returned step has one of five kinds:

| Kind | Action |
|---|---|
| `run` | Run `step.command`. |
| `dispatch` | Complete each task from its pack and prompt, then run its submit command. |
| `ask` | Ask the questions and record answers with the provided template. |
| `wait` | Report the reason and stop until its condition is met. |
| `done` | Report the summary and stop. |

Use `uv run docket next --workspace ./workspace --full --json` when an ask step needs full evidence, refused options and exact commands for every option.

## Safety Model

- Docket makes no model calls. An agent completes self-contained task packs and submits typed results.
- Message actions create drafts. Sending is deferred.
- Approval binds to the exact plan digest and destination snapshots.
- Apply is idempotent. Every operation has a receipt and read-back verification.
- Sandbox runs use destination twins and write no durable cursors, dispositions or ledgers.
- `docket run rehearse` drives a copied run to completion with replayed sources and sandbox twins.
- Docket mutates only destination objects carrying its ownership marker.

## Configuration

A workspace contains `docket.yaml` and may include more YAML files. A conventional split is:

| File | Purpose |
|---|---|
| `docket.yaml` | Version, includes, presets and workflows |
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
