"""Competition contract and submission validation.

This module is intentionally conservative: unknown top-level and validator
fields fail fast, while task-specific data metadata remains available to the
project's own evaluator. A malformed artifact fails before any upload step.
"""

from __future__ import annotations

import ast
import csv
import json
import math
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


class ContractError(ValueError):
    """Raised when a contract or an artifact violates a hard invariant."""


SUPPORTED_FORMATS = frozenset({"csv", "zip", "code_competition"})
SUPPORTED_DIRECTIONS = frozenset({"min", "max"})
UPLOAD_MODES = frozenset({"manual", "agent", "disabled"})


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(mapping).difference(allowed))
    if unknown:
        raise ContractError(f"unknown {name} field(s): {unknown}")


def _as_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value


def _string_list(value: object, name: str) -> list[str]:
    """Validate a list of non-empty strings used by a contract field."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ContractError(f"{name} must be a list of non-empty strings")
    return value


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a contract mapping.

    Required fields are kept small enough to support CSV, zip, and Code
    Competition tasks while preserving task-specific checks in the project.
    """
    if contract.get("schema_version") != 1:
        raise ContractError("schema_version must be 1")
    _reject_unknown(
        contract,
        {"schema_version", "competition", "platform", "data", "submission", "evaluation", "split", "policy", "research"},
        "contract",
    )
    competition = _as_mapping(contract.get("competition"), "competition")
    _reject_unknown(competition, {"name", "slug"}, "competition")
    for key in ("name", "slug"):
        if not isinstance(competition.get(key), str) or not competition[key].strip():
            raise ContractError(f"competition.{key} must be a non-empty string")
    platform = _as_mapping(contract.get("platform"), "platform")
    _reject_unknown(platform, {"kind"}, "platform")
    if not isinstance(platform.get("kind"), str) or not platform["kind"].strip():
        raise ContractError("platform.kind must be a non-empty string")
    submission = _as_mapping(contract.get("submission"), "submission")
    _reject_unknown(
        submission,
        {
            "format", "columns", "sample_order", "max_rows", "required_paths", "max_entries",
            "max_uncompressed_bytes", "required_files", "kernel_metadata", "required_metadata_keys",
            "code_file_field", "code_file_extensions", "code_file_must_exist", "python_entrypoint",
            "required_class", "required_methods", "upload_mode",
        },
        "submission",
    )
    submission_format = submission.get("format")
    if submission_format not in SUPPORTED_FORMATS:
        raise ContractError(
            f"submission.format must be one of {sorted(SUPPORTED_FORMATS)}"
        )
    columns = submission.get("columns")
    invalid_csv_columns = submission_format == "csv" and not isinstance(columns, list)
    invalid_code_columns = (
        submission_format == "code_competition"
        and columns is not None
        and not isinstance(columns, list)
    )
    if invalid_csv_columns or invalid_code_columns:
        raise ContractError("submission.columns must be a list for CSV contracts")
    upload_mode = submission.get("upload_mode", "manual")
    if not isinstance(upload_mode, str) or upload_mode not in UPLOAD_MODES:
        raise ContractError(f"submission.upload_mode must be one of {sorted(UPLOAD_MODES)}")
    for field in ("required_paths", "required_files", "required_metadata_keys", "code_file_extensions", "required_methods"):
        if field in submission:
            _string_list(submission[field], f"submission.{field}")
    for field in ("python_entrypoint", "required_class", "code_file_field"):
        if field in submission and (
            not isinstance(submission[field], str) or not submission[field].strip()
        ):
            raise ContractError(f"submission.{field} must be a non-empty string")
    if "code_file_must_exist" in submission and not isinstance(submission["code_file_must_exist"], bool):
        raise ContractError("submission.code_file_must_exist must be boolean")
    evaluation = _as_mapping(contract.get("evaluation"), "evaluation")
    _reject_unknown(
        evaluation,
        {"metric", "direction", "scope", "proxy_metrics", "official_formula", "formula", "metric_domain"},
        "evaluation",
    )
    if not isinstance(evaluation.get("metric"), str) or not evaluation["metric"].strip():
        raise ContractError("evaluation.metric must be a non-empty string")
    if evaluation.get("direction") not in SUPPORTED_DIRECTIONS:
        raise ContractError("evaluation.direction must be 'min' or 'max'")
    metric_domain = evaluation.get("metric_domain")
    if metric_domain is not None:
        metric_domain = _as_mapping(metric_domain, "evaluation.metric_domain")
        _reject_unknown(metric_domain, {"minimum", "maximum"}, "evaluation.metric_domain")
        for key in ("minimum", "maximum"):
            if key in metric_domain and (
                not isinstance(metric_domain[key], (int, float))
                or isinstance(metric_domain[key], bool)
                or not math.isfinite(float(metric_domain[key]))
            ):
                raise ContractError(f"evaluation.metric_domain.{key} must be numeric")
        if (
            "minimum" in metric_domain
            and "maximum" in metric_domain
            and float(metric_domain["minimum"]) > float(metric_domain["maximum"])
        ):
            raise ContractError("evaluation.metric_domain minimum exceeds maximum")
    split = _as_mapping(contract.get("split"), "split")
    _reject_unknown(
        split,
        {
            "strategy", "time_column", "primary_holdout", "mask_scenarios", "selection_must_be_nested",
            "nearby_well_spatial_holdout_required", "group_key", "fresh_replay_seeds_required",
        },
        "split",
    )
    if not isinstance(split.get("strategy"), str) or not split["strategy"].strip():
        raise ContractError("split.strategy must be a non-empty string")
    policy = contract.get("policy", {})
    policy = _as_mapping(policy, "policy")
    _reject_unknown(
        policy,
        {
            "external_data_requires_provenance", "test_labels_forbidden", "forbidden_inference_columns",
            "train_only_columns", "train_only_must_be_absent_from_test", "threshold_semantics",
            "official_gate_required", "proxy_cannot_claim_lb", "compile_import_smoke_required",
            "unsafe_fallback_forbidden", "external_upload_requires_user_confirmation",
            "hidden_test_safe_required", "same_id_lookup_forbidden", "required_checks",
            "train_only_must_exist_in_train", "seed_only_adoption_forbidden",
            "research_preflight_required", "completion_proof_required",
            "selection_proof_required_for_adoption",
            "issue_evidence_required",
        },
        "policy",
    )
    if "required_checks" in policy:
        required_checks = policy["required_checks"]
        if not isinstance(required_checks, list) or not all(
            isinstance(item, str) and item.strip() for item in required_checks
        ):
            raise ContractError("policy.required_checks must be a list of non-empty strings")
    if (
        "selection_proof_required_for_adoption" in policy
        and not isinstance(policy["selection_proof_required_for_adoption"], bool)
    ):
        raise ContractError(
            "policy.selection_proof_required_for_adoption must be boolean"
        )
    if "issue_evidence_required" in policy and not isinstance(policy["issue_evidence_required"], bool):
        raise ContractError("policy.issue_evidence_required must be boolean")
    platform_kind = str(platform["kind"]).lower()
    if upload_mode == "agent" and not platform_kind.startswith("kaggle"):
        raise ContractError(
            "non-Kaggle contracts must use submission.upload_mode='manual' or 'disabled'"
        )
    if upload_mode == "agent" and policy.get("external_upload_requires_user_confirmation") is not True:
        raise ContractError(
            "submission.upload_mode='agent' requires policy.external_upload_requires_user_confirmation=true"
        )
    if "research" in contract:
        research = _as_mapping(contract["research"], "research")
        _reject_unknown(research, {"required_sections", "minimum_sources"}, "research")
        required_sections = research.get("required_sections")
        if not isinstance(required_sections, list) or not all(
            isinstance(item, str) and item.strip() for item in required_sections
        ):
            raise ContractError("research.required_sections must be a list of non-empty strings")
        minimum_sources = research.get("minimum_sources", 1)
        if not isinstance(minimum_sources, int) or isinstance(minimum_sources, bool) or minimum_sources < 1:
            raise ContractError("research.minimum_sources must be a positive integer")
    return contract


