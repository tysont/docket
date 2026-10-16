# Docket: Product Requirements

**Status:** Draft v0.1 for review
**Date:** 2026-10-15
**Scope:** this document is normative for *what* Docket must do and *for whom*. `Docket Technical Design.md` is normative for *how*. Where they disagree, this document wins on intent and the design is corrected.

---

## 1. Summary

Docket is a command-line tool that an AI agent drives to turn the day's signals into decided, scheduled, recorded work. It reads from the places work arrives (chat, mail, calendars, tickets, documents, notes, metrics), finds what needs the user, asks the user a small number of well-formed decisions, plans the resulting work into the calendar and task list, applies the approved changes, and remembers what was decided.

Docket itself never calls a model. Every judgment (is this message a request, who owns this commitment, is this finding overstated) arrives from the agent as a typed answer to a task Docket issued, and every effect on the outside world happens only after the human approves the exact change.

Docket is the generic successor to an earlier personal workflow, referred to here as the predecessor. The predecessor proved the shape of the product. It also hardcoded the operator's identity, organization, note layout, calendar vocabulary and judgment, and it failed in a new place on most mornings of its final week. Docket keeps what the predecessor proved and applies the lessons from those failures.

## 2. Background: what the predecessor proved

### 2.1 What worked

Over its final week, the predecessor ran end to end on live data. On a working day it:

- read dozens of active chat spaces, direct conversations, hundreds of mails, meeting transcripts, an incident wiki, a ticket tracker, a metrics service, the calendar and a Markdown backlog within a few minutes;
- reduced those inputs to a small set of questions after an adversarial review pass;
- asked the user those questions, plus "did yesterday's planned blocks happen", in one burst with no source reads in between;
- wrote the answers into the backlog and meeting agendas, built a calendar plan under real constraints (work hours, deep-work window, meetings, out-of-office, daily caps, deadlines), showed the change set, and applied it on approval;
- advanced per-source read positions only across coverage it could prove, and wrote a day record and an audit of everything it surfaced or dropped.

The mechanisms that made this trustworthy are worth keeping exactly:

1. **Raw evidence first.** Every source read is persisted before anything interprets it. Every quoted line in a finding is checked verbatim against the raw read.
2. **Judgment is bounded.** Agents score from self-contained packs, cannot read outside them, and return typed records that the tool validates.
3. **An adversarial reviewer** sees each candidate without its score and must state the strongest argument against asking.
4. **Decisions happen in one burst.** No source reads between questions. The user decides; the tool writes.
5. **Every effect is bound to an approval.** The approval covers an exact, digest-bound change set. Execution is idempotent and receipted, so a crash resumes without duplicate events.
6. **Read positions advance only over proven coverage.** A gap holds the position; the next run re-reads.
7. **Privacy is structural.** Blocks on a shared calendar never name protected customers or sensitive subjects. Mail is drafted, never sent.
8. **Rehearsal.** The remaining steps of a live run can be driven end to end on a copy, against fake backends, before the user is asked anything.
9. **Integrity versus shape.** Digest and binding checks refuse. A data shape the code has not modelled takes the safe reading, continues, and records where the tooling should grow.

The last two mechanisms were added after repeated live-data failures and shaped this product's current form.

### 2.2 What failed

In its final week, live runs stopped or degraded in several distinct ways. They fall into a small number of classes:

| Class | Example |
|---|---|
| Exact checks on shapes that merely varied | A verb allowlist refused "Determine", "Notify", "Reconnect". A shape probe of one chat space failed when that space was a direct message with no display name. `returned == read` called a complete source incomplete. |
| State from one generation checked against another | After a refresh, plans built from the first read were validated against the second read's bindings, and refused. |
| Two implementations of one rule | The schedule builder and its validator encoded placement separately and disagreed about whether a slot was free. |
| Hand edits of machine artifacts | "Monday", "60 minutes", "merge these two" and "over the flight" had nowhere to go in an answer, so the agent edited plan JSON by hand, which nothing could verify. |
| Prompt and validator drift | Agents were told one schema and checked against another, and wrote the wrong source name, object-shaped evidence, or a missing owner. |
| A code path meeting live data for the first time in the user's morning | Finalize crashed on a title-only update; a recurring calendar series arrived with eight-week spans; a deleted personal event's tombstone broke a count. |
| Hardcoded judgment | Every chat message from a direct conversation was weighted as a peripheral channel, because the only weight table was for named channels. |

Every one of these was fixed, and each fix was correct. The pattern is the problem: the code encoded one person's data in specific shapes and treated any variation as an error. A generic product must be built the other way round.

### 2.3 What this means for Docket

