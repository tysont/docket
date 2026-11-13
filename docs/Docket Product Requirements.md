# Docket: Product Requirements

**Status:** M0-M9 built
**Date:** 2026-10-16
**Scope:** this document is normative for *what* Docket must do and *for whom*. `Docket Technical Design.md` is normative for *how*. Where they disagree, this document wins on intent and the design is corrected.

---

## 1. Summary

Docket is a command-line tool that an AI agent drives to turn the day's signals into decided, scheduled, recorded work. It reads from the places work arrives (chat, mail, calendars, tickets, documents, notes, metrics), finds what needs the user, asks the user a small number of well-formed decisions, plans the resulting work into the calendar and task list, applies the approved changes, and remembers what was decided.

Docket itself never calls a model. Every judgment (is this message a request, who owns this commitment, is this finding overstated) arrives from the agent as a typed answer to a task Docket issued, and every effect on the outside world happens only after the human approves the exact change.

Docket is the generic successor to an earlier personal tool, referred to here as the predecessor. The predecessor proved the shape of the product. It also hardcoded the operator's identity, organization, note layout, calendar vocabulary and judgment, and it failed in a new place on most mornings of its final week. Docket keeps what the predecessor proved and applies the lessons from those failures.

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
5. **Every effect is bound to an approval.** The approval covers an exact, digest-bound change set. Execution is idempotent and receipted, so repeating it after a crash makes no duplicate event.
6. **Read positions advance only over proven coverage.** A gap holds the position; the next run re-reads.
7. **Privacy is structural.** Blocks on a shared calendar never name protected customers or sensitive subjects. Mail is drafted, never sent.
8. **Dry runs on live data.** A full run can be driven end to end against live reads and fake backends before the user is asked anything.
9. **Integrity versus shape.** Digest and binding checks refuse. A data shape the code has not modelled takes the safe reading, continues, and records where the tooling should grow.

The last two mechanisms were added after repeated live-data failures and shaped this product's current form.

### 2.2 What failed

In its final week, live runs stopped or degraded in several distinct ways. They fall into a small number of classes:

| Class | Example |
|---|---|
| Exact checks on shapes that merely varied | A verb allowlist refused "Determine", "Notify", "Reconnect". A shape probe of one chat space failed when that space was a direct message with no display name. `returned == read` called a complete source incomplete. |
| State from one read checked against another | Plans built from a first read were validated against a second read's bindings, and refused. |
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
- **The CLI owns the procedure.** The agent never needs a long document of command ladders. It asks Docket what to do next and gets every step that is ready, each concrete.
- **Answers carry everything the user says.** No user decision ever requires editing an artifact by hand.
- **Sandbox runs are part of the product,** not a test harness.

## 3. Goals and non-goals

### 3.1 Goals

- **G1. Generic.** Model the predecessor completely through configuration, and model a materially different operator (different role, different tools) with zero code changes. See §12.
- **G2. Agent-native.** Be the most ergonomic possible CLI for an AI agent: discoverable, self-describing, deterministic, with structured output, actionable errors, and a single "what next" entry point.
- **G3. Safe by construction.** No external effect without approval of the exact change. No send by default. No mutation of anything Docket does not own. No step that can block on a person while unattended.
- **G4. Robust.** A run completes in the presence of data shapes nobody anticipated, partial source failures, crashes, and mid-run code or configuration changes. Unanticipated shapes become a visible, prioritized extension list.
- **G5. Fast for the human.** On a normal day the human spends ten minutes or less answering questions and approving the plan.
- **G6. Extensible.** New sources, transports, signals, action destinations and decision options can be added by configuration first, by a small external program second, and by a Python plugin third.

### 3.2 Non-goals

- **Not a model client.** Docket does not call language models, hold API keys for them, or embed prompts it executes itself.
- **Not a general automation engine.** A graph is built from nine verbs that Docket defines (§5). A workspace composes nodes from them; it cannot add a verb.
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

### 4.3 The workspace author