def load_contract(path: Path) -> dict[str, Any]:
    """Load, validate, and return a JSON contract."""
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON contract: {path}: {exc}") from exc
    if not isinstance(contract, dict):
        raise ContractError("contract root must be an object")
    return validate_contract(contract)


def _column_specs(contract: dict[str, Any]) -> list[dict[str, Any]]:
    specs = contract["submission"].get("columns", [])
    if all(isinstance(item, str) for item in specs):
        return [{"name": item} for item in specs]
    normalized: list[dict[str, Any]] = []
    for item in specs:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ContractError("each submission column needs a name")
        normalized.append(item)
    return normalized


def _float_value(raw: str, column: dict[str, Any], row_number: int) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            f"row {row_number}: {column['name']} must be numeric, got {raw!r}"
        ) from exc
    if column.get("finite", True) and not math.isfinite(value):
        raise ContractError(f"row {row_number}: {column['name']} must be finite")
    if "minimum" in column and value < float(column["minimum"]):
        raise ContractError(f"row {row_number}: {column['name']} below minimum")
    if "maximum" in column and value > float(column["maximum"]):
        raise ContractError(f"row {row_number}: {column['name']} above maximum")
    if column.get("integer") and value != int(value):
        raise ContractError(f"row {row_number}: {column['name']} must be integer")
    return value


