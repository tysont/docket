# Docket: Implementation Plan

**Status:** M0-M9 built; M10-M12 not started
**Date:** 2026-10-16
**Scope:** the order in which Docket is built, what each milestone must prove before the next starts, how the reference workflow moves from the predecessor to Docket, and which PRD *Should* items are deferred. Section numbers prefixed TD refer to `Docket Technical Design.md`; TP refers to `Docket Test Plan.md`.

---

## Status

M0 through M9 are built. M10 (reference workspace port), M11 (shadow period) and M12 (cutover) have not started because they run against the operator's live accounts and calendars.

## 1. Approach

Five rules shape the sequence.

1. **The walking skeleton first.** By the end of M2, a synthetic run goes from `docket run start` to `docket close` through every stage, with most stages trivial. Each later milestone deepens a stage behind an interface that already exists end to end. Integration problems surface in week one, not at the end.
2. **The litmus test from the first commit.** The test that scans `src/` and `presets/` for reference-world literals (TP §4) lands in M0, before any domain code. It cannot be retrofitted cheaply.
3. **Two personas from the start.** Every milestone's exit criterion runs against two fixture workspaces: *reference-like* (a people manager with chat, mail, transcripts, tickets, a Markdown backlog, agendas and a bridge calendar) and *second persona* (a support lead with a different chat tool, a ticket queue as the main source, a different task grammar and ICS calendars). Fixture data is invented. A feature that only works for one persona is not done.
4. **Mechanism before vocabulary.** Every domain module is built against the rule and template interfaces, never against a specific word list. Presets come after the mechanism they configure.
5. **Live data before the operator's morning.** From M9 on, every change is rehearsed against a copy of a live run (TD §14.3) before it is relied on.

## 2. Repository setup

Repository conventions:

```
docket/
  docs/          the four design documents
  adr/           decision records, NNNN-title.md
  src/docket/    package (TD §2.3)
  tests/         unit, contract, conformance, scenario, litmus
  tests/fixtures/personas/{reference-like,support-lead}/
  temp/          scratch, gitignored
  pyproject.toml, uv.lock, Makefile, .gitignore
```

`Makefile` targets provide these tiers: `test-smoke`, `test-fast`, `test` (full), `lint`, `litmus`, `rehearse-fixtures`. Commits are local until the operator names a remote. The first ADRs record the decisions in TD §20.1.

## 3. Milestones

Estimates are in focused working sessions (about three hours each, agent-assisted). They are planning aids, not commitments.

### M0. Foundation (3 sessions)

**Build:** package skeleton, `uv` project, CLI command registry, the envelope and error model (TD §15.2, §15.3), `docket schema`, `docket help --json`, `docket version`; configuration loader with presets, origins and the four validation passes (TD §3); `config validate` and `config explain`; the template language (TD §3.6); date and duration parsers (TD §15.5); the state root, database with migrations, blob store (TD §4).

**Tests first:** envelope and exit-code contract, configuration error messages with file and line, template totality, parser tables, litmus scanner with a seeded literal list.

**Exit:** both persona workspaces validate; a deliberate unknown key, bad reference and bad template each fail with file, line and fix; `docket schema list` prints every model; litmus passes.

### M1. Engine and walking skeleton (3 sessions)

**Build:** run creation and clock pin (TD §4.6), stage protocol and digests (TD §4.4, §5.1), `docket next` with all five step kinds (TD §5.2), leases (TD §5.5), workflows (TD §5.6), stub implementations of every stage that pass inputs through, the `files` transport, the in-memory calendar twin and a minimal Markdown task twin.

**Exit:** for both personas, an agent loop script (`tests/agent_loop.py`, TP §6) reaches `done` on a fixture day by looping only on `docket next`. Editing configuration mid-run stales exactly the declared stages. Killing the process between any two commands and repeating the last command is safe.

### M2. Collection (5 sessions)

**Build:** `Source` and `Transport` interfaces (TD §6.2, §6.3); transports `exec_jsonl`, `http_json`, `mcp_stdio`, `mcp_http`, `codemode`; cursor models (TD §6.4); pager with step-down (TD §6.5); outcomes and isolation (TD §6.6); response contracts and `unexpected()` (TD §6.7, §14.2); auth checks in unattended mode (TD §6.8); unit registry with rule classification (TD §6.9); context records; `docket source test`; `docket collect [--refresh]`; generations (TD §4.5); cursor proposals.

**Tests first:** transport conformance against recorded fixtures; a contract test for each documented variation (missing optional, unknown enum, type change); each cursor model's coverage proof, including the case the predecessor got wrong (fewer rows than requested on a complete page).

**Exit:** both personas collect from recorded transports with one source failing, one unavailable and one partial, and the other sources complete; `docket unexpected` lists the injected shapes with extension points.