The person (often the operator, often helped by the agent) who writes the workspace configuration: profile, sources, signal rules, action destinations, planning policy and templates. They need strict validation with clear errors, an `explain` for any rule or setting, presets to start from, and a fast way to test a change against real data without writing anything.

### 4.4 The plugin author

Someone adding a source, transport or action destination that configuration cannot express. They need a small, versioned protocol, a conformance suite, and a sandbox twin pattern.

## 5. Core concepts

The full glossary is Appendix A. The concepts below appear throughout. Work moves from Source to Item to Decision: a source yields records, records become items, and the operator decides each item that needs them.

- **Workspace.** A directory of configuration that describes one operator's world and graphs. State is kept per workspace.
- **Graph.** A named set of nodes that together do one kind of run, such as a daily run or a triage run.
- **Node.** One step of a graph. It does one thing, named by its verb: `read`, `run`, `judge`, `merge`, `ask`, `plan`, `approve`, `apply` or `write`. It names the nodes whose output it works on (`from`) and the nodes it must follow (`after`). Docket computes a node's work, an agent judges it, or the operator is asked.
- **Source.** A configured, read-only connection to a system where work arrives. A source produces **records**: immutable raw reads.
- **Item.** A normalized candidate derived from records: who, what, where, when, verbatim evidence, and an assessment.
- **Signal rule.** A declarative rule that matches items and contributes features: importance, urgency, must-surface, routing hints, sensitivity, deadlines.
- **Agent task.** A unit of judgment Docket issues to the agent: a self-contained input pack, a rendered prompt, and an output schema. Docket validates the result.
- **Finding.** An item that survived scoring and review and needs the operator.
- **Question.** A structured decision put to the operator, with options that are typed action proposals. Kinds: *reconcile* (did planned work happen), *covered* (did a meeting with no record cover this agenda entry), *finding* (what to do with this), *verify* (this looks done: close it?), *approve* (accept this plan).
- **Action.** A typed proposed effect: add a task, add an agenda line, place a calendar block, draft a message, do it now, watch, dismiss.
- **Destination.** A configured place an action lands: a task store, an agenda, a calendar, a message channel, a journal.
- **Plan.** The concrete set of operations a run proposes, including a schedule, with a digest.
- **Run.** One execution of a graph for a date. A node runs as soon as everything it names is done, and every run ends with a **seal** that advances cursors and writes the journal.
- **Cursor.** A durable read position per source (and per unit, such as a chat space) that advances only across proven coverage.
- **Unexpected shape.** A record of data the code did not model, with the safe reading taken and the extension point named.

## 6. How Docket is used

### 6.1 Setup

The operator or agent runs `docket init`, chooses presets (for example a chat preset, a Markdown task-store preset and a calendar preset), and fills in the profile: identity and handles, people and relationships, groups and their meeting forums, topics the operator owns, protected entities, and how their calendar titles map to availability. `docket doctor` checks configuration, credentials, binaries and adapter permissions without writing anything, and names each failure and its fix. `docket source test <id>` reads a small sample from a source and shows what Docket made of it.

### 6.2 The daily run

The reference workspace defines a `daily` graph. The agent starts a run of it with `docket run start daily` and loops on `docket next`.

The read nodes, one per source, are ready together, and the agent runs them at once. A judge node is ready once the reads it works on are done; it issues assessment tasks, which the agent runs as parallel subagents. A merge node combines rules, scores and judgments, and a review node issues the adversarial review task. The ask node then asks one burst of questions: reconcile yesterday's blocks, decide the findings. The agent relays them; the operator answers in their own words and the agent records structured answers ("due Monday, 60 minutes, merge with the other one, schedule it over my flight"). The plan node builds the plan, the approve node shows the change list, and on approval the apply node writes it and records receipts. The seal advances cursors and writes the journal.

The evening before, a sandbox run of the same graph (`docket run start daily --sandbox`) drives every node against live reads and sandbox twins, with each question answered by its recommended option. A defect surfaces then, not in the operator's morning.

### 6.3 Other runs

