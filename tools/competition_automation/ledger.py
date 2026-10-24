"""Append-only experiment and submission ledger helpers."""

from __future__ import annotations

import fcntl
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .fingerprint import artifact_record, canonical_json_digest, fingerprint_path
from .record import RecordError, validate_record
from .signature import experiment_signature, legacy_signature


class LedgerError(ValueError):
    """Raised when an append-only ledger invariant is violated."""


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL ledger and fail on malformed or non-object records."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise LedgerError(f"ledger record at {path}:{line_number} is not an object")
        records.append(value)
    return records


def _record_key(record: dict[str, Any]) -> str:
    key = record.get("record_id") or record.get("experiment_id")
    if not isinstance(key, str) or not key.strip():
        raise LedgerError("record requires a non-empty record_id or experiment_id")
    return key


def append_record(
    path: Path,
    record: dict[str, Any],
    allow_duplicate_artifact: bool = False,
    root: Path | None = None,
) -> str:
    """Serialize legacy ledger validation and append across local processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _append_record_locked(path, record, allow_duplicate_artifact, root)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _append_record_locked(
    path: Path,
    record: dict[str, Any],
    allow_duplicate_artifact: bool,
    root: Path | None,
) -> str:
    """Append a canonical record after checking IDs and artifact hashes.

    When ``root`` is provided, every artifact path is resolved below it and
    re-hashed before append.  A caller can therefore not turn a prose- or
    LLM-supplied hash into accepted evidence without a matching file.
    """
    if not isinstance(record, dict):
        raise LedgerError("record must be an object")
    record = dict(record)
    computed_signature = experiment_signature(record, canonical=True)
    supplied_signature = record.get("experiment_signature")
    if computed_signature is None and supplied_signature is not None:
        raise LedgerError("experiment_signature cannot be supplied without semantic experiment fields")
    if computed_signature is not None:
        legacy_verified = (
            record.get("signature_version", 1) == 1
            and supplied_signature is not None
            and supplied_signature == legacy_signature(record)
        )
        if supplied_signature is not None and supplied_signature != computed_signature and not legacy_verified:
            raise LedgerError("experiment_signature does not match the canonical semantic payload")
        record["experiment_signature"] = computed_signature
        record["signature_version"] = 2
    try:
        validate_record(record)
    except RecordError as exc:
        raise LedgerError(str(exc)) from exc
    records = read_ledger(path)
    record_key = _record_key(record)
    existing_keys = {_record_key(item) for item in records}
    if record_key in existing_keys:
        raise LedgerError(f"duplicate ledger record: {record_key}")
    current_signature = record.get("experiment_signature")
    existing_signatures: set[str | None] = set()
    existing_seedless_signatures: set[str | None] = set()
    for item in records:
        canonical = experiment_signature(item, canonical=True)
        supplied = item.get("experiment_signature")
        if canonical is None and supplied is not None:
            raise LedgerError("ledger contains an unverifiable experiment_signature")
        if canonical is not None and supplied is not None and supplied != canonical:
            if item.get("signature_version", 1) != 1 or supplied != legacy_signature(item):
                raise LedgerError("ledger contains a forged experiment_signature")
        existing_signatures.add(canonical)
        existing_seedless_signatures.add(experiment_signature(item, ignore_seed=True, canonical=True))
    if current_signature and current_signature in existing_signatures:
        if record.get("control") is not True:
            raise LedgerError("experiment signature already exists; record a distinct hypothesis or explicit control")
        reference = record.get("control_of") or record.get("reference_record_id")
        reason = record.get("control_reason") or record.get("reason")
        if not isinstance(reference, str) or not reference.strip() or not isinstance(reason, str) or not reason.strip():
            raise LedgerError("duplicate experiment control requires reference and reason")

    # A seed change must not masquerade as a new hypothesis.  The ordinary
    # signature includes the seed so that exact replays are distinguishable;
    # the seedless signature catches the common "same model, next seed" loop.
    current_seedless_signature = experiment_signature(record, ignore_seed=True, canonical=True)
    if (
        current_seedless_signature is not None
        and current_seedless_signature in existing_seedless_signatures
        and record.get("control") is not True
    ):
        raise LedgerError(
            "experiment semantics already exist with another seed; record a distinct hypothesis or explicit control"
        )
    def artifact_hashes_from(items: list[dict[str, Any]]) -> set[str]:
        """Collect output identities, retaining shared evidence as provenance."""
        values: set[str] = set()
        for item in items:
            artifacts = item.get("artifacts", [])
            if not isinstance(artifacts, list):
                raise LedgerError(f"{_record_key(item)}.artifacts must be a list")
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    raise LedgerError("each artifact must be an object")
                artifact_path = artifact.get("path")
                digest = artifact.get("sha256")
                if not isinstance(artifact_path, str) or not artifact_path.strip():
                    raise LedgerError("each artifact needs a path")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise LedgerError(f"invalid artifact SHA-256 for {artifact_path}")
                if root is not None:
                    root_resolved = root.expanduser().resolve()
                    candidate = root_resolved / artifact_path
                    if candidate.is_symlink():
                        raise LedgerError(f"symlink artifacts are not allowed: {artifact_path}")
                    resolved = candidate.resolve()
                    try:
                        resolved.relative_to(root_resolved)
                    except ValueError as exc:
                        raise LedgerError(f"artifact escapes root: {artifact_path}") from exc
                    actual = artifact_record(resolved)["sha256"]
                    if actual != digest:
                        raise LedgerError(f"artifact hash mismatch: {artifact_path}")
                # A shared Issue/research/check snapshot is provenance, not a
                # duplicate prediction. Only exclude known evidence references;
                # arbitrary output hashes keep the conservative legacy behavior.
                evidence_paths = set()
                source_issue = item.get("source_issue")
                issue = source_issue.get("snapshot", {}) if isinstance(source_issue, dict) else {}
                if isinstance(issue, dict):
                    evidence_paths.add(issue.get("path"))
                evidence_paths.update(item.get(field) for field in ("research_manifest_path", "completion_state_path"))
                for check in item.get("checks", {}).values():
                    if isinstance(check, dict):
                        evidence_paths.add(check.get("evidence_path"))
                if artifact_path not in evidence_paths or digest in item.get("submission_artifact_sha256", []):
                    values.add(digest)
        return values

    artifact_hashes = artifact_hashes_from(records)
    current_hashes = artifact_hashes_from([record])
    duplicates = sorted(hash_value for hash_value in current_hashes.intersection(artifact_hashes) if hash_value)
    if duplicates and allow_duplicate_artifact:
        if record.get("control") is not True:
            raise LedgerError("duplicate artifact requires control=true")
        reference = record.get("control_of") or record.get("reference_record_id")
        reason = record.get("control_reason") or record.get("reason")
        if not isinstance(reference, str) or not reference.strip():
            raise LedgerError("duplicate control requires control_of/reference_record_id")
        if not isinstance(reason, str) or not reason.strip():
            raise LedgerError("duplicate control requires a non-empty reason")
    if duplicates and not allow_duplicate_artifact:
        raise LedgerError(
            "artifact already exists in ledger; record the same artifact as a control explicitly: "
            + ", ".join(duplicates)
        )
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
    return canonical_json_digest(record)


def make_artifact_records(paths: Iterable[Path], root: Path | None = None) -> list[dict[str, Any]]:
    """Fingerprint artifacts in deterministic path order."""
    return [artifact_record(path, root=root) for path in sorted(paths, key=lambda item: str(item))]


def assert_new_submission(
    path: Path,
    ledger_path: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    """Reject a submission whose content hash already appears in the ledger.

    The check covers both generic ``artifacts`` entries and the explicit
    ``submission_artifact_sha256`` field.  It is intentionally independent of
    filenames and directories, so repackaging the same payload cannot create a
    new candidate by changing only its path.
    """
    if path.is_symlink():
        raise LedgerError("submission artifact must not be a symlink")
    digest = str(fingerprint_path(path)["sha256"])
    known: set[str] = set()
    for record in read_ledger(ledger_path):
        artifacts = record.get("artifacts", [])
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, dict) and isinstance(artifact.get("sha256"), str):
                    known.add(artifact["sha256"])
        submission_hashes = record.get("submission_artifact_sha256", [])
        if isinstance(submission_hashes, list):
            known.update(value for value in submission_hashes if isinstance(value, str))
    if root is not None:
        submit_root = root.expanduser().resolve() / "submit"
        if submit_root.is_dir():
            for candidate in submit_root.rglob("*"):
                if (
                    candidate.is_file()
                    and not candidate.is_symlink()
                    and (candidate.suffix.lower() in {".csv", ".zip", ".ipynb"} or candidate.name == "agent.py")
                ):
                    known.add(str(fingerprint_path(candidate)["sha256"]))
    if digest in known:
        raise LedgerError(
            f"submission artifact already exists in ledger ({digest}); create a distinct payload or explicit control"
        )
    return {
        "status": "PASS",
        "sha256": digest,
        "ledger": str(ledger_path.expanduser().resolve()),
    }
