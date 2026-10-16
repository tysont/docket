"""The error catalog: every error code Docket can emit, with its kind.

Codes are dotted ``<area>.<condition>`` and stable across versions (TD §15.3).
``docket help errors --json`` prints this table. ``DocketError`` refuses a code
that is not listed here, so the catalog cannot drift from the code.
"""
from __future__ import annotations

KINDS: dict[str, int] = {
    "input": 2,
    "config": 3,
    "integrity": 4,
    "external": 5,
    "lease": 6,
    "state": 7,
    "internal": 70,
}

# code -> (kind, retryable default, description)
ERRORS: dict[str, tuple[str, bool, str]] = {
    # input
    "input.bad_argument": ("input", False, "A command argument is missing, unknown or malformed."),
    "input.unknown_command": ("input", False, "No command has this name."),
    "input.unknown_option": ("input", False, "The answer names no option of the question."),
    "input.ambiguous_option": ("input", False, "The answer matches more than one option."),
    "input.bad_date": ("input", False, "A date or weekday could not be parsed."),
    "input.bad_window": ("input", False, "A scheduling window could not be parsed."),
    "input.bad_duration": ("input", False, "A duration could not be parsed."),
    "input.bad_json": ("input", False, "A JSON input could not be parsed."),
    "input.unknown_schema": ("input", False, "No schema has this name."),
    "input.unknown_preset": ("input", False, "No shipped preset has this id."),
    "input.unknown_path": ("input", False, "The dotted configuration path does not exist."),
    "input.unknown_question": ("input", False, "No question with this id exists in the run."),
    "input.unknown_task": ("input", False, "No task with this id exists in the run."),
    "input.missing_digest": ("input", False, "Approving a plan requires --digest with the plan digest."),
    "input.unknown_workflow": ("input", False, "The workspace defines no workflow with this name."),
    "input.not_empty": ("input", False, "The target directory already holds a workspace."),
    "task.invalid_result": ("input", False, "A task result does not match the task's output schema."),
    # config
    "config.invalid": ("config", False, "Configuration validation failed; details.errors lists every error."),
    "config.yaml_syntax": ("config", False, "A configuration file is not valid YAML."),
    "config.unknown_key": ("config", False, "A configuration key is not part of the schema."),
    "config.bad_type": ("config", False, "A configuration value has the wrong type."),
    "config.missing_key": ("config", False, "A required configuration key is absent."),
    "config.bad_reference": ("config", False, "A configuration value names an id that does not exist."),
    "config.duplicate_id": ("config", False, "Two entries in one list share an id."),
    "config.duplicate_section": ("config", False, "A top-level section is defined in two workspace files."),
    "config.include_missing": ("config", False, "An included file does not exist."),
    "config.unknown_preset": ("config", False, "The workspace extends a preset that is not shipped."),
    "config.preset_forbidden": ("config", False, "A preset defines a section only a workspace may define."),
    "config.bad_template": ("config", False, "A template does not parse or names a field its context lacks."),
    "config.bad_regex": ("config", False, "A regular expression does not compile."),
    "config.bad_timezone": ("config", False, "A timezone name is not known."),
    "config.bad_mapping": ("config", False, "A source mapping key or path is malformed."),
    "config.bad_stage_order": ("config", False, "A workflow's stages break the fixed pipeline order."),
    "config.unknown_kind": ("config", False, "A source kind or destination adapter is not known."),
    "config.capability_missing": ("config", False, "No configured adapter supports an operation the workflow needs."),
    "config.bad_merge_form": ("config", False, "An extend/remove form is malformed or used where no list exists."),
    "workspace.not_found": ("config", False, "No docket.yaml was found from the working directory, --workspace or DOCKET_WORKSPACE."),
    # integrity
    "integrity.partition": ("integrity", False, "A task result does not cover exactly the pack's item ids."),
    "integrity.stale_artifact": ("integrity", True, "An artifact's digest does not match the current inputs."),
    "approve.digest_mismatch": ("integrity", True, "The approval digest differs from the current plan digest."),
    "apply.receipt_conflict": ("integrity", False, "A receipt exists for this operation id with a different payload."),
    # external
    "source.unavailable": ("external", True, "A source could not be read."),
    "dest.apply_failed": ("external", True, "A destination refused or failed an operation."),
    # lease
    "lease.held": ("lease", True, "Another live session holds the run lease."),
    # state
    "run.none_open": ("state", False, "No unsealed run exists in this workspace."),
    "run.not_found": ("state", False, "No run has this id."),
    "run.sealed": ("state", False, "The run is sealed; it can be inspected but not changed."),
    "run.abandoned": ("state", False, "The run was abandoned."),
    "run.paused": ("state", True, "The workflow pauses before this stage until the run is resumed."),
    "stage.not_ready": ("state", True, "An earlier stage is not current yet."),
    "stage.not_in_workflow": ("state", False, "The run's workflow does not include this stage."),
    "stage.post_apply_change": ("state", False, "Inputs changed after apply wrote receipts; the run cannot go backward."),
    "store.db_too_new": ("state", False, "The workspace database was written by a newer Docket."),
    # --- M2 collection ---
    "config.secret_literal": ("config", False, "A credential-named field holds a literal that looks like a secret; "
                              "reference an environment variable or a file instead."),
    "input.unknown_source": ("input", False, "The workspace defines no source with this id."),
    "input.unknown_unit": ("input", False, "The source discovered no unit with this id."),
    "source.failed": ("external", True, "A source or unit failed; details carry the raw cause."),
    "source.auth_failed": ("external", True, "A source's unattended auth check failed; the source is unavailable."),
    "source.contract.type_changed": ("external", False, "A required response field changed type; the unit failed."),
    "source.contract.not_json": ("external", False, "A response body is not the JSON the contract describes."),
    "source.budget_exhausted": ("external", True, "A source's page or time budget ended collection as partial."),
    "source.truncated": ("external", True, "A page stayed truncated at the minimum page size; the unit is partial."),
    "transport.timeout": ("external", True, "A transport request or handshake timed out."),
    "transport.too_large": ("external", True, "A response exceeded the transport's size cap or was declared too large."),
    "transport.status": ("external", True, "The remote system answered with an error status."),
    "transport.protocol": ("external", False, "The remote system broke the transport protocol."),
    "transport.unavailable": ("external", True, "The transport could not start or connect."),
    "transport.no_recording": ("external", False, "A replay transport has no recording for this request digest."),
    "transport.error": ("external", True, "A transport failed; the message is the system's own."),
    # --- M6 destinations ---
    "dest.line_changed": ("integrity", False, "A Markdown line changed since the plan was built: zero or several lines match the operation's old-line digest in its section."),
    "dest.redaction_leak": ("integrity", False, "Text rendered for a shared destination still names a protected entity or a person."),
    "dest.unowned": ("integrity", False, "The operation targets an object Docket does not own (no valid ownership marker, or attendees or recurrence)."),
    "dest.state_changed": ("integrity", False, "The destination object differs from what the operation expects."),
    "dest.quality": ("integrity", False, "A task line fails the destination's quality rules; details.fix names the answer override."),
    "dest.not_accepted": ("integrity", False, "The destination cannot accept the action (full, missing target, or not supported)."),
    "dest.unsupported": ("integrity", False, "The destination does not declare the verb the operation needs."),
    "dest.unavailable": ("external", True, "The destination's program, server or transport is not reachable."),
    "dest.protocol": ("external", True, "An exec-protocol program sent a malformed or failed response."),
    "plugin.unknown": ("input", False, "No built-in adapter or configured destination has this name."),
    "plugin.check_failed": ("external", False, "The conformance suite failed; details.report lists every check."),
    # --- M7 planning ---
    "plan.window_too_small": ("input", False, "A scheduling window has too little free time for the task's estimate."),
    "plan.window_unresolved": ("input", False, "A named-event window matches no calendar event in the horizon."),
    "plan.unknown_item": ("input", False, "No planned task matches the reference given to plan explain."),
    "plan.not_built": ("state", True, "No current plan exists for the run; build it first."),
    "plan.no_calendar": ("config", False, "The command needs planning.calendar, which the workspace does not set."),
    "plan.invalid": ("integrity", False, "A built plan failed validation by its own constraint engine (a defect)."),
    # --- M3 signals ---
    "config.unknown_predicate": ("config", False, "A signal rule condition names a predicate outside the closed set."),
    "config.unknown_effect": ("config", False, "A signal rule names an effect outside the closed set."),
    "config.unknown_plugin": ("config", False, "A rule or scoring formula names a plugin no installed plugin registers."),
    "item.unknown": ("input", False, "No item of the run matches the id, URL or text given."),
    "item.ambiguous": ("input", False, "More than one item matches the text given; details.matches lists them."),
    "item.not_merged": ("state", True, "The run has no merge artifact yet; run the merge stage first."),
    "item.bad_filter": ("input", False, "An item list filter is not key=value over a known key."),
    "run.no_records": ("state", False, "No run of this workspace has collected records to test against."),
    # --- M4 tasks ---
    "task.rows_refused": ("integrity", False, "One or more task result rows failed an integrity or cross-field "
                          "check; details.rows names each row's path, received value, rule and fix."),
    "task.identity_mismatch": ("integrity", False, "A result row's copied identity field (such as source) differs "
                               "from its pack item (a row code in details.rows)."),
    "task.unknown_ref": ("integrity", False, "A result row's item reference names no other item of the pack "
                         "(a row code in details.rows)."),
    "task.evidence_not_verbatim": ("integrity", False, "An evidence quote does not appear verbatim in the item's "
                                   "raw record (a row code in details.rows)."),
    "task.evidence_unknown_line": ("integrity", False, "An evidence quote names a line id the item does not have "
                                   "(a row code in details.rows)."),
    "task.cross_field": ("input", False, "A result row breaks a cross-field rule of its kind, such as a kept item "
                         "without an owner (a row code in details.rows)."),
    # --- M5 decisions ---
    "input.missing_override": ("input", False, "The chosen option needs an override the answer did not give "
                               "(partly needs --est); the hint is the command with it."),
    "input.bad_merge": ("input", False, "--merge-with names no other finding question, or one that already absorbs "
                        "others."),
    "input.unknown_forum": ("input", False, "--forum names no forum of the profile; details.forums lists them."),
    # --- M8 apply and close ---
    "apply.stale_inputs": ("integrity", True, "A destination the plan touches changed since the plan was built; "
                           "details.destinations names each change. Rebuild the plan and approve again."),
    "hook.failed": ("external", True, "A hook under the fail policy exited non-zero, timed out or could not start; "
                    "details carry its exit status and log path."),
    # --- M9 robustness ---
    "cursor.regression": ("integrity", False, "An imported or proposed cursor position is behind the durable cursor; "
                          "cursors only move forward. details.rows names each."),
    "input.bad_cursor_file": ("input", False, "A cursor import file has the wrong shape; details.problems names each row."),
    "rehearse.no_run": ("state", False, "No run to rehearse: name one with --run, or start a run first."),
    "doctor.failed": ("external", True, "One or more doctor checks failed; details.checks names each with its fix."),
    # internal
    "internal.bug": ("internal", False, "An unexpected internal error; details.traceback_path holds the traceback."),
}


def kind_of(code: str) -> str:
    return ERRORS[code][0]


def as_rows() -> list[dict]:
    return [
        {"code": code, "kind": kind, "exit": KINDS[kind], "retryable": retry, "description": desc}
        for code, (kind, retry, desc) in sorted(ERRORS.items())
    ]