### M3. Items, signals and merge (4 sessions)

**Build:** mapping and identity (TD §7.1, §7.2), relationship sets, the rule engine with the full predicate and effect sets (TD §8.1), scoring and caps (TD §8.2), coverage cross-references (TD §8.3), merge and suppression (TD §7.3), traces and `docket item why` (TD §8.4), `docket config test` (CFG-07).

**Exit:** the reference-like persona's rule file expresses every predecessor signal listed in PRD Appendix B; `item why` explains a kept, a dropped, a merged and a suppressed item with rule ids; `config test` shows a score change after a weight edit without writing.

### M4. Agent tasks (4 sessions)

**Build:** task kinds `assess`, `extract`, `review`, `classify` and workspace-defined kinds (TD §9.1); packs with budgets (TD §9.2); schema-derived prompts and the fence (TD §9.3); `task submit` validation (TD §9.4); carry-forward (TD §9.6); dispatch steps.

**Tests first:** a prompt/validator parity test that renders each kind's prompt and checks that every field and value it names is accepted by the validator and every accepted field is named (TP §5.3); submissions that exercise each refusal and each tolerated vocabulary value.

**Exit:** a scripted agent submits results for both personas, including one wrong source name, one paraphrased quote, one missing owner and one unknown kind value; the first three refuse with per-row fixes, the last is accepted on its safe reading and recorded.

### M5. Decisions (3 sessions)

**Build:** question generation and ordering (TD §10.1), option sets and guards with `can_accept` (TD §10.2), answers with every override and forgiving resolution (TD §10.3), reconcile with carry limits (TD §10.4), task verification through `resolve` (TD §10.5), day load (TD §10.6), answer binding across refresh.

**Exit:** every structured answer from the predecessor's final-week runlog that forced a hand edit ("Monday", "60 minutes", "merge these two", "over the flight") is expressible as one `docket ask answer` command and appears correctly in the action preview.

### M6. Destinations (4 sessions)

**Build:** the `Destination` interface (TD §11.1); `markdown_tasks` with the line grammar and quality rules (TD §11.3, §11.5); `markdown_agenda`; `journal`; `calendar_file`; `calendar_exec`; sandbox twins for each; ownership (TD §11.2); redaction with leak validation (TD §11.4); the conformance suite and `docket plugin check` (TD §17.3).

**Exit:** each adapter passes conformance, including twin parity; a shared-calendar operation that would leak a protected entity is refused; a backlog line edited after plan build is refused, not overwritten.

### M7. Planning (5 sessions)

**Build:** calendar classification (TD §12.1); the constraint engine (TD §12.2); the scheduler with batching, keep, move cap and release (TD §12.3); windows (TD §12.4); mirrors (TD §12.5); plan artifact, `plan diff`, `plan explain` (TD §12.6).

**Tests first:** a property test that every built plan passes `check` and every unschedulable claim is confirmed by `search` (TP §5.4); the recurring-series and tombstone cases from the predecessor's final week.

**Exit:** 200 tasks over 42 days plan in under 5 seconds; both personas' calendars classify with no unexpected shapes except the injected one; "schedule it over my flight" places a block on an out-of-office event.

### M8. Approve, apply and close (4 sessions)

**Build:** approval binding and state-based freshness (TD §13.1, §13.2); executor with receipts and verify (TD §13.3); cursor advancement (TD §13.4); journal and ledgers (TD §13.5); hooks (TD §13.6); dispositions (TD §13.7); `message_mcp` drafts.

**Exit:** a crash injected after each operation resumes with no duplicate effect; a calendar change between build and approval refuses with the rebuild command; a source with a gap holds its cursor while others advance; a hook failure under `warn` does not block close and is not repeated on resume.

### M9. Robustness and agent polish (3 sessions)

**Build:** `docket run rehearse` (TD §14.3); `unexpected accept`; `docket doctor` complete (TD §15.4); performance work to the TD §18 budgets; `help errors`; the static raise check (TP §5.1); `docket import cursors`.

**Exit:** both personas rehearse from the evening pause to close in under 60 seconds with nothing written outside the rehearse directory; `docket next` answers in under 300 ms warm on the supported baseline environment; a fresh agent session with only `docket help --json` completes a fixture run.

### M10. Reference workspace port (4 sessions)

**Build:** the reference workspace in the operator's private configuration repository, outside the Docket repository: profile, sources, signals, destinations, planning, workflows and templates from PRD Appendix B; the calendar bridge shim for `calendar_exec`; presets `mcp/chat-codemode`, `mcp/mail`, `docs/transcripts`, `wiki/search`, `tickets/jira-like`, `http/health`, `markdown/tasks-inline-fields`, `markdown/agendas`, `calendar/exec-bridge`. Presets carry no deployment-specific hosts or names.