def _validate_csv(
    path: Path,
    contract: dict[str, Any],
    sample_path: Path | None = None,
) -> dict[str, Any]:
    if path.read_bytes()[:3] == b"\xef\xbb\xbf":
        raise ContractError("CSV must not contain a UTF-8 BOM")
    specs = _column_specs(contract)
    required = [str(spec["name"]) for spec in specs]
    if not required:
        raise ContractError("CSV contract must declare submission.columns")
    if contract["submission"].get("sample_order", False) and sample_path is None:
        raise ContractError("this contract requires --sample for ID/order validation")
    rows = 0
    ids: set[str] = set()
    id_index = next(
        (index for index, spec in enumerate(specs) if spec.get("role") == "id"),
        0,
    )
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ContractError("submission CSV is empty") from exc
        if header != required:
            raise ContractError(f"columns must be {required}, got {header}")
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(required):
                raise ContractError(
                    f"row {row_number}: expected {len(required)} fields, got {len(row)}"
                )
            identifier = row[id_index]
            if not identifier:
                raise ContractError(f"row {row_number}: ID must be non-empty")
            if identifier in ids:
                raise ContractError(f"row {row_number}: duplicate ID {identifier!r}")
            ids.add(identifier)
            for index, spec in enumerate(specs):
                if index == id_index and spec.get("type", "string") == "string":
                    continue
                if spec.get("type", "float") in {"float", "number", "probability"}:
                    _float_value(row[index], spec, row_number)
                elif spec.get("type") == "integer":
                    spec_with_integer = {**spec, "integer": True}
                    _float_value(row[index], spec_with_integer, row_number)
                elif not row[index]:
                    raise ContractError(f"row {row_number}: {spec['name']} is empty")
            rows += 1
    max_rows = contract["submission"].get("max_rows")
    if max_rows is not None and rows > int(max_rows):
        raise ContractError(f"row count {rows} exceeds max_rows {max_rows}")
    if sample_path is not None:
        sample_ids = _read_sample_ids(sample_path, required, id_index)
        actual_ids = _read_csv_ids(path, id_index)
        if actual_ids != sample_ids:
            raise ContractError("submission IDs do not exactly match sample order")
    return {"status": "PASS", "format": "csv", "rows": rows, "columns": required}


def _read_sample_ids(path: Path, required: list[str], id_index: int) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ContractError("sample CSV is empty") from exc
        if header != required:
            raise ContractError(f"sample columns must be {required}, got {header}")
        values = [row[id_index] for row in reader if row]
        if len(values) != len(set(values)):
            raise ContractError("sample IDs must be unique")
        return values


def _read_csv_ids(path: Path, id_index: int) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader)
        return [row[id_index] for row in reader if row]