- **Nothing user-specific in code.** Identity, people, groups, topics, protected entities, note layouts, calendar vocabulary, source endpoints, weights, vocabularies and templates are configuration.
- **Robust to new shapes by construction.** Configuration is strict and validated up front. Data is tolerant: unknown shapes take a declared safe reading and are recorded with the extension point that would model them.
- **One implementation per rule.** A constraint is checked by the same code whether Docket is building a plan or validating one.
- **The CLI owns the procedure.** The agent never needs a long document of command ladders. It asks Docket what to do next and gets one concrete step.
- **Answers carry everything the user says.** No user decision ever requires editing an artifact by hand.
- **Rehearsal and sandboxes are part of the product,** not a test harness.

## 3. Goals and non-goals

### 3.1 Goals

- **G1. Generic.** Model the predecessor workflow completely through configuration, and model a materially different workflow (different role, different tools) with zero code changes. See §12.
- **G2. Agent-native.** Be the most ergonomic possible CLI for an AI agent: discoverable, self-describing, deterministic, with structured output, actionable errors, and a single "what next" entry point.
- **G3. Safe by construction.** No external effect without approval of the exact change. No send by default. No mutation of anything Docket does not own. No step that can block on a person while unattended.
- **G4. Robust.** A run completes in the presence of data shapes nobody anticipated, partial source failures, crashes, refreshes and mid-run code or configuration changes. Unanticipated shapes become a visible, prioritized extension list.
- **G5. Fast for the human.** On a normal day the human spends ten minutes or less answering questions and approving the plan.
- **G6. Extensible.** New sources, transports, signals, action destinations and decision options can be added by configuration first, by a small external program second, and by a Python plugin third.

### 3.2 Non-goals

- **Not a model client.** Docket does not call language models, hold API keys for them, or embed prompts it executes itself.
- **Not a general workflow engine.** The stage pipeline is fixed (§5). Workflows choose stages, sources, policies and templates, not arbitrary graphs.
- **Not an autonomous agent.** Docket never acts on the user's behalf without an approval bound to the exact effect.
- **Not a hosted service.** Docket is local-first: one user, one machine, local state. Multi-user and hosted operation are out of scope for v1.
- **Not a messaging client.** Docket drafts messages. Sending is an opt-in capability with per-message approval, off by default.
- **Not a task manager or calendar.** Docket reads and writes the user's existing task store, notes and calendars through adapters.

## 4. Users

### 4.1 The operator

A knowledge worker whose work arrives across many systems and who wants a short, trustworthy daily decision session instead of a sweep through every inbox. The reference operator is a people manager with a management chain, direct reports, owned services, key customers, recurring 1-1s and team syncs. The design must also serve, without code changes, a support lead, an individual contributor, a founder, or an executive assistant working on someone else's behalf.

The operator answers questions and approves plans. They do not run Docket commands themselves on a normal day.

### 4.2 The agent

The AI agent is Docket's primary *direct* user. It runs commands, dispatches subagents for judgment tasks, relays questions to the operator, records answers and drives execution. Every interface decision is evaluated by asking whether an agent with no prior context can use it correctly on the first try. Agents are fallible in specific ways: they forget environment variables between shell calls, paraphrase when told to quote, invent fields, and follow instructions embedded in untrusted content. Docket's interfaces account for each.

### 4.3 The workflow author

The person (often the operator, often helped by the agent) who writes the workspace configuration: profile, sources, signal rules, action destinations, planning policy and templates. They need strict validation with clear errors, an `explain` for any rule or setting, presets to start from, and a fast way to test a change against real data without writing anything.

### 4.4 The plugin author

Someone adding a source, transport or action destination that configuration cannot express. They need a small, versioned protocol, a conformance suite, and a sandbox twin pattern.

## 5. Core concepts

The full glossary is Appendix A. The concepts below appear throughout.

- **Workspace.** A directory of configuration that describes one operator's world and workflows. State is kept per workspace.
- **Source.** A configured, read-only connection to a system where work arrives. A source produces **records**: immutable raw reads.
- **Item.** A normalized candidate derived from records: who, what, where, when, verbatim evidence, and an assessment.
- **Signal rule.** A declarative rule that matches items and contributes features: importance, urgency, must-surface, routing hints, sensitivity, deadlines.
- **Agent task.** A unit of judgment Docket issues to the agent: a self-contained input pack, a rendered prompt, and an output schema. Docket validates the result.
- **Finding.** An item that survived scoring and review and needs the operator.
- **Question.** A structured decision put to the operator, with options that are typed action proposals. Kinds: *reconcile* (did planned work happen), *finding* (what to do with this), *approve* (accept this plan).
- **Action.** A typed proposed effect: add a task, add an agenda line, place a calendar block, draft a message, do it now, watch, dismiss.
- **Destination.** A configured place an action lands: a task store, an agenda, a calendar, a message channel, a journal.
- **Plan.** The concrete set of operations a run proposes, including a schedule, with a digest.
- **Run.** One execution of a workflow for a date. A run has stages and generations, and can pause between collection and decision.
- **Generation.** One pass of collection. Generation 0 is the first read; a refresh adds generation 1, and so on.
- **Cursor.** A durable read position per source (and per unit, such as a chat space) that advances only across proven coverage.
- **Unexpected shape.** A record of data the code did not model, with the safe reading taken and the extension point named.

