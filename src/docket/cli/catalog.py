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