def _safe_zip_names(path: Path, contract: dict[str, Any]) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ContractError("zip contains duplicate entry names")
        bad: list[str] = []
        for info in archive.infolist():
            normalized_name = info.filename.replace("\\", "/")
            name = PurePosixPath(normalized_name)
            mode = (info.external_attr >> 16) & 0o170000
            windows_absolute = bool(re.match(r"^[A-Za-z]:", normalized_name))
            unc_absolute = normalized_name.startswith("//")
            if (
                name.is_absolute()
                or windows_absolute
                or unc_absolute
                or ".." in name.parts
                or mode == stat.S_IFLNK
            ):
                bad.append(info.filename)
        if bad:
            raise ContractError(f"unsafe zip entries: {bad[:3]}")
        limits = contract["submission"]
        max_entries = limits.get("max_entries")
        if max_entries is not None and len(names) > int(max_entries):
            raise ContractError(f"zip entry count exceeds max_entries {max_entries}")
        max_bytes = limits.get("max_uncompressed_bytes")
        if max_bytes is not None and sum(i.file_size for i in archive.infolist()) > int(max_bytes):
            raise ContractError("zip uncompressed size exceeds max_uncompressed_bytes")
        required_paths = limits.get("required_paths", [])
        for required in required_paths:
            if required not in names:
                raise ContractError(f"required zip path is missing: {required}")
        entrypoint = limits.get("python_entrypoint")
        if entrypoint:
            normalized_entrypoint = str(entrypoint).replace("\\", "/")
            if normalized_entrypoint not in names:
                raise ContractError(f"required Python entrypoint is missing: {entrypoint}")
            try:
                source = archive.read(normalized_entrypoint).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractError(f"Python entrypoint is not valid UTF-8: {entrypoint}") from exc
            try:
                tree = ast.parse(source, filename=normalized_entrypoint)
                compile(tree, normalized_entrypoint, "exec")
            except SyntaxError as exc:
                raise ContractError(f"Python entrypoint does not compile: {entrypoint}: {exc}") from exc
            required_class = limits.get("required_class")
            required_methods = limits.get("required_methods", [])
            if required_class:
                classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == required_class]
                if not classes:
                    raise ContractError(f"Python entrypoint is missing class {required_class}")
                methods = {
                    node.name
                    for node in classes[0].body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                missing_methods = sorted(set(required_methods).difference(methods))
                if missing_methods:
                    raise ContractError(
                        f"Python entrypoint class {required_class} is missing methods: {missing_methods}"
                    )
        return names


def _validate_code_competition(path: Path, contract: dict[str, Any]) -> dict[str, Any]:
    if not path.is_dir():
        raise ContractError("code_competition artifact must be a directory")
    submission = contract["submission"]
    root_resolved = path.resolve()
    missing: list[str] = []
    for name in submission.get("required_files", []):
        child = path / name
        if child.is_symlink() or not child.is_file():
            missing.append(name)
            continue
        try:
            child.resolve().relative_to(root_resolved)
        except ValueError as exc:
            raise ContractError(f"required Code Competition file escapes artifact root: {name}") from exc
    if missing:
        raise ContractError(f"required Code Competition files are missing: {missing}")
    metadata_name = submission.get("kernel_metadata", "kernel-metadata.json")
    metadata_path = path / metadata_name
    if metadata_path.is_symlink():
        raise ContractError(f"symlink is not allowed for {metadata_name}")
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid {metadata_name}") from exc
        if not isinstance(metadata, dict):
            raise ContractError(f"{metadata_name} must contain an object")
        for key in submission.get("required_metadata_keys", []):
            if key not in metadata:
                raise ContractError(f"{metadata_name} is missing key {key}")
        code_file_field = submission.get("code_file_field")
        if code_file_field:
            code_file = metadata.get(code_file_field)
            if not isinstance(code_file, str) or not code_file.strip():
                raise ContractError(f"{metadata_name}.{code_file_field} must be a non-empty path")
            normalized_code_file = code_file.replace("\\", "/")
            code_path = PurePosixPath(normalized_code_file)
            if (
                code_path.is_absolute()
                or bool(re.match(r"^[A-Za-z]:", normalized_code_file))
                or normalized_code_file.startswith("//")
                or ".." in code_path.parts
            ):
                raise ContractError(f"unsafe {metadata_name}.{code_file_field}: {code_file}")
            extensions = submission.get("code_file_extensions", [])
            if extensions and Path(normalized_code_file).suffix not in extensions:
                raise ContractError(
                    f"{metadata_name}.{code_file_field} must use one of {extensions}"
                )
            code_path_on_disk = path / normalized_code_file
            if code_path_on_disk.is_symlink():
                raise ContractError(f"symlink is not allowed for Code Competition code file: {code_file}")
            if submission.get("code_file_must_exist", True) and not code_path_on_disk.is_file():
                raise ContractError(f"Code Competition code file is missing: {code_file}")
            try:
                code_path_on_disk.resolve().relative_to(root_resolved)
            except ValueError as exc:
                raise ContractError(f"Code Competition code file escapes artifact root: {code_file}") from exc
    return {
        "status": "PASS",
        "format": "code_competition",
        "required_files": submission.get("required_files", []),
    }


def validate_submission(
    path: Path,
    contract: dict[str, Any],
    sample_path: Path | None = None,
) -> dict[str, Any]:
    """Validate a submission artifact against a loaded contract."""
    validate_contract(contract)
    resolved = path.expanduser().resolve()
    submission_format = contract["submission"]["format"]
    if submission_format == "csv":
        if not resolved.is_file():
            raise ContractError(f"submission file not found: {resolved}")
        result = _validate_csv(resolved, contract, sample_path)
    elif submission_format == "zip":
        if not resolved.is_file() or resolved.suffix.lower() != ".zip":
            raise ContractError("zip submission must be an existing .zip file")
        names = _safe_zip_names(resolved, contract)
        result = {"status": "PASS", "format": "zip", "entries": len(names)}
    else:
        result = _validate_code_competition(resolved, contract)
    upload_mode = contract["submission"].get("upload_mode", "manual")
    result.update(
        {
            "artifact_status": "ready",
            "external_submission_status": {
                "manual": "manual_pending",
                "agent": "not_requested",
                "disabled": "not_requested",
            }[upload_mode],
            "upload_mode": upload_mode,
            "external_upload_attempted": False,
            "next_action": {
                "manual": "human_upload_required",
                "agent": "user_confirmation_required_before_external_upload",
                "disabled": "external_upload_disabled",
            }[upload_mode],
        }
    )
    return result


def validate_feature_boundary(
    train_columns: list[str],
    test_columns: list[str],
    contract: dict[str, Any],
    feature_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Check that forbidden train-only columns cannot enter inference."""
    policy = contract.get("policy", {})
    forbidden = set(policy.get("forbidden_inference_columns", []))
    train_only = set(policy.get("train_only_columns", []))
    if feature_columns is not None:
        illegal = sorted(forbidden.intersection(feature_columns))
        if illegal:
            raise ContractError(f"forbidden columns used as features: {illegal}")
    present_in_test = sorted(train_only.intersection(test_columns))
    if present_in_test and policy.get("train_only_must_be_absent_from_test", False):
        raise ContractError(f"train-only columns unexpectedly present in test: {present_in_test}")
    missing_train_only = sorted(train_only.difference(train_columns))
    if missing_train_only and policy.get("train_only_must_exist_in_train", False):
        raise ContractError(f"declared train-only columns absent from train: {missing_train_only}")
    return {
        "status": "PASS",
        "forbidden_features": sorted(forbidden),
        "train_only_columns": sorted(train_only),
    }


def validate_id_disjointness(
    train_ids: list[str], test_ids: list[str]
) -> dict[str, Any]:
    """Check that train and test identifiers do not overlap."""
    overlap = sorted(set(train_ids).intersection(test_ids))
    if overlap:
        raise ContractError(f"train/test ID overlap: {overlap[:5]}")
    return {"status": "PASS", "train_ids": len(train_ids), "test_ids": len(test_ids)}