## 6. How Docket is used

### 6.1 Setup

The operator or agent runs `docket init`, chooses presets (for example a Google Workspace preset, a Markdown task-store preset and a calendar preset), and fills in the profile: identity and handles, people and relationships, groups and their meeting forums, topics the operator owns, protected entities, and how their calendar titles map to availability. `docket doctor` checks configuration, credentials, binaries and adapter permissions without writing anything, and names each failure and its fix. `docket source test <id>` reads a small sample from a source and shows what Docket made of it.

### 6.2 The daily run

The reference workflow splits a day into two sessions.

**Evening (start).** The agent runs `docket next` in a loop. Docket collects every source, issues assessment tasks, which the agent runs as parallel subagents, merges the results, issues a review task, drafts the questions and a preliminary plan, rehearses the rest of the run against sandboxes, and pauses. No questions are asked yet.

**Morning (finish).** `docket next` resumes the run, refreshes the volatile sources incrementally, re-assesses only what changed, carries forward review verdicts for unchanged items, and returns one batch of questions: reconcile yesterday's blocks, decide the findings. The agent relays them; the operator answers in their own words and the agent records structured answers ("due Monday, 60 minutes, merge with the other one, schedule it over my flight"). Docket builds the plan, shows the change list, and on approval applies it, records receipts, advances cursors and writes the journal.

### 6.3 Other runs

A workspace can define other workflows from the same stages. A **triage** run reads chat, direct messages and mail, asks about anything urgent, routes to the task store, and schedules nothing. A **weekly** run might reconcile the whole task store and agendas. Workflows share sources, profile and rules.

### 6.4 The agent loop

The agent's entire procedure is:

```
loop:
  step = docket next --json
  match step.kind:
    run      -> execute step.command
    dispatch -> run step.tasks as subagents (at most step.concurrency at once), submit each result
    ask      -> put step.questions to the operator, record answers with docket ask answer
    wait     -> report step.reason and step.instructions to the operator; stop
    done     -> report step.summary; stop
```

Every step names the command to run and why. A failed command returns a machine-readable error with a code, a one-line cause and the next command to try.

## 7. Functional requirements

Requirements are numbered for traceability. **Must** is required for v1; **Should** is required for v1 unless explicitly deferred in the implementation plan; **May** is optional.

### 7.1 Sources

- **FR-SRC-01 (Must).** A source is configured, not coded. Its configuration declares the system, the transport, the record kind, how to page, how to resume (cursor model), which fields are read, and how fields map to item fields.
- **FR-SRC-02 (Must).** Supported cursor models: *snapshot* (re-read the whole thing each generation), *incremental* (read records newer than the cursor), *units* (discover units such as chat spaces, then read each unit incrementally with its own cursor), *id-scan* (read every id not yet processed), and *cadence* (read only on configured days).
- **FR-SRC-03 (Must).** Supported transports: an MCP client (stdio and streamable HTTP), MCP code-mode (a server exposing a code-execution tool, used to project results server-side and stay under response-size caps), a local executable speaking Docket's JSON-lines protocol, HTTP JSON, and local files.
- **FR-SRC-04 (Must).** Vendor specifics (tool names, argument shapes, pagination fields, field paths) ship as **presets**: data files with no user information. A workspace extends presets and overrides any field.
- **FR-SRC-05 (Must).** Every raw read is persisted, unmodified, before interpretation, with the query, the interval and the outcome.
- **FR-SRC-06 (Must).** One source's failure never discards another source's results. Each source reports its own outcome: read, empty, partial (with the retry position), failed (with cause), unavailable (with the auth or network reason), or skipped (by policy).
- **FR-SRC-07 (Must).** A source declares the fields it reads and which are optional. A missing optional field is not drift. A missing required field or a type change is recorded as an unexpected shape, with the extension point; it fails only that source.
- **FR-SRC-08 (Must).** Paging steps down on truncation (smaller pages) before failing, and long collections page to exhaustion within a configurable budget.
- **FR-SRC-09 (Must).** Sources may emit **context records** that inform the journal but never become items (for example service-health snapshots).
- **FR-SRC-10 (Should).** A source may implement **resolve**: given a URL or reference from a task, return its current state (open or closed, last activity, last author). Docket uses this to verify open tasks.
- **FR-SRC-11 (Should).** A source may implement **unit discovery with classification**: a durable registry of units (for example chat spaces) with a tier or class, assigned by rules and refined by agent tasks.
- **FR-SRC-12 (Must).** Authentication never opens a browser or prompts. A source declares how to check and refresh credentials (an MCP token, an executable). An auth failure makes the source *unavailable* with the configured instructions, and the run continues.

