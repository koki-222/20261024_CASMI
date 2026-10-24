"""Competition bootstrap, published handoff and bounded official-score loops.

This module does not scrape sites, generate hypotheses, create GitHub issues,
or upload submissions. Those actions remain explicit agent/user workflows.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .contract import load_contract
from .research import DEFAULT_REQUIRED_SECTIONS, validate_research_manifest

TRIGGER = "PDCA開始"
STATE = ".local/competition/workflow.json"
CATEGORIES = {
    "official",
    "notebooks",
    "discussions",
    "models_data",
    "papers",
    "prior_solutions",
}


class WorkflowError(ValueError):
    """Reject incomplete handoffs, unapproved starts and false achievement."""


def _time(value: str) -> datetime:
    """Parse one timezone-aware instant."""
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise WorkflowError("timestamp must be ISO-8601") from exc
    if result.tzinfo is None:
        raise WorkflowError("timestamp needs a timezone")
    return result


def _now() -> str:
    """Return the observed UTC clock."""
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, field: str) -> str:
    """Require substantive text, not a template placeholder."""
    if not isinstance(value, str) or not value.strip() or "replace-with" in value:
        raise WorkflowError(f"{field} is required")
    return value.strip()


def _number(value: object, field: str) -> float:
    """Require a finite number without accepting booleans."""
    if type(value) not in {int, float} or not math.isfinite(value):
        raise WorkflowError(f"{field} must be finite numeric")
    return float(value)


def _path(root: Path, relative: str) -> Path:
    """Resolve a relative path without traversal or symlink components."""
    path = Path(_text(relative, "path"))
    if path.is_absolute() or ".." in path.parts:
        raise WorkflowError("path must remain below the repository")
    current = root.resolve()
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise WorkflowError(f"symlink evidence is not allowed: {relative}")
    return current


def _read(path: Path) -> dict[str, Any]:
    """Read a finite JSON object."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkflowError("expected a JSON object")
    json.dumps(value, allow_nan=False)
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace host-local state, not scientific artifacts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def snapshot(root: Path, relative: str) -> dict[str, str]:
    """Bind one actual file to its repository-relative name."""
    path = _path(root, relative)
    return {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _verify(root: Path, ref: dict[str, Any]) -> Path:
    """Verify one immutable artifact reference."""
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise WorkflowError("snapshot requires path and sha256")
    if snapshot(root, ref["path"]) != ref:
        raise WorkflowError(f"snapshot changed: {ref['path']}")
    return _path(root, ref["path"])


def _state(root: Path) -> dict[str, Any]:
    """Load the sole lifecycle projection and verify its project binding."""
    state = _read(_path(root, STATE))
    _verify(root, state["project"])
    return state


def _locked(operation: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Serialize lifecycle mutations so score/review updates cannot be lost."""

    @wraps(operation)
    def locked(root: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Hold one host-local lifecycle lock across the complete transition."""
        path = _path(root, ".local/competition/workflow.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return operation(root, *args, **kwargs)

    return locked


def _git(root: Path, *args: str) -> str:
    """Execute bounded, shell-free Git operations without leaking diagnostics."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError(
            f"Git operation failed: {args[0]}; inspect authentication/remote locally"
        ) from exc


@_locked
def initialize(root: Path, url: str) -> dict[str, Any]:
    """Initialize one competition without overwriting an existing project."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WorkflowError(
            "provide a canonical HTTPS competition URL without credentials/query/fragment"
        )
    project = {
        "schema_version": 1,
        "competition_url": url.rstrip("/"),
        "contract_path": "automation/contracts/current.json",
    }
    destination = _path(root, "automation/project.json")
    if destination.exists() and _read(destination) != project:
        raise WorkflowError(
            "existing competition differs; do not transplant over active state"
        )
    if _path(root, STATE).exists():
        return _state(root)
    _write(destination, project)
    state = {
        "schema_version": 1,
        "project": snapshot(root, "automation/project.json"),
        "phase": "adapting",
        "created_at": _now(),
    }
    _write(_path(root, STATE), state)
    return state


def validate_package(root: Path, package_path: str) -> dict[str, Any]:
    """Check adaptation, source coverage, evaluation and redistribution records."""
    package = _read(_path(root, package_path))
    if package.get("schema_version") != 1:
        raise WorkflowError("bootstrap package schema_version must be 1")
    for name in ("contract", "research", "adaptation", "evaluation"):
        _verify(root, package[name])
    project = _read(_path(root, "automation/project.json"))
    if package["contract"]["path"] != project["contract_path"]:
        raise WorkflowError("package contract differs from project")
    contract = load_contract(_path(root, package["contract"]["path"]))
    for value in (
        contract["competition"]["name"],
        contract["competition"]["slug"],
        contract["evaluation"]["metric"],
        contract["split"]["strategy"],
    ):
        _text(value, "adapted contract value")
    manifest_path = _path(root, package["research"]["path"])
    research_policy = contract.get("research", {})
    required = tuple(
        dict.fromkeys(
            [*DEFAULT_REQUIRED_SECTIONS, *research_policy.get("required_sections", [])]
        )
    )
    validate_research_manifest(
        manifest_path,
        root,
        required_sections=required,
        minimum_sources=research_policy.get("minimum_sources", 1),
    )
    if _read(manifest_path).get("competition_slug") != contract["competition"]["slug"]:
        raise WorkflowError("research belongs to another competition")
    evaluation = _read(_path(root, package["evaluation"]["path"]))
    if evaluation.get("mode") not in {"local", "hybrid", "official_feedback"}:
        raise WorkflowError("evaluation mode must be local/hybrid/official_feedback")
    for key in (
        "population",
        "leakage_boundary",
        "exposure",
        "selection_rule",
        "limitations",
    ):
        _text(evaluation.get(key), f"evaluation.{key}")
    coverage = package.get("coverage", [])
    if (
        len(coverage) != len(CATEGORIES)
        or {row["category"] for row in coverage} != CATEGORIES
    ):
        raise WorkflowError("coverage must inventory all six source categories")
    for row in coverage:
        if row.get("status") not in {
            "complete",
            "partial",
            "not_applicable",
            "unavailable",
        }:
            raise WorkflowError("invalid coverage status")
        _text(row.get("reason"), "coverage.reason")
        inventory = _read(_verify(root, row["inventory"]))
        if inventory.get("schema_version") != 1 or not isinstance(
            inventory.get("items"), list
        ):
            raise WorkflowError("inventory needs schema_version=1 and items")
        _text(inventory.get("scope"), "inventory search scope")
        _time(inventory.get("observed_at"))
        seen_sources = set()
        for source in inventory["items"]:
            identity = (
                _text(source.get("uri"), "source URI"),
                _text(source.get("version"), "source version"),
            )
            if identity in seen_sources:
                raise WorkflowError("duplicate source/version in inventory")
            seen_sources.add(identity)
            if source.get("status") == "archived":
                _verify(root, source["artifact"])
            elif source.get("status") in {"restricted", "unavailable"}:
                _text(source.get("reason"), "source gap reason")
            else:
                raise WorkflowError("source must be archived/restricted/unavailable")
        if row["status"] == "complete" and (
            inventory.get("pagination_complete") is not True
            or any(item["status"] != "archived" for item in inventory["items"])
        ):
            raise WorkflowError(
                "complete coverage requires completed pagination and archived bodies"
            )
        if row["category"] == "official" and not inventory["items"]:
            raise WorkflowError("official inventory cannot be empty")
        if row["category"] == "official" and row["status"] != "complete":
            raise WorkflowError("official contract sources must be complete")
    publications = package.get("publication", [])
    if not publications or len(
        {row["artifact"]["path"] for row in publications}
    ) != len(publications):
        raise WorkflowError("publication requires distinct reviewed artifacts")
    for item in publications:
        _verify(root, item["artifact"])
        if item.get("redistribution") != "allowed":
            raise WorkflowError(
                "restricted sources cannot be pushed; publish a lawful summary/index instead"
            )
        _text(item.get("basis"), "publication license/permission basis")
        if _path(root, item["artifact"]["path"]).stat().st_size > 10 * 1024 * 1024:
            raise WorkflowError(
                "large sources need an approved external archive and hash index"
            )
    return package


@_locked
def mark_ready(root: Path, package_path: str, refresh: bool = False) -> dict[str, Any]:
    """Advance directly from adaptation/research to the reviewed publishing gate."""
    state = _state(root)
    refreshing = state["phase"] == "waiting_for_issues" and refresh
    if state["phase"] not in {"adapting", "researching", "ready_to_publish"} and not refreshing:
        raise WorkflowError("bootstrap is already handed off or PDCA has started")
    validate_package(root, package_path)
    if refreshing:
        if not state.get("publication"):
            raise WorkflowError("refresh requires a verified prior publication")
        state.setdefault("previous_publications", []).append(
            {key: state[key] for key in ("publication", "package", "handoff") if key in state}
        )
        state.pop("publication", None)
        state.pop("handoff", None)
    state.update(phase="ready_to_publish", package=snapshot(root, package_path))
    _write(_path(root, STATE), state)
    return state


@_locked
def publish(root: Path, remote: str, branch: str, authorized: bool) -> dict[str, Any]:
    """Push an already reviewed clean commit once, then verify the remote ref."""
    state = _state(root)
    if state["phase"] != "ready_to_publish" or not authorized:
        raise WorkflowError(
            "reviewed package and explicit --authorize-push are required"
        )
    if remote.startswith("-") or branch.startswith("-"):
        raise WorkflowError("remote/branch cannot be command options")
    _git(root, "check-ref-format", f"refs/heads/{branch}")
    if _git(root, "branch", "--show-current") != branch or _git(
        root, "status", "--porcelain"
    ):
        raise WorkflowError(
            "commit reviewed changes first; branch must match and worktree must be clean"
        )
    _verify(root, state["package"])
    package = validate_package(root, state["package"]["path"])
    head = _git(root, "rev-parse", "HEAD")
    refs = [
        state["project"],
        state["package"],
        *(package[key] for key in ("contract", "research", "adaptation", "evaluation")),
        *(row["inventory"] for row in package["coverage"]),
        *(row["artifact"] for row in package["publication"]),
    ]
    allowed_research = {ref["path"] for ref in refs}
    tracked = _git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    if any(
        path.startswith("research/") and path not in allowed_research
        for path in tracked
    ):
        raise WorkflowError(
            "every tracked research file needs an explicit publication review"
        )
    for ref in refs:
        actual = _git(root, "rev-parse", f"{head}:{ref['path']}")
        if actual != _git(root, "hash-object", str(_verify(root, ref))):
            raise WorkflowError(
                "handoff artifacts must be included in the reviewed commit"
            )
    url = _git(root, "remote", "get-url", remote)
    if url.startswith("git@github.com:"):
        repository = "https://github.com/" + url.split(":", 1)[1].removesuffix(".git")
    else:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise WorkflowError(
                "use a credential-free HTTPS remote or GitHub SSH remote"
            )
        repository = url.removesuffix(".git")
    _git(root, "push", "--porcelain", remote, f"{head}:refs/heads/{branch}")
    observed = _git(root, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    if observed.split()[:1] != [head]:
        raise WorkflowError(
            "remote commit not verified; do not issue the handoff prompt"
        )
    state.update(
        phase="waiting_for_issues",
        publication={
            "repository": repository,
            "commit": head,
            "branch": branch,
            "remote": remote,
            "verified_at": _now(),
        },
    )
    _write(_path(root, STATE), state)
    return state


@_locked
def handoff(root: Path) -> dict[str, Any]:
    """Render a commit-pinned ChatGPT Pro prompt only after verified publication."""
    state = _state(root)
    if state["phase"] != "waiting_for_issues" or not state.get("publication"):
        raise WorkflowError("push and remote verification must precede the Pro handoff")
    _verify(root, state["package"])
    package = validate_package(root, state["package"]["path"])
    publication = state["publication"]
    repository, commit = publication["repository"], publication["commit"]
    template = _path(root, "docs/workflow/PRO_ISSUES_PROMPT.md").read_text(
        encoding="utf-8"
    )
    paths = list(
        dict.fromkeys(
            [
                state["package"]["path"],
                *(
                    package[name]["path"]
                    for name in ("adaptation", "evaluation", "research", "contract")
                ),
                *(row["inventory"]["path"] for row in package["coverage"]),
                *(
                    row["artifact"]["path"]
                    for row in package["publication"]
                    if row["artifact"]["path"] in {
                        "research/SOURCE_MAP.md", "research/LEGACY_REVIEW.md",
                        "research/legacy/comparison_evidence.json", "research/legacy/reuse_policy.json",
                        "research/legacy/exposure.json", "research/legacy/official_observations.json",
                    }
                ),
            ]
        )
    )
    files = "\n".join(
        f"- [{path}]({repository}/blob/{commit}/{quote(path, safe='/')})"
        if urlsplit(repository).hostname == "github.com"
        else f"- {path} (commit {commit})"
        for path in paths
    )
    gaps = "\n".join(
        f"- {row['category']}: {row['status']} — {row['reason']}"
        for row in package["coverage"]
    )
    prompt = (
        template.replace(
            "{{COMPETITION_URL}}",
            _read(_path(root, "automation/project.json"))["competition_url"],
        )
        .replace("{{REPOSITORY}}", repository)
        .replace("{{COMMIT}}", commit)
        .replace("{{FILES}}", files)
        .replace("{{COVERAGE}}", gaps)
    )
    path = _path(root, ".local/competition/CHATGPT_PRO_PROMPT.md")
    path.write_text(prompt, encoding="utf-8")
    state["handoff"] = snapshot(root, ".local/competition/CHATGPT_PRO_PROMPT.md")
    _write(_path(root, STATE), state)
    return {
        "phase": state["phase"],
        "prompt_path": str(path),
        "commit": commit,
        "next_message": TRIGGER,
        "prompt": prompt,
    }


@_locked
def start_pdca(
    root: Path, goal_path: str, intake_path: str, trigger: str
) -> dict[str, Any]:
    """Freeze a human-selected official LB target and Issue intake before research."""
    state = _state(root)
    if trigger != TRIGGER or state["phase"] != "waiting_for_issues":
        raise WorkflowError(f"send {TRIGGER!r} after the published Issue handoff")
    _verify(root, state.get("handoff"))
    _verify(root, state["package"])
    goal = _read(_path(root, goal_path))
    package = validate_package(root, state["package"]["path"])
    contract = load_contract(_path(root, package["contract"]["path"]))
    if (
        goal.get("metric") != contract["evaluation"]["metric"]
        or goal.get("direction") != contract["evaluation"]["direction"]
    ):
        raise WorkflowError("goal metric/direction must match the competition contract")
    target = _number(goal.get("target"), "official LB target")
    domain = contract["evaluation"].get("metric_domain") or {}
    if (
        not domain.get("minimum", -math.inf)
        <= target
        <= domain.get("maximum", math.inf)
    ):
        raise WorkflowError("LB target is outside the metric domain")
    if goal.get("scope") not in {"public", "private", "final"}:
        raise WorkflowError("goal scope must be public/private/final")
    if type(goal.get("max_attempts")) is not int or goal["max_attempts"] < 1:
        raise WorkflowError("max_attempts must bound the loop")
    if _number(goal.get("max_wall_seconds"), "max_wall_seconds") <= 0 or _time(
        goal["deadline"]
    ) <= datetime.now(timezone.utc):
        raise WorkflowError("loop needs positive time budget and a future deadline")
    intake = _read(_path(root, intake_path))
    issues = intake.get("issues")
    if intake.get("schema_version") != 1 or not isinstance(issues, list) or not issues:
        raise WorkflowError("nonempty user-approved Issue intake is required")
    seen = set()
    for item in issues:
        issue = item["source_issue"]
        original = _read(_verify(root, issue["snapshot"]))
        if (
            type(issue["number"]) is not int
            or issue["number"] < 1
            or issue["number"] in seen
            or original.get("number") != issue["number"]
        ):
            raise WorkflowError("Issue identity is inconsistent or duplicated")
        seen.add(issue["number"])
        if (
            original.get("url", "").rstrip("/")
            != f"{state['publication']['repository']}/issues/{issue['number']}"
        ):
            raise WorkflowError(
                "Issue must belong to the published competition repository"
            )
        _text(original.get("body"), "Issue body")
        if not item.get("scope_ids"):
            raise WorkflowError("Issue must have an executable scope")
    if "loop" in state:
        previous = json.dumps(state["loop"], sort_keys=True, allow_nan=False).encode()
        archive = _path(
            root,
            ".local/competition/history/"
            + hashlib.sha256(previous).hexdigest()
            + ".json",
        )
        if not archive.exists():
            _write(archive, state["loop"])
    state.update(
        phase="pdca_running",
        loop={
            "goal": snapshot(root, goal_path),
            "intake": snapshot(root, intake_path),
            "started_at": _now(),
            "issue_snapshots": [item["source_issue"] for item in issues],
            "reviews": [],
        },
    )
    _write(_path(root, STATE), state)
    return state


def check_dispatch(
    root: Path,
    source_issue: dict[str, Any],
    attempts: list[dict[str, Any]],
    timeout: int,
    evaluation: dict[str, Any] | None = None,
    contract_ref: dict[str, Any] | None = None,
) -> None:
    """Gate modern workspaces under the registry's atomic claim transaction."""
    if not _path(root, "automation/project.json").exists():
        return
    state = _state(root)
    if state["phase"] != "pdca_running":
        raise WorkflowError(
            "PDCA has not been explicitly started with an official LB target"
        )
    loop = state["loop"]
    goal = _read(_verify(root, loop["goal"]))
    package = _read(_verify(root, state["package"]))
    _verify(root, package["contract"])
    _verify(root, package["evaluation"])
    if evaluation is not None and any(
        evaluation.get(key) != goal[key] for key in ("metric", "direction")
    ):
        raise WorkflowError("runtime evaluation differs from the frozen LB goal")
    if contract_ref is not None and contract_ref != package["contract"]:
        raise WorkflowError("runtime preflight uses another competition contract")
    _verify(root, loop["intake"])
    for issue in loop["issue_snapshots"]:
        _verify(root, issue["snapshot"])
    if source_issue not in loop["issue_snapshots"]:
        raise WorkflowError("experiment Issue is outside the frozen human intake")
    now = datetime.now(timezone.utc)
    remaining = min(
        (_time(goal["deadline"]) - now).total_seconds(),
        goal["max_wall_seconds"] - (now - _time(loop["started_at"])).total_seconds(),
    )
    if remaining <= 0 or timeout > remaining:
        raise WorkflowError(
            "PDCA time budget/deadline exhausted or request cannot finish in time"
        )
    used = sum(
        _time(row["created_at"]) >= _time(loop["started_at"]) for row in attempts
    )
    if used >= goal["max_attempts"]:
        raise WorkflowError(
            "PDCA attempt budget exhausted; keep target unmet and request direction"
        )


@_locked
def review_pdca(root: Path, review_path: str) -> dict[str, Any]:
    """Record a new result's scientific interpretation, not repeated maintenance."""
    from .runtime import _verify_receipt, inspect_registry

    state = _state(root)
    if state["phase"] != "pdca_running":
        raise WorkflowError("PDCA is not running")
    review = _read(_path(root, review_path))
    if review.get("decision") not in {"continue", "request_issues", "freeze"}:
        raise WorkflowError("review needs continue/request_issues/freeze")
    for name in ("finding", "next_question", "why_priority_changed"):
        _text(review.get(name), name)
    attempt = next(
        (
            row
            for row in inspect_registry(root.resolve())
            if row["id"] == review.get("attempt_id")
        ),
        None,
    )
    if (
        not attempt
        or attempt["status"] != "completed"
        or _time(attempt["created_at"]) < _time(state["loop"]["started_at"])
    ):
        raise WorkflowError("review must use a completed experiment from this loop")
    _verify_receipt(attempt["receipt"], root)
    used = [row["attempt_id"] for row in state["loop"]["reviews"]]
    if attempt["id"] in used:
        raise WorkflowError("this result was already interpreted; reuse the finding")
    state["loop"]["reviews"].append(
        {"attempt_id": attempt["id"], "review": snapshot(root, review_path)}
    )
    if review["decision"] != "continue":
        state["phase"] = (
            "waiting_for_issues"
            if review["decision"] == "request_issues"
            else "frozen_unmet"
        )
    _write(_path(root, STATE), state)
    return state


@_locked
def observe_score(root: Path, evidence_path: str) -> dict[str, Any]:
    """Recognize only bound, successful, non-preliminary official LB evidence."""
    state = _state(root)
    if state["phase"] not in {"pdca_running", "frozen_unmet"}:
        raise WorkflowError("there is no active target to reconcile")
    evidence = _read(_path(root, evidence_path))
    goal = _read(_verify(root, state["loop"]["goal"]))
    raw = _read(_verify(root, evidence["snapshot"]))
    native = _read(_verify(root, evidence["native_snapshot"]))
    row: Any = native
    row_path = evidence.get("native_row_path")
    fields = evidence.get("native_fields")
    if (
        not isinstance(row_path, list)
        or not isinstance(fields, dict)
        or set(fields) != {"submission_id", "score", "status"}
    ):
        raise WorkflowError(
            "official evidence needs a native row path and field binding"
        )
    try:
        for key in row_path:
            if type(key) not in {int, str} or (isinstance(key, int) and key < 0):
                raise WorkflowError("invalid native row path")
            row = row[key]
        if not isinstance(row, dict):
            raise WorkflowError("native official row must be an object")
        native_id = row[_text(fields["submission_id"], "native ID field")]
        native_score = float(row[_text(fields["score"], "native score field")])
        native_status = row[_text(fields["status"], "native status field")]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WorkflowError("native official row cannot be parsed") from exc
    if (
        str(native_id) != str(raw.get("submission_id"))
        or native_score != raw.get("score")
        or isinstance(row[fields["score"]], bool)
    ):
        raise WorkflowError("normalized score/ID differs from the native official row")
    success = _text(evidence.get("native_success_status"), "native success status")
    if (
        native_status != success
        or success.lower() in {"pending", "running", "queued", "failed", "error"}
        or any(row.get(key) for key in ("errorDescription", "error", "error_message"))
    ):
        raise WorkflowError("native official row is not a successful scored result")
    if fields["score"] in {"publicScore", "privateScore"} and raw.get(
        "scope"
    ) != fields["score"].removesuffix("Score"):
        raise WorkflowError("score field and Public/Private scope differ")
    _time(raw["observed_at"])
    project = _read(_path(root, "automation/project.json"))
    if (
        raw.get("competition_url") != project["competition_url"]
        or raw.get("kind") != "official"
        or raw.get("status") != "scored"
        or raw.get("preliminary") is not False
    ):
        raise WorkflowError(
            "official scored non-preliminary competition evidence is required"
        )
    for name in ("metric", "direction", "scope"):
        if raw.get(name) != goal[name]:
            raise WorkflowError(f"official {name} differs from goal")
    _text(str(raw.get("submission_id") or ""), "submission_id")
    source = urlsplit(_text(raw.get("source_url"), "official source URL"))
    if (
        source.scheme != "https"
        or source.hostname != urlsplit(project["competition_url"]).hostname
    ):
        raise WorkflowError("official source must belong to the competition platform")
    score = _number(raw.get("score"), "official score")
    package = _read(_verify(root, state["package"]))
    contract = load_contract(_verify(root, package["contract"]))
    domain = contract["evaluation"].get("metric_domain") or {}
    if not domain.get("minimum", -math.inf) <= score <= domain.get("maximum", math.inf):
        raise WorkflowError("official score is outside the metric domain")
    met = (
        score >= goal["target"]
        if goal["direction"] == "max"
        else score <= goal["target"]
    )
    state["loop"]["last_official"] = snapshot(root, evidence_path)
    if met:
        state["phase"] = "target_reached"
    _write(_path(root, STATE), state)
    return {
        "phase": state["phase"],
        "target_achieved": met,
        "score": score,
        "scope": goal["scope"],
        "caveat": "Public target achievement is not a Private medal guarantee",
    }


def workflow_status(root: Path) -> dict[str, Any]:
    """Expose compact lifecycle state without reprinting archives or old reviews."""
    state = _state(root)
    loop = state.get("loop", {})
    decision = None
    if state["phase"] == "pdca_running":
        from .runtime import inspect_registry

        goal = _read(_verify(root, loop["goal"]))
        now = datetime.now(timezone.utc)
        used = sum(
            _time(row["created_at"]) >= _time(loop["started_at"])
            for row in inspect_registry(root.resolve())
        )
        if now >= _time(goal["deadline"]):
            decision = "STOP_DEADLINE_UNMET"
        elif (
            used >= goal["max_attempts"]
            or (now - _time(loop["started_at"])).total_seconds()
            >= goal["max_wall_seconds"]
        ):
            decision = "STOP_BUDGET_UNMET"
    return {
        "phase": state["phase"],
        "decision": decision,
        "publication": state.get("publication"),
        "goal": loop.get("goal"),
        "review_count": len(loop.get("reviews", [])),
        "next_message": TRIGGER if state["phase"] == "waiting_for_issues" else None,
    }


@_locked
def close_pdca(
    root: Path, reason: str, evidence_path: str | None = None
) -> dict[str, Any]:
    """Close an exhausted/ended loop without claiming its target was achieved."""
    state = _state(root)
    if state["phase"] not in {"pdca_running", "frozen_unmet"}:
        raise WorkflowError("only an unmet running/frozen loop can be closed")
    if reason == "competition_ended":
        evidence = _read(_path(root, _text(evidence_path, "closure evidence")))
        raw = _read(_verify(root, evidence["snapshot"]))
        _verify(root, evidence["native_snapshot"])
        project = _read(_path(root, "automation/project.json"))
        if (
            raw.get("competition_url") != project["competition_url"]
            or raw.get("kind") != "official"
            or raw.get("status") != "ended"
            or _time(raw["observed_at"]) > datetime.now(timezone.utc)
        ):
            raise WorkflowError("official observed competition closure is required")
        state["loop"]["closure"] = snapshot(root, evidence_path)
    else:
        expected = {
            "budget_exhausted": "STOP_BUDGET_UNMET",
            "deadline": "STOP_DEADLINE_UNMET",
        }
        if (
            reason not in expected
            or workflow_status(root)["decision"] != expected[reason]
        ):
            raise WorkflowError("budget/deadline has not actually been reached")
    state.update(phase=reason + "_unmet", closed_at=_now())
    _write(_path(root, STATE), state)
    return workflow_status(root)