A workspace can define other graphs from the same verbs. A **triage** graph reads chat, direct messages and mail, asks about anything urgent, routes to the task store, and schedules nothing. A **weekly** graph might reconcile the whole task store and agendas. Graphs share sources, profile and rules. `docket graph show <graph>` lists a graph's nodes: what each does, who does it, and what it waits for.

### 6.4 The agent loop

The agent's entire procedure is:

```
loop:
  reply = docket next --json
  if reply.done: report reply.summary; stop
  for each step in reply.steps, together:
    run      -> execute step.command
    dispatch -> run step.tasks as subagents (at most step.concurrency at once), submit each result
    ask      -> put step.questions to the operator, record answers with docket ask answer
    wait     -> report step.reason and step.instructions; this node and the nodes after it wait
  if every step is a wait: stop
```

Every step names its node, the command to run and why. A failed command returns a machine-readable error with a code, a one-line cause and the next command to try.

## 7. Functional requirements

Requirements are numbered for traceability. **Must** is required for v1; **Should** is required for v1 unless explicitly deferred in the implementation plan; **May** is optional.

### 7.1 Sources

- **FR-SRC-01 (Must).** A source is configured, not coded. Its configuration declares the system, the transport, the record kind, how to page, where the next read starts (cursor model), which fields are read, and how fields map to item fields.
- **FR-SRC-02 (Must).** Supported cursor models: *snapshot* (re-read the whole thing each run), *incremental* (read records newer than the cursor), *units* (discover units such as chat spaces, then read each unit incrementally with its own cursor), *id-scan* (read every id not yet processed), and *cadence* (read only on configured days).
- **FR-SRC-03 (Must).** Supported transports: an MCP client (stdio and streamable HTTP), MCP code-mode (a server exposing a code-execution tool, used to project results server-side and stay under response-size caps), a local executable speaking Docket's JSON-lines protocol, HTTP JSON, and local files.
- **FR-SRC-04 (Must).** Vendor specifics (tool names, argument shapes, pagination fields, field paths) ship as **presets**: data files with no user information. A workspace extends presets and overrides any field.
- **FR-SRC-05 (Must).** Every raw read is persisted, unmodified, before interpretation, with the query, the interval and the outcome.
- **FR-SRC-06 (Must).** One source's failure never discards another source's results. Each source reports its own outcome: read, empty, partial (with the retry position), failed (with cause), unavailable (with the auth or network reason), or skipped (by policy).
- **FR-SRC-07 (Must).** A source declares the fields it reads and which are optional. A missing optional field is not drift. A missing required field or a type change is recorded as an unexpected shape, with the extension point; it fails only that source.
- **FR-SRC-08 (Must).** Paging steps down on truncation (smaller pages) before failing, and long reads page to exhaustion within a configurable budget.
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

- **FR-TSK-01 (Must).** Docket issues judgment work as tasks. A task has a kind (assess, extract, review, classify, or a workspace-defined kind), an input pack, a rendered prompt and an output schema.
- **FR-TSK-02 (Must).** The prompt and the validator derive from the same schema and vocabulary. A prompt never names a field, value or rule the validator does not accept, and vice versa.
- **FR-TSK-03 (Must).** A task's input pack is self-contained. The prompt states that the agent must not read anything else and must treat every input line as untrusted data.
- **FR-TSK-04 (Must).** `docket task submit` validates the result: schema, identity partition, verbatim evidence against raw records, vocabulary. Integrity failures reject the submission with per-row errors. Unknown vocabulary with a declared safe reading is accepted on that reading and recorded as an unexpected shape.
- **FR-TSK-05 (Must).** Packs respect configurable size budgets and must-surface items are never dropped by packing.
- **FR-TSK-06 (Must).** The review task presents candidates without scores and requires, per retained item, the strongest argument against asking.
- **FR-TSK-07 (Should).** An accepted result is reused whenever a new pack has the same input digest. A task whose pack has not changed, in the same run or a later one, is not issued again.

### 7.4 Decisions