### 7.2 Items and signals

- **FR-SIG-01 (Must).** Records become items through a source's mapping. An item has a stable identity derived from where it was found and a topic identity used to merge the same matter across sources.
- **FR-SIG-02 (Must).** Signal rules are declarative, with a closed set of predicates (source, sender relationship, recipient position, mentions of the operator, people, groups, topics or entities, text and field patterns, age, unanswered state, thread ownership, field values) and a closed set of effects (importance and urgency contributions, must-surface, route hint, area or topic, sensitivity class, deadline extraction, tags, dismiss with reason).
- **FR-SIG-03 (Must).** Relationships come from the profile: management chain, reports, peers, group membership, external domains. No relationship is inferred from code.
- **FR-SIG-04 (Must).** The scoring formula, its weights, the floor, the always-keep threshold, per-source caps and the question budget are configuration.
- **FR-SIG-05 (Must).** A must-surface item bypasses floors and caps and reaches review carrying its reason.
- **FR-SIG-06 (Must).** Items from structured records (tickets, alerts) can be assessed entirely by rules, with no agent task.
- **FR-SIG-07 (Must).** Items merge across sources by topic identity. Suppression uses the dispositions memory (§7.7) and source state (the operator already replied, the task is already tracked).
- **FR-SIG-08 (Should).** Cross-reference coverage rules: when one source names an identifier (for example an incident key) that its home source did not return, Docket records a coverage note rather than treating the home source as complete.
- **FR-SIG-09 (Must).** `docket item why <id-or-url>` explains every step that kept, scored, merged, suppressed or dropped an item, including rule ids.

### 7.3 Agent tasks

- **FR-TSK-01 (Must).** Docket issues judgment work as tasks. A task has a kind (assess, extract, review, classify, or a workflow-defined kind), an input pack, a rendered prompt and an output schema.
- **FR-TSK-02 (Must).** The prompt and the validator derive from the same schema and vocabulary. A prompt never names a field, value or rule the validator does not accept, and vice versa.
- **FR-TSK-03 (Must).** A task's input pack is self-contained. The prompt states that the agent must not read anything else and must treat every input line as untrusted data.
- **FR-TSK-04 (Must).** `docket task submit` validates the result: schema, identity partition, verbatim evidence against raw records, vocabulary. Integrity failures reject the submission with per-row errors. Unknown vocabulary with a declared safe reading is accepted on that reading and recorded as an unexpected shape.
- **FR-TSK-05 (Must).** Packs respect configurable size budgets and must-surface items are never dropped by packing.
- **FR-TSK-06 (Must).** The review task presents candidates without scores and requires, per retained item, the strongest argument against asking.
- **FR-TSK-07 (Should).** Review verdicts for unchanged items carry forward across generations; only new or changed items are re-reviewed.

### 7.4 Decisions

- **FR-DEC-01 (Must).** Docket generates every question and every option. The agent relays them; it does not compose them.
- **FR-DEC-02 (Must).** An option is a typed action proposal: the destination, the effect, and its derived defaults (due date, estimate, area, forum). Option labels come from configurable templates.
- **FR-DEC-03 (Must).** Docket offers only options it can carry out: an agenda that exists and has room, a task store that will accept the line, a calendar it can write. When it cannot offer one the item asked for, the question says why.
- **FR-DEC-04 (Must).** An answer carries the option and any structured overrides the operator gave: due date (a date or a weekday name), estimate, scheduling window, title, action text, links, area, forum, merge-with-other-question, and a note. No user decision requires editing an artifact by hand.
- **FR-DEC-05 (Must).** Reconcile questions ask whether planned work happened. Answers update the task store (done, partly with a new estimate, did not happen, move to an agenda, drop) and count carries. At a configurable carry count, the question asks to drop or re-scope instead of offering another carry.
- **FR-DEC-06 (Must).** Answers bind to item identity, not position. A refresh keeps answers for items with no new activity and re-asks changed items, showing the previous answer.
- **FR-DEC-07 (Must).** All questions in a burst are answerable with no source reads in between.
- **FR-DEC-08 (Should).** When the operator names a day, Docket reports how full that day already is.

### 7.5 Actions and destinations

