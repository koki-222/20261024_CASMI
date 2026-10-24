#!/usr/bin/env python3
"""Operate a Kaggle Notebook PDCA loop without storing credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class KaggleNotebookError(ValueError):
    """Raised when the Kaggle Notebook configuration or operation is unsafe."""


KERNEL_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,49}\Z")
OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KaggleNotebookError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KaggleNotebookError(f"{field} must be a non-empty string")
    return value.strip()


def _integer(value: object, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise KaggleNotebookError(f"{field} must be an integer >= {minimum}")
    return value


def _reject_unknown(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise KaggleNotebookError(f"unknown {field} field(s): {unknown}")


def _safe_path(root: Path, relative: str, field: str, *, must_exist: bool) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise KaggleNotebookError(f"{field} must be a safe repository-relative path")
    root_resolved = root.expanduser().resolve()
    resolved = (root_resolved / candidate).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise KaggleNotebookError(f"{field} escapes the repository") from exc
    if must_exist and not resolved.exists():
        raise KaggleNotebookError(f"{field} does not exist: {relative}")
    return resolved


def load_config(path: Path, root: Path) -> dict[str, Any]:
    """Load and validate the task-specific Kaggle Notebook configuration."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KaggleNotebookError(f"configuration is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise KaggleNotebookError(f"invalid configuration JSON: {exc}") from exc
    config = _mapping(config, "configuration")
    _reject_unknown(
        config,
        {"schema_version", "competition_slug", "kernel", "quota", "execution", "output_root"},
        "configuration",
    )
    if config.get("schema_version") != 1:
        raise KaggleNotebookError("schema_version must be 1")
    _text(config.get("competition_slug"), "competition_slug")

    kernel = _mapping(config.get("kernel"), "kernel")
    _reject_unknown(
        kernel,
        {
            "slug",
            "owner",
            "title",
            "source_dir",
            "code_file",
            "language",
            "kernel_type",
            "is_private",
            "enable_gpu",
            "enable_internet",
            "dataset_sources",
            "kernel_sources",
        },
        "kernel",
    )
    slug = _text(kernel.get("slug"), "kernel.slug")
    if not KERNEL_SLUG_RE.fullmatch(slug):
        raise KaggleNotebookError("kernel.slug must use lowercase letters, digits, and hyphens")
    owner = _text(kernel.get("owner"), "kernel.owner")
    if not OWNER_RE.fullmatch(owner):
        raise KaggleNotebookError("kernel.owner contains unsupported characters")
    _text(kernel.get("title"), "kernel.title")
    source_dir = _safe_path(
        root,
        _text(kernel.get("source_dir"), "kernel.source_dir"),
        "kernel.source_dir",
        must_exist=True,
    )
    if not source_dir.is_dir():
        raise KaggleNotebookError("kernel.source_dir must be a directory")
    code_file = _text(kernel.get("code_file"), "kernel.code_file")
    if Path(code_file).name != code_file or Path(code_file).suffix != ".ipynb":
        raise KaggleNotebookError("kernel.code_file must be one .ipynb filename")
    if not (source_dir / code_file).is_file():
        raise KaggleNotebookError(f"kernel.code_file is missing below source_dir: {code_file}")
    if kernel.get("language") != "python" or kernel.get("kernel_type") != "notebook":
        raise KaggleNotebookError("analysis execution must use a Python notebook")
    if kernel.get("is_private") is not True:
        raise KaggleNotebookError("the analysis notebook must remain private")
    if kernel.get("enable_gpu") is not True:
        raise KaggleNotebookError("the analysis notebook must enable a GPU")
    if kernel.get("enable_internet") is not False:
        raise KaggleNotebookError("the notebook must keep internet disabled for submission parity")
    for field in ("dataset_sources", "kernel_sources"):
        values = kernel.get(field)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise KaggleNotebookError(f"kernel.{field} must be a string list")

    quota = _mapping(config.get("quota"), "quota")
    _reject_unknown(
        quota,
        {
            "colab_linked",
            "exact_weekly_gpu_hours",
            "remaining_gpu_hours",
            "reset_at",
            "evidence_kind",
            "observed_at",
        },
        "quota",
    )
    if quota.get("colab_linked") is not True:
        raise KaggleNotebookError("quota.colab_linked must be true for this operating profile")
    for field in ("exact_weekly_gpu_hours", "remaining_gpu_hours"):
        value = quota.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        ):
            raise KaggleNotebookError(f"quota.{field} must be null or non-negative")
    if quota.get("remaining_gpu_hours") is not None and quota.get("observed_at") is None:
        raise KaggleNotebookError("quota.observed_at is required when remaining_gpu_hours is known")
    _text(quota.get("evidence_kind"), "quota.evidence_kind")

    execution = _mapping(config.get("execution"), "execution")
    _reject_unknown(
        execution,
        {
            "official_max_runtime_minutes",
            "planning_limit_minutes",
            "checkpoint_interval_minutes",
            "required_outputs",
        },
        "execution",
    )
    official_limit = _integer(
        execution.get("official_max_runtime_minutes"), "execution.official_max_runtime_minutes", 1
    )
    planning_limit = _integer(
        execution.get("planning_limit_minutes"), "execution.planning_limit_minutes", 1
    )
    if planning_limit > official_limit * 0.8:
        raise KaggleNotebookError(
            "planning_limit_minutes must preserve at least 20% runtime margin"
        )
    checkpoint_interval = _integer(
        execution.get("checkpoint_interval_minutes"), "execution.checkpoint_interval_minutes", 1
    )
    if checkpoint_interval > 30:
        raise KaggleNotebookError("checkpoint_interval_minutes must be <= 30")
    required_outputs = execution.get("required_outputs")
    if not isinstance(required_outputs, list) or not required_outputs:
        raise KaggleNotebookError("execution.required_outputs must be a non-empty list")
    for index, name in enumerate(required_outputs):
        text = _text(name, f"execution.required_outputs[{index}]")
        if Path(text).name != text:
            raise KaggleNotebookError("required outputs must be filenames")

    _safe_path(
        root, _text(config.get("output_root"), "output_root"), "output_root", must_exist=False
    )
    return config