- **FR-DEC-01 (Must).** Docket generates every question and every option. The agent relays them; it does not compose them.
- **FR-DEC-02 (Must).** An option is a typed action proposal: the destination, the effect, and its derived defaults (due date, estimate, area, forum). Option labels come from configurable templates.
- **FR-DEC-03 (Must).** Docket offers only options it can carry out: an agenda that exists and has room, a task store that will accept the line, a calendar it can write. When it cannot offer one the item asked for, the question says why.
- **FR-DEC-04 (Must).** An answer carries the option and any structured overrides the operator gave: due date (a date or a weekday name), estimate, scheduling window, title, action text, links, area, forum, merge-with-other-question, and a note. No user decision requires editing an artifact by hand.
- **FR-DEC-05 (Must).** Reconcile questions ask whether planned work happened. Answers update the task store (done, partly with a new estimate, did not happen, move to an agenda, drop) and count carries. At a configurable carry count, the question asks to drop or re-scope instead of offering another carry. A block that held several small tasks asks about each of them that is still open.
- **FR-DEC-06 (Must).** Answers bind to item identity, not position. When the questions are recomputed, an answer whose item is unchanged is kept; a changed item is asked again, showing the previous answer.
- **FR-DEC-07 (Must).** All questions in a burst are answerable with no source reads in between.
- **FR-DEC-08 (Should).** When the operator names a day, Docket reports how full that day already is.
- **FR-DEC-09 (Should).** For a meeting that left no record, Docket can ask which agenda entries were covered instead of assuming all were: covered, still to raise, or drop. The answers of one meeting become one change to its agenda, approved like any other, and an entry kept for a meeting is not asked about again for that meeting.
- **FR-DEC-10 (Must).** A question about an item carries what a driver needs to show it without reading an artifact: the item's link and every link merged into it, its source, sender, time, score and a bounded text, and what a review changed or doubted. A question about a task line or an agenda entry carries the line's link.
- **FR-DEC-11 (Must).** Nothing an ask node was given is dropped without the driver seeing it. By default the items past a question budget are still asked, flagged and after the rest. A workspace can choose not to ask them; then they are listed on the ask step and in `docket ask list`, the seal step says how many, and the run summary counts them.

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
- **FR-PLN-09 (Must).** A live plan never places work before the moment it is built: nothing is placed, moved to or kept movable before the later of the run's clock and the time the plan is built, and the plan says when it was built for. A run whose clock was given explicitly plans from that clock only.
- **FR-PLN-10 (Must).** A task placed again after its block elapsed moves that block to its new slot, with its words updated, and never asks the calendar for a second block under the same identity. This holds for a block of an earlier day, for the parts of a split task, and for a batch whose members are unchanged. A batch whose members changed leaves its elapsed block as it stands.
- **FR-PLN-11 (Should).** A workspace can name standing blocks, such as its own planning session: a title, a length and a day (the next working day, or today). Each is placed before tasks, in the earliest legal slot of its day, with its own title and notes. The next run keeps the block it placed and places one for the following day. A standing block is never reconciled, batched, split or recorded in the item ledger, and removing it from the configuration releases its blocks. A day with no legal slot is noted in the plan.

### 7.7 Apply, seal and memory

- **FR-APP-01 (Must).** Approval binds the exact plan digest. Approval is invalidated by any change to the plan's inputs.
- **FR-APP-02 (Must).** Freshness is checked against state, not a clock: at approval, Docket compares the live calendar and task store with what the plan was built from, approves if unchanged, and refuses with a rebuild instruction if changed. A configurable note records any delay. Time passing alone never refuses an approval; a planned block whose start has passed is reported to the operator.
- **FR-APP-03 (Must).** Apply is idempotent and repeatable. Each operation writes a receipt. Repeating apply after a crash continues from the receipts and makes no duplicate effect.
- **FR-APP-07 (Must).** A write that did not complete holds the run. When any operation of an apply or a write was refused, failed or blocked, the node is not done, the seal refuses, and the driver is told each such operation with its error. The driver can repeat what did not complete, or accept that it stays unwritten. An acceptance is recorded in the run, and the seal summary and the journal show the operations left unwritten.
- **FR-APP-04 (Must).** The seal advances cursors only across proven, contiguous coverage, writes the journal from a configurable template and appends ledgers. A command the workspace runs after apply (for example a commit or a reindex) is a `run` node ordered with `after`, and it runs once per run.
- **FR-APP-05 (Must).** A dispositions memory records every decision (done, watch, dismiss, routed) so the next run suppresses or re-surfaces correctly.
- **FR-APP-06 (Must).** The seal can be repeated after a partial failure and never repeats a completed step. A failing `run` node stops the run at that node; repeating it runs only that node.