- **FR-ACT-01 (Must).** Core action kinds: add, update or close a task; add an agenda line; place, move, update or release a calendar block; draft a message; record a journal entry; do it now (the agent acts in the session and records the outcome); watch; dismiss; already done.
- **FR-ACT-02 (Must).** Each destination is configured with an adapter, its location, its format and its policies. Built-in adapters: a Markdown task store with a configurable line grammar and quality rules; Markdown agendas with a configurable section and capacity; calendars through a local executable adapter, a file adapter, and an MCP adapter where the server supports writes; message drafts through MCP; a Markdown journal.
- **FR-ACT-03 (Must).** Every adapter implements preview (the exact operations an action will produce), apply (idempotent by operation id), and verify (read back). Every adapter ships a sandbox twin with the same behavior and no external effect.
- **FR-ACT-04 (Must).** Docket mutates only what it owns. Calendar blocks carry an ownership marker and never carry attendees or recurrence. Task and agenda edits replace exactly one matching line or append; a line that changed since the plan was built is refused, not overwritten.
- **FR-ACT-05 (Must).** Messages are drafts unless a destination's policy enables sending, and then each send needs approval of that exact message.
- **FR-ACT-06 (Must).** Destinations declare visibility. On a shared destination, protected entities, people's names in titles, figures and sensitive subjects are redacted by configurable templates, and the full detail stays in the private record.
- **FR-ACT-07 (Must).** Task-store quality rules (an opening imperative, a length limit, evidence, a primary link, a due date, an estimate, an area) are configurable. Open vocabularies use a denylist of non-matches plus a safe default, never an allowlist.

### 7.6 Planning

- **FR-PLN-01 (Must).** Docket schedules open tasks into calendar blocks under a configurable policy: workdays, day window, deep-work window, protected lunch, granularity, minimum and maximum block, meeting buffer, daily caps, horizon, move cap, move lock, ordering (imminent deadlines first, then overdue, then by due date, tier and size).
- **FR-PLN-02 (Must).** Small tasks batch into a configurable daily block.
- **FR-PLN-03 (Must).** Calendar availability comes from configurable title and property rules that map each event to a kind (meeting, focus, soft hold, out of office, boundary, mirrored personal event, owned block, informational). Unknown kinds are treated as busy and recorded.
- **FR-PLN-04 (Must).** A task may carry a scheduling window. The block takes the estimate's length in the first stretch of the window free of meetings and may sit on the operator's own holds or out-of-office time.
- **FR-PLN-05 (Must).** One constraint engine builds and validates plans. A built plan always validates; an "unschedulable" claim is verified by the same search.
- **FR-PLN-06 (Must).** Existing owned blocks are kept, moved, updated or released explicitly. No block is left orphaned by a plan.
- **FR-PLN-07 (Should).** Personal-calendar mirroring: opaque busy holds copied from one calendar to another, reconciled each run, honoring the operator's deletions.
- **FR-PLN-08 (Must).** `docket plan diff` shows every change in human terms, and `docket plan explain <item>` says why an item landed where it did or why it could not be placed.

### 7.7 Apply, close and memory

- **FR-APP-01 (Must).** Approval binds the exact plan digest. Approval is invalidated by any change to the plan's inputs.
- **FR-APP-02 (Must).** Freshness is checked against state, not a clock: at approval, Docket compares the live calendar and task store with what the plan was built from, approves if unchanged, and refuses with a rebuild instruction if changed. A configurable note records any delay.
- **FR-APP-03 (Must).** Apply is idempotent and resumable. Each operation writes a receipt. A crash resumes from receipts.
- **FR-APP-04 (Must).** Close advances cursors only across proven, contiguous coverage, writes the journal from a configurable template, appends ledgers, and runs configured hooks (for example a commit or a reindex).
- **FR-APP-05 (Must).** A dispositions memory records every decision (done, watch, dismiss, routed) so the next run suppresses or re-surfaces correctly.
- **FR-APP-06 (Must).** Close is resumable after a partial failure and never repeats a completed step.

### 7.8 Robustness

- **FR-ROB-01 (Must).** Integrity checks (digests, bindings, identity partitions, receipts) are exact and refuse. Shape checks are tolerant: the code takes a declared safe reading, continues, and records an unexpected shape with the area, the detail, a sample and the extension point.
- **FR-ROB-02 (Must).** Unexpected shapes appear in the journal, in `docket unexpected` (aggregated across runs, most frequent first), in rehearsal output and in test reports.
- **FR-ROB-03 (Must).** `docket run rehearse` drives the remainder of a live run on a copy of its state, with every destination swapped for its sandbox twin and recommended answers for unanswered questions, and reports what the real run will do, what would fail, and what shapes are unexpected. Nothing live is written.
- **FR-ROB-04 (Must).** Refresh, resume and a mid-run code or configuration change are the same operation: recompute stale derived stages from immutable raw reads and recorded answers. Completed effects are never recomputed.
- **FR-ROB-05 (Must).** A run holds a lease. A crashed owner's lease is adoptable; a live foreign owner requires an explicit steal.
- **FR-ROB-06 (Must).** Nothing Docket runs unattended can block on a person: no browser, no keychain prompt, no permission dialog. `docket doctor` detects each such dependency up front.