**Process:** port one source at a time. For each, run `docket source test` against live data, then `docket config test` against a recorded predecessor day, and compare items with the predecessor's candidates for that day. Every difference is either fixed or explained in a port log under `temp/` and summarized in an ADR.

**Exit:** `docket doctor` is clean in the operator's environment; a live evening run reaches the pause and its rehearsal passes; the litmus test, fed the reference workspace's identifiers, passes.

### M11. Shadow period (5 working days, about 1 session per day)

Both systems run each day. The predecessor stays authoritative and is the only one that writes. Docket runs the same day's workflow with every destination in sandbox mode (`--sandbox` on the run), and answers are copied from the predecessor's recorded answers where the questions match.

Daily, a parity report (TP §11) compares findings, questions, routes and planned blocks. Each difference gets one of: fixed in Docket, fixed in configuration, accepted as an improvement, or accepted as a regression with an owner.

**Exit (cutover gate):** the PRD §11 parity target holds (at least 90% of the predecessor's findings asked, every difference explained by `item why`); zero code defects reached a morning after a passing evening rehearsal on the last three days; no open unexpected shape older than three working days.

### M12. Cutover (1 session)

Docket becomes authoritative with live destinations. The predecessor's entry points are changed to point at Docket and then disabled, not deleted. Cursors are seeded from the predecessor's final watermarks with `docket import cursors`. The predecessor remains in the operator's private configuration repository for 30 days as a fallback, then is removed in one commit.

**Rollback:** re-enable the predecessor's entry points. Both systems read the same sources and write the same backlog grammar and calendar marker format (the reference workspace's templates match the predecessor's formats exactly), so either can take over from the other's state. The cursor export is the only manual step and is one command.

## 4. Sequencing and parallelism

```
M0 -> M1 -> M2 -> M3 -> M4 -> M5 -> M8 -> M9 -> M10 -> M11 -> M12
                    \-> M6 ----/  /
                    \-> M7 ------/
```

M6 (destinations) and M7 (planning) depend on M1's interfaces only, and can run in parallel with M3 to M5. Following the delegation rule, each parallel implementer gets its own `git worktree` and branch, and merges are done by the coordinating session after re-running `make test-fast`. M2 transports can also split across worktrees by transport.

Total: about 47 sessions of build plus the shadow week.

## 5. Deferred PRD items

The PRD defines 90 requirements. Tests map every *Must* and every non-deferred *Should*. Two requirements remain deferred:

| Item | Decision | Reason |
|---|---|---|
| FR-ACT-05 sending | Deferred to after v1. v1 creates drafts only; `policy.send` is rejected by validation with a "not in v1" message. | PRD open question 3. Drafts cover the reference workflow; sending adds approval surface for little gain. |
| FR-SRC-11 agent classification of units | Rule classification in v1; the `classify` task kind ships, but its use for units is deferred to v1.1. | PRD open question 4. Rules covered the predecessor's space tiers; agent classification is an improvement, not a parity need. |
| FR-PLN-07 mirrors | In v1. | The reference workflow depends on it. |
| FR-SIG-08 coverage cross-references | In v1. | Small, and the predecessor relied on it for incidents. |
| FR-TSK-07 review carry-forward | In v1. | The morning refresh depends on it for speed. |
| FR-DEC-08 day load | In v1. | Falls out of the constraint engine. |
| CFG-07 config test | In v1. | Central to the port process (M10). |
| AE-10 speed | In v1, measured in M9. | |

## 6. Risks to the plan

| Risk | Signal | Response |
|---|---|---|
| The rule language cannot express a reference judgment | M3 or M10 needs a predicate not in the set | Use a plugin predicate in the reference workspace; promote to core only when the second persona needs it too. |
| MCP gateway quirks consume M2 | Code-mode paging needs server-specific handling | Keep handling in the preset's script; the transport stays generic. |
| The calendar bridge needs permission prompts | `doctor` reports a non-prompting status verb missing | Add the status verb to the shim before M10; until then, the calendar runs from the file adapter in rehearsal. |
| Shadow parity stalls on judgment differences between agents | Parity under 90% with differences in assess, not rules | Compare against the predecessor's recorded assessments; diff prompts; accept improvements explicitly. |
| Scope pull from later items (MCP server mode, Slack) | Requests during build | Record in an ADR as later work; do not build before M12. |

## 7. Definition of done for v1

- Every PRD *Must* and every non-deferred *Should* is traced to a passing test (TP §9).
- Both personas pass the full scenario suite and the litmus test.
- The reference workspace has run as the authoritative system for five consecutive working days with no code defect reaching a morning.
- The four documents are updated to match what was built, and the ADRs record every deviation.
