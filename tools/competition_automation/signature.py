"""Stable semantic signatures for experiment and hypothesis deduplication."""

from __future__ import annotations

import json
import re
from typing import Any

from .fingerprint import canonical_json_digest

VOLATILE_FIELDS = frozenset(
    {
        "record_id",
        "experiment_id",
        "status",
        "requested_decision",
        "decision_source",
        "proposal",
        "metrics",
        "checks",
        "artifacts",
        "created_at",
        "started_at",
        "finished_at",
        "ready_at",
        "submitted_at",
        "submission_status",
        "platform_submission_id",
        "submission_artifact_sha256",
        "external_submission_status",
        "control",
        "control_of",
        "reference_record_id",
        "control_reason",
        "reason",
        "experiment_signature",
        "base_signature",
    }
)
SEMANTIC_HINT_FIELDS = frozenset(
    {
        "competition_slug",
        "competition",
        "family",
        "provenance",
        "params",
        "config",
        "code",
        "data",
        "split",
        "features",
        "model",
        "seed",
        "change_scope",
        "hypothesis",
    }
)


def legacy_signature(record: dict[str, Any]) -> str | None:
    """Verify historical v1 records without rewriting their stored identities."""
    if not SEMANTIC_HINT_FIELDS.intersection(record):
        return None
    return canonical_json_digest(
        {key: value for key, value in record.items() if key not in VOLATILE_FIELDS}
    )


class SignatureError(ValueError):
    """Raised for an ambiguous or incomplete computation specification."""


SPEC_FIELDS = frozenset(
    {
        "schema_version",
        "task_type",
        "code",
        "data",
        "split",
        "config",
        "seed",
        "budget",
        "evaluation",
        "environment",
        "command",
    }
)
SEED_FIELDS = frozenset({"seed", "seeds", "random_seed", "random_state"})


def _seedless(value: Any) -> Any:
    """Remove model RNG seeds recursively, retaining split/data identities."""
    if isinstance(value, dict):
        return {
            key: item if key in {"split", "data"} else _seedless(item)
            for key, item in value.items()
            if key not in SEED_FIELDS
        }
    if isinstance(value, list):
        return [_seedless(item) for item in value]
    return value


def computation_payload(
    spec: dict[str, Any], ignore_seed: bool = False
) -> dict[str, Any]:
    """Validate an explicit spec and strip only operational snapshot paths.

    Artifact roles, content, effective config, budget, environment and command
    template determine identity. Issue IDs, family labels and output locations
    belong to the execution request, never this specification.
    """
    if (
        not isinstance(spec, dict)
        or not SPEC_FIELDS.issubset(spec)
        or set(spec) - SPEC_FIELDS - {"resume"}
    ):
        raise SignatureError(
            f"computation_spec requires {sorted(SPEC_FIELDS)} and optional resume"
        )
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise SignatureError("computation_spec.schema_version must be 1")
    try:
        json.dumps(spec, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SignatureError(
            "computation_spec must contain finite JSON values"
        ) from exc
    if not isinstance(spec["task_type"], str) or not spec["task_type"].strip():
        raise SignatureError("task_type must be non-empty")
    if type(spec["seed"]) is not int:
        raise SignatureError("seed must be an integer")
    for key in ("config", "budget", "evaluation", "environment"):
        if not isinstance(spec[key], dict) or (key != "config" and not spec[key]):
            raise SignatureError(f"{key} must be an object (non-empty except config)")
    command = spec["command"]
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(x, str) or not x or "\0" in x for x in command)
    ):
        raise SignatureError("command must be a non-empty argv array")
    payload = dict(spec)
    resume = spec.get("resume")
    if resume is not None:
        if not isinstance(resume, dict) or set(resume) != {"mode", "manifest"}:
            raise SignatureError("resume requires mode and manifest")
        if resume["mode"] not in {"exact_resume", "weights_only", "feature_transfer"}:
            raise SignatureError("resume mode is invalid")
        manifest = resume["manifest"]
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"path", "sha256"}
            or not isinstance(manifest["path"], str)
            or not manifest["path"]
            or not isinstance(manifest["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])
        ):
            raise SignatureError("resume manifest requires path and SHA-256")
        payload["resume"] = {
            "mode": resume["mode"],
            "manifest_sha256": manifest["sha256"],
        }
    else:
        payload.pop("resume", None)
    for group in ("code", "data", "split"):
        entries = spec[group]
        if not isinstance(entries, dict) or not entries:
            raise SignatureError(f"{group} requires role-to-snapshot entries")
        hashes: dict[str, str] = {}
        for role, snapshot in entries.items():
            if not isinstance(role, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", role
            ):
                raise SignatureError(f"invalid snapshot role in {group}")
            if not isinstance(snapshot, dict) or set(snapshot) != {"path", "sha256"}:
                raise SignatureError(f"{group}.{role} requires path and sha256")
            if not isinstance(snapshot["path"], str) or not snapshot["path"]:
                raise SignatureError(f"{group}.{role}.path must be non-empty")
            if not isinstance(snapshot["sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", snapshot["sha256"]
            ):
                raise SignatureError(f"{group}.{role}.sha256 is invalid")
            hashes[role] = snapshot["sha256"]
        payload[group] = hashes
    if ignore_seed:
        payload.pop("seed")
        payload["config"] = _seedless(payload["config"])
    return payload


def computation_signature(spec: dict[str, Any], ignore_seed: bool = False) -> str:
    """Hash a checked computation independent of Issue names and output paths."""
    return canonical_json_digest(computation_payload(spec, ignore_seed))


def experiment_signature(
    record: dict[str, Any], ignore_seed: bool = False, *, canonical: bool = False
) -> str | None:
    """Preserve verified historical IDs, using v2 for new identity comparisons.

    Existing adapters compare this return value with stored v1 signatures.
    Deduplication explicitly requests ``canonical=True`` so historical IDs never
    change comparison semantics or make an edited record pass verification.
    """
    if (
        not canonical
        and not ignore_seed
        and record.get("signature_version", 1) == 1
        and record.get("experiment_signature") is not None
        and record["experiment_signature"] == legacy_signature(record)
    ):
        return record["experiment_signature"]
    if "computation_spec" in record:
        return computation_signature(record["computation_spec"], ignore_seed)
    if not SEMANTIC_HINT_FIELDS.intersection(record):
        return None
    payload = {
        key: value for key, value in record.items() if key in SEMANTIC_HINT_FIELDS
    }

    # Content-addressed snapshots retain roles but not relocation-only paths.
    def normalize(value: Any) -> Any:
        """Normalize legacy snapshot evidence without dropping semantic params."""
        if isinstance(value, dict):
            if isinstance(value.get("sha256"), str):
                return {
                    key: normalize(item)
                    for key, item in value.items()
                    if key not in {"path", "relative_path", "created_at", "size_bytes"}
                }
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    payload = normalize(payload)
    if ignore_seed:
        payload = _seedless(payload)
    return canonical_json_digest(payload)