## 8. Agent ergonomics

These requirements carry the same weight as the functional ones.

- **AE-01 (Must). One entry point.** `docket next` returns the single next step for the current run: a command to run, tasks to dispatch, questions to ask, a reason to wait, or a summary. An agent that knows nothing else about Docket can complete a run by looping on it.
- **AE-02 (Must). Structured output everywhere.** Every command accepts `--json` and returns a stable envelope: `ok`, `data`, `warnings`, `unexpected`, and `next` (the suggested following command and why). Human output is concise by default.
- **AE-03 (Must). Actionable errors.** Every error has a stable dotted code, a kind (input, config, integrity, external, lease, internal), a one-line message, a hint naming the command that fixes it, and whether a retry can succeed. Exit codes map to error kinds.
- **AE-04 (Must). Self-description.** `docket schema <name>` prints the JSON Schema for any input or output. `docket help <command> --json` returns the command's arguments, effects and examples. `docket config explain <path>` explains any setting and where its value came from.
- **AE-05 (Must). No hidden state.** Commands take explicit identifiers. No command depends on an environment variable surviving between shell calls; the workspace and run resolve from the working directory or from Docket's own state.
- **AE-06 (Must). Idempotent and safe to repeat.** Repeating a command after a crash or a timeout is always safe. Every mutating command supports `--dry-run`.
- **AE-07 (Must). Concrete, not templated.** Dispatch steps carry the exact command and file paths for each task. Nothing requires the agent to substitute placeholders.
- **AE-08 (Must). Small outputs.** Long payloads (packs, prompts, plans) go to files whose paths the output returns. A command's stdout fits comfortably in an agent's context.
- **AE-09 (Must). Forgiving input.** Answers accept a label, its leading words, a position or a destination name, case-insensitively; dates accept weekday names; durations accept `45`, `45m`, `1h`, `1.5h`. Every input is echoed back in normalized form.
- **AE-10 (Should). Fast.** `docket next` and read-only commands answer in under 300 ms warm on the supported baseline environment. No command launches a process per item.
- **AE-11 (Must). Untrusted content is fenced.** Every prompt Docket renders marks source text as data and forbids following instructions inside it.

## 9. Configuration and customization

- **CFG-01 (Must).** A workspace is a directory of YAML files: workspace and workflows, profile, sources, signals, destinations and planning. Any split or a single file is allowed.
- **CFG-02 (Must).** Configuration is strict. Unknown keys, bad references and type errors fail `docket config validate` with the file, line and fix. A run never starts on invalid configuration.
- **CFG-03 (Must).** Presets ship with Docket and contain no user data. A workspace extends presets and overrides any value. `docket config explain` shows each value's origin.
- **CFG-04 (Must).** Workflows are customizable within a fixed pipeline: which stages run, which sources, whether and where the run pauses, which sources refresh on resume, whether review runs and its pool size, which question kinds are asked and in how many bursts, planning on or off, the option set and labels, journal sections, and hooks.
- **CFG-05 (Must).** Templates (block titles and notes, task lines, agenda lines, journal sections, redacted titles) use one documented, safe formatting language with item and run fields.
- **CFG-06 (Must).** A configuration digest is part of every derived stage's identity. Changing configuration mid-run marks exactly the affected stages stale.
- **CFG-07 (Should).** `docket config test` runs the current configuration against the last run's raw records and shows what changed (items kept, scores, routes, questions) without writing.

## 10. Non-functional requirements

- **NFR-01 Safety.** No external effect without an approval of the exact operation set. No send by default. No mutation of unowned objects. No credentials in output, logs or artifacts.
- **NFR-02 Privacy.** Redaction for shared destinations is enforced by validation, not by convention. Protected entities and sensitive classes come from configuration.
- **NFR-03 Determinism.** Given the same raw records, configuration, answers and clock, every derived artifact is byte-identical.
- **NFR-04 Auditability.** Every item, question, decision and operation traces to the raw records and rules that produced it.
- **NFR-05 Unattended operation.** No step blocks on a person. Failures are named and fast.
- **NFR-06 Performance.** Collection is network-bound and concurrent within configured limits. Planning a 42-day horizon with 200 open tasks takes under 5 seconds. A full synthetic run in the test suite takes under 60 seconds.
- **NFR-07 Portability.** macOS is the primary supported platform; Linux is also supported. Platform-specific adapters (for example a native calendar bridge) are optional and isolated.
- **NFR-08 Testability.** The full pipeline runs offline against sandbox twins and recorded transports. Tests never touch live state or real credentials.
- **NFR-09 Compatibility.** Configuration, artifact schemas and the plugin protocol are versioned. Docket reads the previous minor version of each.