def build_metadata(config: dict[str, Any], owner: str | None = None) -> dict[str, Any]:
    """Build Kaggle kernel metadata from validated task configuration."""
    owner = owner or config["kernel"]["owner"]
    if not OWNER_RE.fullmatch(owner):
        raise KaggleNotebookError("owner contains unsupported characters")
    kernel = config["kernel"]
    return {
        "id": f"{owner}/{kernel['slug']}",
        "title": kernel["title"],
        "code_file": kernel["code_file"],
        "language": kernel["language"],
        "kernel_type": kernel["kernel_type"],
        "is_private": kernel["is_private"],
        "enable_gpu": kernel["enable_gpu"],
        "enable_internet": kernel["enable_internet"],
        "dataset_sources": kernel["dataset_sources"],
        "competition_sources": [config["competition_slug"]],
        "kernel_sources": kernel["kernel_sources"],
    }


def _run_kaggle(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("kaggle")
    if executable is None:
        raise KaggleNotebookError("Kaggle CLI is not installed or not on PATH")
    result = subprocess.run(
        [executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = " ".join((result.stderr or result.stdout).strip().split())
        raise KaggleNotebookError(f"Kaggle CLI failed ({result.returncode}): {message[:500]}")
    return result


def check_connection(config: dict[str, Any]) -> dict[str, Any]:
    """Verify CLI authentication and accepted competition access without mutation."""
    version = _run_kaggle(["--version"]).stdout.strip()
    _run_kaggle(["competitions", "files", "-c", config["competition_slug"], "--page-size", "1"])
    return {
        "status": "PASS",
        "authenticated": True,
        "competition_access": True,
        "competition_slug": config["competition_slug"],
        "kaggle_cli": version,
    }


def _kernel_ref(config: dict[str, Any], owner: str | None = None) -> str:
    owner = owner or config["kernel"]["owner"]
    if not OWNER_RE.fullmatch(owner):
        raise KaggleNotebookError("owner contains unsupported characters")
    return f"{owner}/{config['kernel']['slug']}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_output(
    config: dict[str, Any], root: Path, owner: str | None, run_id: str
) -> dict[str, Any]:
    """Download one completed kernel output and write its immutable artifact manifest."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise KaggleNotebookError("run_id contains unsupported characters")
    output_root = _safe_path(root, config["output_root"], "output_root", must_exist=False)
    destination = output_root / run_id
    if destination.exists() and any(destination.iterdir()):
        raise KaggleNotebookError(f"output directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    kernel_ref = _kernel_ref(config, owner)
    _run_kaggle(["kernels", "output", kernel_ref, "-p", str(destination), "--quiet"])
    files = []
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        if path.name == "artifact_manifest.json":
            continue
        files.append(
            {
                "path": str(path.relative_to(destination)),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    found = {item["path"] for item in files}
    required = list(config["execution"]["required_outputs"])
    missing = sorted(set(required).difference(found))
    manifest = {
        "schema_version": 1,
        "status": "PASS" if not missing else "FAIL",
        "kernel_ref": kernel_ref,
        "run_id": run_id,
        "collected_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "required_outputs": required,
        "missing_required_outputs": missing,
        "files": files,
    }
    manifest_path = destination / "artifact_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if missing:
        raise KaggleNotebookError(f"kernel output is incomplete: missing {missing}")
    return manifest


def _dump(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("automation/kaggle_notebook.json"))
    parser.add_argument("--root", type=Path, default=Path("."))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("auth-check")

    render = subparsers.add_parser("render-metadata")
    render.add_argument("--owner")

    push = subparsers.add_parser("push")
    push.add_argument("--owner")
    push.add_argument("--confirm-push", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--owner")

    logs = subparsers.add_parser("logs")
    logs.add_argument("--owner")

    output = subparsers.add_parser("collect-output")
    output.add_argument("--owner")
    output.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the requested Kaggle Notebook control operation."""
    args = _parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        config_path = args.config if args.config.is_absolute() else root / args.config
        config = load_config(config_path, root)
        if args.command == "validate":
            _dump({"status": "PASS", "config": str(config_path)})
        elif args.command == "auth-check":
            _dump(check_connection(config))
        elif args.command == "render-metadata":
            metadata = build_metadata(config, args.owner)
            source_dir = root / config["kernel"]["source_dir"]
            output = source_dir / "kernel-metadata.json"
            output.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            _dump({"status": "PASS", "path": str(output), "kernel_ref": metadata["id"]})
        elif args.command == "push":
            if not args.confirm_push:
                raise KaggleNotebookError("push requires --confirm-push")
            expected = build_metadata(config, args.owner)
            source_dir = root / config["kernel"]["source_dir"]
            metadata_path = source_dir / "kernel-metadata.json"
            if not metadata_path.is_file():
                raise KaggleNotebookError("render kernel-metadata.json before push")
            actual = json.loads(metadata_path.read_text(encoding="utf-8"))
            if actual != expected:
                raise KaggleNotebookError(
                    "kernel-metadata.json does not match the validated configuration"
                )
            result = _run_kaggle(["kernels", "push", "-p", str(source_dir)])
            print(result.stdout, end="")
        elif args.command == "status":
            result = _run_kaggle(["kernels", "status", _kernel_ref(config, args.owner)])
            print(result.stdout, end="")
        elif args.command == "logs":
            result = _run_kaggle(["kernels", "logs", _kernel_ref(config, args.owner)])
            print(result.stdout, end="")
        elif args.command == "collect-output":
            _dump(collect_output(config, root, args.owner, args.run_id))
        else:
            raise KaggleNotebookError(f"unsupported command: {args.command}")
    except (KaggleNotebookError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