### 7.8 Robustness

- **FR-ROB-01 (Must).** Integrity checks (digests, bindings, identity partitions, receipts) are exact and refuse. Shape checks are tolerant: the code takes a declared safe reading, continues, and records an unexpected shape with the area, the detail, a sample and the extension point.
- **FR-ROB-02 (Must).** Unexpected shapes appear in the journal, in `docket unexpected` (aggregated across runs, most frequent first), in the run's summary and in test reports.
- **FR-ROB-03 (Must).** `docket run start <graph> --sandbox` binds every destination of the run to its sandbox twin and runs each `run` node in its `dry_run` form, or skips it when it has none. A sandbox run reads live sources and goes to its seal, and writes nothing outside the run: no cursor, disposition, ledger entry or live destination.
- **FR-ROB-04 (Must).** A crash, a repeated command and a mid-run code or configuration change are handled the same way: every node whose digest changed recomputes from immutable raw reads and recorded answers, and nothing else does. Once a write has begun, the nodes upstream of it are read as pinned; a completed effect is never recomputed.
- **FR-ROB-05 (Must).** A run holds a lease. A crashed owner's lease is adoptable; a live foreign owner requires an explicit steal.
- **FR-ROB-06 (Must).** Nothing Docket runs unattended can block on a person: no browser, no keychain prompt, no permission dialog. `docket doctor` detects each such dependency up front.

## 8. Agent ergonomics

These requirements carry the same weight as the functional ones.

- **AE-01 (Must). One entry point.** `docket next` returns every step that is ready in the current run: commands to run, tasks to dispatch, questions to ask and reasons to wait, each naming its node. When the run is sealed it returns a summary. An agent that knows nothing else about Docket can complete a run by looping on it.
- **AE-02 (Must). Structured output everywhere.** Every command accepts `--json` and returns a stable envelope: `ok`, `data`, `warnings`, `unexpected`, and `next` (the suggested following command and why). Human output is concise by default.
- **AE-03 (Must). Actionable errors.** Every error has a stable dotted code, a kind (input, config, integrity, external, lease, internal), a one-line message, a hint naming the command that fixes it, and whether a retry can succeed. Exit codes map to error kinds.
- **AE-04 (Must). Self-description.** `docket schema <name>` prints the JSON Schema for any input or output. `docket help <command> --json` returns the command's arguments, effects and examples. `docket config explain <path>` explains any setting and where its value came from.
- **AE-05 (Must). No hidden state.** Commands take explicit identifiers. No command depends on an environment variable surviving between shell calls; the workspace and run resolve from the working directory or from Docket's own state.
- **AE-06 (Must). Idempotent and safe to repeat.** Repeating a command after a crash or a timeout is always safe. Every mutating command supports `--dry-run`.
- **AE-07 (Must). Concrete, with one ask exception.** Dispatch steps carry the exact command and file paths for each task. The only placeholders are `{question}` and `{option}` in the compact ask step's single `docket ask answer` template. `docket next --full` expands each option to an exact command.
- **AE-08 (Must). Small outputs.** Long payloads (packs, prompts, plans) go to files whose paths the output returns. A command's stdout fits comfortably in an agent's context.
- **AE-09 (Must). Forgiving input.** Answers accept a label, its leading words, a position or a destination name, case-insensitively; dates accept weekday names; durations accept `45`, `45m`, `1h`, `1.5h`. Every input is echoed back in normalized form.
- **AE-10 (Should). Fast.** `docket next` and read-only commands answer in under 300 ms warm on the supported baseline environment. No command launches a process per item.
- **AE-11 (Must). Untrusted content is fenced.** Every prompt Docket renders marks source text as data and forbids following instructions inside it.