## 11. Success metrics

| Metric | Target |
|---|---|
| Operator time per day (answering and approving) | 10 minutes or less on a normal day |
| Runs that hit a code defect after a passing evening rehearsal | 0 over 10 consecutive working days |
| User-specific literals in Docket's source tree | 0, enforced by an automated litmus test (§12) |
| Second-persona run with zero code changes | Passes end to end in the test suite |
| Shadow parity with the predecessor over 5 working days | Same questions for at least 90% of the predecessor's findings, every difference explained by `item why` |
| Unexpected-shape extension latency | Each recurring shape modelled or explicitly accepted within 3 working days |
| Human edits of Docket artifacts | 0; every user decision expressible in an answer |

## 12. The litmus test

Two conditions, both automated in the test suite:

1. **Nothing hardcoded.** The Docket source tree contains no literal from the reference operator's world: no names, handles, email domains, organization or product names, note-repository names, folder names, calendar titles, glyph conventions, hosts or service vocabularies. The test extracts every identifier from the reference workspace configuration (kept outside the Docket repository) and asserts none appears under `src/`. A second, static list covers conventions such as the reference calendar's title words.
2. **Everything modellable.** The reference workflow (Appendix B) runs end to end as configuration only, and a second, materially different persona (different role, different chat and ticket tools, different task-store grammar, different calendar conventions) runs on the same code.

## 13. Release scope

### 13.1 v1

Everything marked Must, the Should items not deferred by the implementation plan, the reference workflow ported as a workspace, the second persona as a test fixture, and a shadow period against the predecessor before cutover.

### 13.2 Later

- Docket as an MCP server, exposing commands as tools.
- Additional transports and presets (Slack, Linear, GitHub, Outlook).
- Multi-workspace runs (one operator, two organizations).
- An executive-assistant mode acting for another principal.
- Learning from dispositions: proposing rule changes from repeated "dismiss" answers.

## 14. Risks and open questions

### 14.1 Risks

- **Rule language creep.** A declarative rule language invites endless predicates. Mitigation: a closed predicate set, an escape hatch through plugin predicates, and a bar of "two workspaces need it" before a predicate joins the core.
- **MCP variance.** Servers differ in pagination, size caps and auth. Mitigation: transports are generic, quirks live in presets, and shape contracts record drift instead of failing runs.
- **Native permission prompts.** OS-level calendar access can prompt once and hang unattended. Mitigation: `doctor` checks permission state; the native bridge is optional.
- **Agent variance.** Different agents follow prompts differently. Mitigation: schema-derived prompts, strict integrity validation, tolerant vocabulary with recorded unexpected shapes.
- **Porting effort.** The reference workflow contains many small judgments. Mitigation: shadow runs with per-item diffs before cutover.

### 14.2 Open questions

1. The reference workspace lives in the operator's private configuration repository, outside the Docket repository.
2. Is a native calendar bridge shipped with Docket, or is the existing bridge wrapped through the executable adapter protocol? This document assumes the latter for v1.
3. Should sending be possible at all in v1, or deferred entirely? This document includes it behind policy and per-message approval.
4. How much of the chat-space registry (classification of hundreds of spaces) belongs in v1?

---

## Appendix A: Glossary

| Term | Meaning |
|---|---|
| Action | A typed proposed effect: task add/update/close, agenda line, calendar block, message draft, journal entry, now, watch, dismiss, done. |
| Adapter | Code that reads and writes a destination or reads a source through a transport. Every write adapter has a sandbox twin. |
| Agent task | A unit of judgment issued to the agent: input pack, prompt, output schema. |
| Answer | The operator's decision on a question: an option plus structured overrides. |
| Context record | A record that informs the journal but never becomes an item. |
| Cursor | A durable read position per source, or per unit within a source. |
| Destination | A configured place actions land, with an adapter, format, visibility and policies. |
| Disposition | A remembered decision about an item, used to suppress or re-surface it later. |
| Finding | An item retained after scoring and review that needs the operator. |
| Forum | A recurring meeting with an agenda, attached to a person or group. |
| Generation | One collection pass within a run. |
| Item | A normalized candidate derived from records. |
| Must-surface | A rule effect that exempts an item from floors and caps. |
| Plan | The concrete operation set and schedule a run proposes, with a digest. |
| Preset | A shipped configuration fragment with no user data. |
| Profile | The operator's identity, people, groups, topics, protected entities, forums and calendar vocabulary. |
| Question | A structured decision with typed options: reconcile, finding or approve. |
| Receipt | Proof that an operation was applied, keyed by operation id. |
| Record | An immutable raw read from a source. |
| Rehearsal | Driving the rest of a live run on a copy, against sandbox twins. |
| Run | One execution of a workflow for a date. |
| Signal rule | A declarative match and effect over items. |
| Source | A configured read-only connection to a system. |
| Stage | One step of the fixed pipeline: collect, assess, merge, review, decide, plan, approve, apply, close. |
| Unexpected shape | A recorded instance of data the code did not model, with the safe reading taken and the extension point. |
| Unit | A sub-collection of a source with its own cursor, such as a chat space or a conversation. |
| Workflow | A named selection of stages, sources, policies and templates. |
| Workspace | A directory of configuration describing one operator's world. |

