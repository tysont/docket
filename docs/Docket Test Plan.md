# Docket: Test Plan

**Status:** Draft v0.1 for review
**Date:** 2026-10-15
**Scope:** how Docket is verified, from unit tests to the shadow period. TD refers to `Docket Technical Design.md`, IP to `Docket Implementation Plan.md`.

---

## 1. What the tests must prove

The predecessor had a large passing test suite and still failed in a new place on most mornings of its final week. The tests checked the shapes the code expected. The failures came from shapes it did not expect, from state crossing generations, and from code paths that met live data for the first time in the operator's morning. Docket's test plan is built against those three causes:

1. **Variation, not examples.** Fixtures are generated with systematic variation (missing optional fields, unknown enum values, empty units, nameless units, tombstones, long spans), and every test asserts that variation produces a safe reading plus a recorded unexpected shape, not a failure (§5.6).
2. **Invariants, not cases.** Properties that must hold for every input are tested as properties: a built plan validates, derived artifacts are deterministic, prompts and validators agree, integrity checks refuse (§5).
3. **Live data before the morning.** Rehearsal on a copy of a live run is a required verification step, not an optional tool (§8).

## 2. Isolation rules

Every test obeys these, and `tests/conftest.py` enforces them with an autouse fixture that fails the test on violation:

- A fake `HOME` and a fake state root under the test's temporary directory.
- Credential variables stripped from the environment. The fixture removes every variable whose name matches the credential patterns, and a sentinel check fails any test that reads a real credential path.
- No network. A socket guard blocks connections except to `127.0.0.1` servers the test started.
- Stub binaries first on `PATH` for every external program a configuration names.
- A pinned clock (`--now` or the run's clock pin). No test reads the wall clock for derived output.
- Subprocesses launched only by tests of transports, adapters and hooks, and only to stubs. Every other path runs in process (`docket.cli.main(argv)`) to avoid repeated process-launch overhead.

## 3. Tiers and layout

| Tier | Target | Contents | Budget |
|---|---|---|---|
| Smoke | `make test-smoke` | Unit tests of the touched package, litmus, envelope contract | 20 s |
| Fast | `make test-fast` | All unit, contract and property tests; one scenario per persona | 90 s |
| Full | `make test` | Everything, including all scenarios, crash injection, conformance, performance | 5 min |
| Rehearse | `make rehearse-fixtures` | Rehearsal of each persona fixture's recorded live-like run | 2 min |

Run the cheapest tier that covers a change while iterating, `test-fast` before claiming anything works, and `test` before a commit to main.

```
tests/
  unit/<package>/          one directory per src package
  contract/                envelope, error codes, schemas, CLI help
  property/                hypothesis-based invariant tests (§5)
  conformance/             transport and adapter suites (§7)
  scenario/                end-to-end runs per persona (§6)
  litmus/                  §4
  perf/                    §10
  fixtures/
    personas/reference-like/   workspace, recorded transports, task results, answers
    personas/support-lead/     same, materially different
    shapes/                    variation generators
```

## 4. The litmus test

Two automated checks, both in every tier (PRD §12).

**4.1 Nothing hardcoded.** `tests/litmus/test_no_literals.py` collects forbidden literals from three sources:

1. A static list in the test of conventions from the reference world: calendar title words, glyph conventions, block key formats, note-repository folder names, organization and product names, hosts and service vocabularies. The list is reviewed when the reference workspace changes.
2. Every identifier in the reference workspace configuration when it is available (`DOCKET_LITMUS_WORKSPACE`, set in the operator's environment and in the shadow-period job): names, aliases, handles, domains, paths, title patterns, tool names, template literals.
3. Every identifier in the two persona fixtures, which must not leak into `src/` either.

It scans `src/docket/` including `presets/`, case-insensitively, on word boundaries, and reports each hit with file, line and the source list. Generic words that collide (for example "focus" as a classification kind) are allowed only through an explicit, commented allowlist entry naming the generic meaning.

**4.2 Everything modellable.** The full scenario suite (§6) passes for both personas with no persona-specific code, and the reference workspace passes a full rehearsal on recorded live data (§8).

## 5. Invariant tests

### 5.1 Integrity versus shape in code

`tests/contract/test_raise_discipline.py` parses every module under `src/` and fails on a `raise` inside a function that handles source, agent or destination data, unless the raised type is an `IntegrityError` subclass with a code listed in the error catalog, or a `ConfigError` raised during configuration loading. Data-handling functions are identified by their package (`sources`, `transports`, `items`, `signals`, `tasks`, `decide`, `destinations`, `plan`, `apply`, `close`) and a decorator for exceptions. This is the automated form of TD §14.1.

A second check asserts that every `unexpected()` call names a non-empty `extend`.

### 5.2 Determinism

For each persona, a scenario runs twice in separate state roots with the same records, configuration, answers and clock, and every artifact under `runs/` is compared byte for byte (NFR-03). A third run with dictionary orderings shuffled (by `PYTHONHASHSEED` and by reversing fixture list order) must produce the same digests.

### 5.3 Prompt and validator parity

For each task kind, including a workspace-defined kind from each persona:

- every field name and enumerated value that appears in the rendered prompt's output specification is accepted by the validator;
- every field the validator accepts appears in the prompt;
- the example embedded in the prompt passes validation;
- a result generated from the JSON Schema by Hypothesis either validates or fails with an integrity code, never with an internal error.

This is the regression class "prompt and validator drift" (PRD §2.2) made impossible to reintroduce.

### 5.4 One constraint engine

Hypothesis generates calendars (meetings, holds, out-of-office, recurring series, multi-day spans, tombstones), policies within valid ranges and task sets, then asserts:

- every block in a built plan has no violations under `check`;
- every task the plan calls unschedulable has `search` returning unschedulable with the same reason;
- no owned block in the input lacks an operation in the output;
- plan build is deterministic.

### 5.5 Integrity refusals

One test per integrity code in the catalog proves it refuses, with the documented code, kind, exit status and hint: stale artifact digest, approval digest mismatch, task identity partition, evidence not verbatim, receipt payload conflict, Markdown line changed, cursor regression, redaction leak.

### 5.6 Shape tolerance

`tests/fixtures/shapes/` holds variation generators over each persona's recorded responses: drop each optional field, drop each required field, replace each enum with an unknown value, change each field's type, empty each collection, remove display names, add unknown fields, inject tombstones and long recurring spans. For every variant, the test runs collection and the downstream stages and asserts:

- the run continues past the affected record or unit;
- the expected safe reading was taken;
- exactly one unexpected-shape record per distinct shape, with area, detail and extension point;
- unaffected sources and units have identical outputs to the unvaried run.

A variant that makes a run stop is a defect, unless the change hits an integrity field, in which case the test asserts the integrity code instead.

## 6. Scenario tests

Each scenario runs in process against a persona fixture, using recorded transports and sandbox twins.

**6.1 Agent loop.** `tests/agent_loop.py` is a minimal agent: it loops on `docket next --json`, runs `run` steps, answers `dispatch` steps from recorded task results (or synthetic results), answers `ask` steps from a scripted answer file, and stops on `wait` or `done`. It reads no Docket documentation and uses only fields from the step. Every scenario below drives Docket through it, which proves AE-01 on every run.

**6.2 Scenario list.**

| Scenario | Proves |
|---|---|
| Evening start, pause, morning finish with refresh | Workflow pause, `refresh_on_resume`, incremental reads, review carry-forward, answers kept for unchanged items |
| Triage workflow | A second workflow from the same stages, no planning |
| One source failing, one unavailable, one partial | Isolation, outcomes, cursor holding per source and unit |
| Kill after every command and repeat it | AE-06 and FR-ROB-04 |
| Crash after every apply operation | Receipts, no duplicate effect, resume |
| Configuration edit mid-run (weights, then planning policy) | Partial digests stale exactly the declared stages |
| Code schema bump mid-run | Stage schema versions recompute exactly the affected stages |
| Calendar change between plan build and approval | State-based freshness refusal and rebuild |
| Backlog line edited after plan build | `dest.line_changed`, others applied |
| Every structured answer override | FR-DEC-04 with no hand edits |
| Window over an out-of-office event | FR-PLN-04 |
| Carry limit reached | Rescope options replace not done |
| Two concurrent agents | Lease held, dead-owner adoption, explicit steal |
| Shared calendar with a protected entity | Redaction, leak refusal |
| Hook failure under warn | Close completes, no repeat on resume |
| Untrusted content with embedded instructions | Fence present with nonce; content cannot close it; no instruction text reaches a command or path |

Every scenario runs for both personas unless it is persona-specific by nature, and the list is the minimum for v1.

## 7. Conformance and recorded transports

**7.1 Recorded transports.** A recorder wraps any transport during an authorized live session in the operator's environment and writes request and response pairs, with credentials and configured private fields redacted, to a fixture directory. A replay transport serves them by request digest. Fixtures for the reference workspace stay outside the Docket repository; persona fixtures in the repository are invented.

**7.2 Transport conformance.** For each transport: handshake, paging to exhaustion, step-down on truncation, timeout behavior, raw error preservation (the original message and status reach the outcome), and no credential in any output.

**7.3 Adapter conformance.** For each destination adapter and its twin (TD §17.3): preview determinism, idempotent apply under repeated operation ids, verify after apply, refusal of unowned objects, twin parity, and capability declarations matching behavior. External programs run the same suite through `docket plugin check`.

## 8. Live verification

Unit, property and scenario tests cannot see the shapes in tomorrow's live data. Two practices cover that gap.

**8.1 Rehearsal as a gate.** A change to any stage, adapter or preset is not verified until `docket run rehearse` has run on a copy of a recent live run of the reference workspace and reported no refusal and no new unexpected shape that the change should have handled. The reference workflow also rehearses automatically at each evening pause (TD §14.3), and a failing evening rehearsal is triaged before the morning session.

**8.2 Config test as a gate.** A change to the reference workspace configuration is not verified until `docket config test` has shown its effect on the last run's raw records and the differences are the intended ones.

## 9. Traceability

`tests/traceability.yaml` maps every PRD requirement id to at least one test id. A test in the contract tier fails when a PRD requirement marked *Must*, or *Should* and not deferred in IP §5, has no mapped test, or a mapped test does not exist. Requirement ids are parsed from the PRD itself, so adding a requirement without a test fails the build.

## 10. Performance tests

| Test | Budget | Source |
|---|---|---|
| `docket next` warm, mid-run, reference-like persona | under 300 ms median of 20 | AE-10 |
| Plan build, 200 tasks, 42 days | under 5 s | NFR-06 |
| Full synthetic run, each persona | under 60 s | NFR-06 |
| Pack build, 500 items | under 1 s | TD §18 |
| No process per item | Counted: subprocess launches during a full run stay constant as the item count grows from 50 to 500 | AE-10 |

Performance tests run in the full tier and report their timings; a budget miss fails the test.

## 11. Shadow parity (M11)

During the shadow period (IP M11), `docket shadow compare --date D` (a test utility under `tests/tools/`, not a product command) reads the predecessor's day record and audit and the Docket sandbox run for the same date, and reports:

- findings asked by both, by one only, and why (from `item why` and the predecessor's audit);
- route and option differences for shared findings;
- planned block differences by day;
- unexpected shapes recorded by Docket.

The cutover gate is IP M11's exit criterion. Each daily report is kept in the reference workspace's journal folder.

## 12. Regression classes

Every change is checked against the predecessor's regression checklist. The inherited classes and the tests that guard each are listed below:

| Class | Guard |
|---|---|
| Exact check on a varying shape | §5.1, §5.6 |
| State from one generation checked against another | Digest-named artifacts (TD §4.4); §6.2 refresh and mid-run edit scenarios |
| Two implementations of one rule | §5.3, §5.4 |
| Hand edits of machine artifacts | §6.2 structured answer scenario; no command accepts an edited artifact without its digest |
| Prompt and validator drift | §5.3 |
| First contact with live data in the morning | §8.1 |
| Hardcoded judgment | §4 |
| A fix that breaks a neighbor | Full tier before commit; traceability (§9) |