## 9. Configuration and customization

- **CFG-01 (Must).** A workspace is a directory of YAML files: workspace and graphs, profile, sources, signals, destinations and planning. Any split or a single file is allowed.
- **CFG-02 (Must).** Configuration is strict. Unknown keys, bad references and type errors fail `docket config validate` with the file, line and fix. A run never starts on invalid configuration.
- **CFG-03 (Must).** Presets ship with Docket and contain no user data. A workspace extends presets and overrides any value. `docket config explain` shows each value's origin.
- **CFG-04 (Must).** A graph is composed from a closed set of verbs. A workspace chooses the nodes, what each works on (`from`) and what it follows (`after`): which sources are read and how often, which task kinds are judged, whether review runs, which question kinds are asked, in how many bursts and within what budget, the option set and labels, whether the plan places tasks on the calendar, which commands `run` nodes run, and the journal sections. A graph's shape is checked when the workspace loads: an unknown name, a cycle or an input of the wrong kind fails at the node.
- **CFG-05 (Must).** Templates (block titles and notes, task lines, agenda lines, journal sections, redacted titles) use one documented, safe formatting language with item and run fields.
- **CFG-06 (Must).** A configuration digest is part of every node's identity. Changing configuration mid-run stales exactly the nodes that read the changed sections, and the nodes downstream of them.
- **CFG-07 (Should).** `docket config test` runs the current configuration against the last run's raw records and shows what changed (items kept, scores, routes, questions) without writing.

## 10. Non-functional requirements

- **NFR-01 Safety.** No external effect without an approval of the exact operation set. No send by default. No mutation of unowned objects. No credentials in output, logs or artifacts.
- **NFR-02 Privacy.** Redaction for shared destinations is enforced by validation, not by convention. Protected entities and sensitive classes come from configuration.
- **NFR-03 Determinism.** Given the same raw records, configuration, answers and clock, every derived artifact is byte-identical. A run whose clock was given explicitly is reproducible end to end; a run whose clock was taken from the wall reads the wall clock only to keep a plan from placing work in the past (FR-PLN-09).
- **NFR-04 Auditability.** Every item, question, decision and operation traces to the raw records and rules that produced it.
- **NFR-05 Unattended operation.** No step blocks on a person. Failures are named and fast.
- **NFR-06 Performance.** Reading is network-bound. Read nodes that do not depend on each other are ready together, so the agent runs them at once. Planning a 42-day horizon with 200 open tasks takes under 5 seconds. A full synthetic run in the test suite takes under 60 seconds.
- **NFR-07 Portability.** macOS is the primary supported platform; Linux is also supported. Platform-specific adapters (for example a native calendar bridge) are optional and isolated.
- **NFR-08 Testability.** Every graph runs offline against sandbox twins and recorded transports. Tests never touch live state or real credentials.
- **NFR-09 Compatibility.** Configuration, artifact schemas, the workspace database and the plugin protocol each carry a version. An artifact written under another schema version is stale and recomputed. A database or protocol at a version Docket does not read is refused with a named error code, never read on a guess.

## 11. Success metrics

| Metric | Target |
|---|---|
| Operator time per day (answering and approving) | 10 minutes or less on a normal day |
| Runs that hit a code defect after a passing sandbox run the evening before | 0 over 10 consecutive working days |
| User-specific literals in Docket's source tree | 0, enforced by an automated litmus test (§12) |
| Second-persona run with zero code changes | Passes end to end in the test suite |
| Shadow parity with the predecessor over 5 working days | Same questions for at least 90% of the predecessor's findings, every difference explained by `item why` |
| Unexpected-shape extension latency | Each recurring shape modelled or explicitly accepted within 3 working days |
| Human edits of Docket artifacts | 0; every user decision expressible in an answer |