## Appendix B: Reference workflow mapping

Every element of the predecessor workflow, and where it lives in Docket. "Profile", "Sources", "Signals", "Destinations", "Planning" and "Workflow" are workspace configuration; "Preset" is shipped data; "Core" is generic code with no user data.

| Predecessor element | Docket home |
|---|---|
| The operator's name, aliases, chat user id, email, ticket handle, metrics-system aliases | Profile: `me` with per-system handles |
| Management chain, direct reports, key ICs, adjacent-team contacts | Profile: `people` with `reports_to` and roles; relationship sets derived in Core |
| Owned services and their aliases, product areas, area taxonomy | Profile: `topics` with aliases, `area`, `owned` |
| Team syncs and the areas they cover; 1-1s per person | Profile: `forums` attached to groups and people, with agenda locations and calendar title matches |
| Strategic customers | Profile: `entities` with `protected` and `strategic` flags |
| Chat via an MCP gateway's code-mode tool, spaces, threads, membership discovery | Preset: chat-over-MCP-code-mode source with the *units* cursor model; Sources: instance and token location |
| Direct messages weighted by counterpart, not channel | Signals: rules keyed on `from.relation` for direct-conversation units |
| Channel tiers and room classes (alerts, bots, announcements) | Signals: unit classification rules by name pattern; Sources: unit registry |
| Must-surface: chain on an owned topic, customer signal, direct ask, user-started thread, compliance notice | Signals: rules with `surface: always` |
| Mail query, automated-mail classes, compliance senders and subjects | Preset: mail-over-MCP source; Signals: suppression and must-surface rules |
| Meeting transcripts from a document store, owner prefixes, filename conventions | Preset: documents source with *id-scan*; Sources: filename and owner-prefix patterns; Agent task kind `extract` |
| Incident reports from a wiki by search terms | Preset: wiki search source; Sources: terms derived from profile topics |
| Incident ids named in chat but missing from the wiki | Signals: cross-reference coverage rule |
| Customer escalation tickets, priority and status routing | Preset: ticket source; Signals: rules-only assessment with field predicates on priority and status |
| Service-health metrics on two weekdays | Preset: HTTP JSON source with *cadence*, context records |
| Backlog in a Markdown file with inline fields, tiers, sections | Destinations: Markdown task store with line grammar, sections and tiers; quality rules |
| Backlog verification against linked tickets and threads | Core: task verification through sources' `resolve` |
| Agendas with a capped "Next" section, dated history, extra sections tolerated | Destinations: Markdown agendas with section, capacity and discovery pattern |
| Calendar title vocabulary (focus, holds, out of office, boundaries, mirrored busy) | Profile: `calendar.classify` rules |
| Native calendar writes through a native calendar bridge | Destinations: calendar through the executable adapter protocol, wrapping the existing bridge |
| Block title glyph, opaque key suffix, note structure, ownership marker | Destinations: calendar block templates and marker format |
| Sensitive-subject triggers and redacted titles | Signals: sensitivity rules; Destinations: redaction templates per sensitivity class |
| Personal calendar mirrored as opaque busy holds | Planning: `mirrors` |
| Work hours, deep window, lunch, buffers, caps, batching, horizon, move lock, ordering | Planning: policy |
| Score formula, floor, overflow, caps, question budget, review pool | Signals: `scoring` |
| Gate A answers, Gate B option labels, "Already handled", "Handle now" | Workflow: decision option templates |
| Structured answer fields (due, estimate, window, merge, links) | Core |
| Watermarks in an external state tool, unit watermarks, processed document ids | Core: state database |
| Dispositions ledger, prior decisions | Core: dispositions memory |
| Day record, surfaced-items audit, coverage and run ledgers | Destinations: journal templates; Core ledgers |
| Search reindex and note-repository commit after finalize | Workflow: close hooks |
| Credential refresh through a local auth tool and a browser-free token check | Sources: auth check and refresh commands |
| Evening start, morning finish, refresh on resume | Workflow: `pause_before: decide`, `refresh_on_resume` |
| Rehearsal, unexpected shapes, leases, receipts, integrity checks | Core |
