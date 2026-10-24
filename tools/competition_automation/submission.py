"""Safe local-to-human submission handoff helpers.

This module deliberately has no network client.  It turns a validated local
artifact into an explicit handoff state so a model cannot interpret a remote
upload error as a failed experiment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import validate_submission
from .ledger import assert_new_submission


def build_submission_plan(
    contract: dict[str, Any],
    artifact: Path,
    sample: Path | None = None,
    ledger: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Validate an artifact and return a non-uploading human handoff plan.

    The plan deliberately records local readiness separately from external
    submission state.  A manual handoff therefore remains successful even
    when no remote score exists yet.
    """
    validation = validate_submission(artifact, contract, sample)
    if ledger is not None:
        validation["ledger"] = assert_new_submission(artifact, ledger, root=root)
    mode = str(contract["submission"].get("upload_mode", "manual"))
    if mode == "manual":
        status = "READY_FOR_MANUAL_UPLOAD"
        next_action = "human_upload_required"
        external_status = "manual_pending"
    elif mode == "disabled":
        status = "UPLOAD_DISABLED"
        next_action = "do_not_upload"
        external_status = "not_requested"
    else:
        status = "USER_CONFIRMATION_REQUIRED"
        next_action = "obtain_explicit_user_confirmation_before_external_upload"
        external_status = "not_requested"
    return {
        "status": status,
        "artifact_status": "ready",
        "external_submission_status": external_status,
        "upload_mode": mode,
        "external_upload_attempted": False,
        "next_action": next_action,
        "validation": validation,
    }