## 12. The litmus test

Two conditions, both automated in the test suite:

1. **Nothing hardcoded.** The Docket source tree contains no literal from the reference operator's world: no names, handles, email domains, organization or product names, note-repository names, folder names, calendar titles, glyph conventions, hosts or service vocabularies. The test extracts every identifier from the reference workspace configuration (kept outside the Docket repository) and asserts none appears under `src/`. A second, static list covers conventions such as the reference calendar's title words.
2. **Everything modellable.** The reference workspace (Appendix B) runs end to end as configuration only, and a second, materially different persona (different role, different chat and ticket tools, different task-store grammar, different calendar conventions) runs on the same code.

## 13. Release scope

### 13.1 v1

Everything marked Must, the Should items not deferred by the implementation plan, the reference workspace ported from the predecessor, the second persona as a test fixture, and a shadow period against the predecessor before cutover.

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
- **Porting effort.** The reference workspace contains many small judgments. Mitigation: shadow runs with per-item diffs before cutover.

### 14.2 Open questions

1. The reference workspace lives in the operator's private configuration repository, outside the Docket repository.
2. Is a native calendar bridge shipped with Docket, or is the existing bridge wrapped through the executable adapter protocol? This document assumes the latter for v1.
3. Sending is deferred until after v1. v1 creates drafts only.
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
| Graph | A named set of nodes that together do one kind of run. |
| Item | A normalized candidate derived from records. |
| Must-surface | A rule effect that exempts an item from floors and caps. |
| Node | One step of a graph, with one verb. Its work is computed by Docket, judged by an agent or asked of the operator. `from` names the nodes whose output it works on; `after` names nodes it follows. |
| Plan | The concrete operation set and schedule a run proposes, with a digest. |
| Preset | A shipped configuration fragment with no user data. |
| Profile | The operator's identity, people, groups, topics, protected entities, forums and calendar vocabulary. |
| Question | A structured decision with typed options: reconcile, covered, finding, verify or approve. |
| Receipt | Proof that an operation was applied, keyed by operation id. |
| Record | An immutable raw read from a source. |
| Run | One execution of a graph for a date. |
| Sandbox run | A run started with `--sandbox`: every destination is bound to its sandbox twin and nothing is written outside the run. |
| Seal | The end of every run, implicit in every graph: cursors advance, dispositions and the ledger are written, and the journal entry is made. |
| Signal rule | A declarative match and effect over items. |
| Source | A configured read-only connection to a system. |
| Unexpected shape | A recorded instance of data the code did not model, with the safe reading taken and the extension point. |
| Unit | A sub-collection of a source with its own cursor, such as a chat space or a conversation. |
| Verb | What a node does: `read`, `run`, `judge`, `merge`, `ask`, `plan`, `approve`, `apply` or `write`. |
| Workspace | A directory of configuration describing one operator's world. |

## Appendix B: Reference workspace mapping

Every element of the predecessor, and where it lives in Docket. "Profile", "Sources", "Signals", "Decisions", "Destinations", "Planning" and "Graphs" are workspace configuration; "Preset" is shipped data; "Core" is generic code with no user data.

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
| Score formula, floor, overflow, caps, question budget | Signals: `scoring` |
| Gate A answers, Gate B option labels, "Already handled", "Handle now" | Decisions: option sets and label templates, chosen by each `ask` node |
| Structured answer fields (due, estimate, window, merge, links) | Core |
| Watermarks in an external state tool, unit watermarks, processed document ids | Core: state database |
| Dispositions ledger, prior decisions | Core: dispositions memory |
| Day record, surfaced-items audit, coverage and run ledgers | Destinations: journal templates; Core ledgers |
| Search reindex and note-repository commit after finalize | Graphs: `run` nodes after the apply node |
| Credential refresh through a local auth tool and a browser-free token check | Sources: auth check and refresh commands |
| Evening check, morning decisions | Graphs: a sandbox run of the daily graph in the evening, the live run in the morning |
| Dry runs, unexpected shapes, leases, receipts, integrity checks | Core |
