"""Evidence-bound Issue coverage, work selection and permission to wait.

This planner never polls, sleeps, submits or launches a process. Execution
continues through the existing runtime/validation entrypoints. A materialized
automation/continuation.json activates the strict shared-queue runtime policy.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .controller import evaluate_controller_state
from .fingerprint import canonical_json_digest
from .runtime import _path, _verify_receipt, inspect_registry, read_snapshot
from .scheduler import bind_daily_quota, plan_work_queue
from .signature import computation_signature
from .validation import plan_validation


class ContinuationError(ValueError):
    """Raised when a continuation plan omits work or has unbound evidence."""


def _object(
    value: object, fields: set[str], required: set[str], name: str
) -> dict[str, Any]:
    """Require an object with explicit, typo-resistant schema keys."""
    if not isinstance(value, dict) or set(value) - fields or required - set(value):
        raise ContinuationError(f"invalid {name} fields")
    return value


def _text(value: object, name: str) -> str:
    """Require a nonempty identifier or explanation."""
    if not isinstance(value, str) or not value.strip():
        raise ContinuationError(f"{name} must be nonempty text")
    return value


def _list(value: object, name: str) -> list[Any]:
    """Require a JSON array."""
    if not isinstance(value, list):
        raise ContinuationError(f"{name} must be an array")
    return value


def _identifiers(value: object, name: str) -> list[str]:
    """Validate distinct nonempty identifiers."""
    items = [_text(item, name) for item in _list(value, name)]
    if len(items) != len(set(items)):
        raise ContinuationError(f"duplicate {name}")
    return items


def _read(root: Path, snapshot: object) -> dict[str, Any]:
    """Read a finite hash-bound JSON object below the workspace root."""
    value = read_snapshot(root, snapshot)
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContinuationError("evidence must be finite JSON") from exc
    return value


def _time(value: object) -> datetime:
    """Parse an observed check time and require an explicit timezone."""
    try:
        result = datetime.fromisoformat(
            _text(value, "next_check_at").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ContinuationError("next_check_at must be ISO-8601") from exc
    if result.tzinfo is None:
        raise ContinuationError("next_check_at needs a timezone")
    return result


def _intake(plan: dict[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    """Enumerate every scope from the frozen, user-approved intake inventory."""
    intake = _read(root, plan["intake"])
    _object(
        intake, {"schema_version", "issues"}, {"schema_version", "issues"}, "intake"
    )
    if type(intake["schema_version"]) is not int or intake["schema_version"] != 1:
        raise ContinuationError("intake schema_version must be 1")
    scopes: dict[str, dict[str, Any]] = {}
    numbers: set[int] = set()
    for issue in _list(intake["issues"], "intake.issues"):
        _object(
            issue,
            {"source_issue", "scope_ids"},
            {"source_issue", "scope_ids"},
            "intake Issue",
        )
        source = _object(
            issue["source_issue"],
            {"number", "snapshot"},
            {"number", "snapshot"},
            "source_issue",
        )
        number = source["number"]
        if type(number) is not int or number < 1 or number in numbers:
            raise ContinuationError(
                "intake Issue numbers must be distinct positive integers"
            )
        numbers.add(number)
        original = _read(root, source["snapshot"])
        if original.get("number") != number:
            raise ContinuationError("intake Issue snapshot number mismatch")
        ids = _identifiers(issue["scope_ids"], "scope_ids")
        if not ids:
            raise ContinuationError("each authorized Issue needs at least one scope")
        for scope_id in ids:
            if scope_id in scopes:
                raise ContinuationError("scope identifiers must be globally unique")
            scopes[scope_id] = source
    return scopes


def _blockers(
    item: dict[str, Any], job: dict[str, Any], root: Path
) -> list[dict[str, Any]]:
    """Apply observed blockers only to their explicitly named work item."""
    blocked = []
    for snapshot in _list(item.get("blockers", []), "blockers"):
        observation = _read(root, snapshot)
        _object(
            observation,
            {
                "kind",
                "state",
                "affected_job_ids",
                "reason",
                "resume_condition",
                "next_check_at",
            },
            {"kind", "state", "affected_job_ids", "reason", "resume_condition"},
            "blocker",
        )
        if _text(observation["kind"], "blocker.kind") not in {
            "score",
            "quota",
            "approval",
            "input",
            "family",
        }:
            raise ContinuationError("unknown blocker kind")
        if _text(observation["state"], "blocker.state") not in {"waiting", "cleared"}:
            raise ContinuationError("unknown blocker state")
        affected = _identifiers(observation["affected_job_ids"], "affected_job_ids")
        if job["id"] not in affected:
            raise ContinuationError("blocker does not name this job")
        if observation["kind"] == "quota" and job["kind"] != "submission":
            raise ContinuationError("submission quota cannot block local work")
        _text(observation["reason"], "blocker.reason")
        _text(observation["resume_condition"], "blocker.resume_condition")
        if observation.get("next_check_at") is not None:
            _time(observation["next_check_at"])
        if observation["state"] == "waiting":
            blocked.append({**observation, "evidence": snapshot})
    return blocked


def _completed(row: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    """Verify scientific outputs against the actual completed runtime receipt."""
    receipt = row["receipt"]
    if not isinstance(receipt, dict) or receipt.get("status") != "completed":
        raise ContinuationError("completed computation lacks a completed receipt")
    if (
        receipt.get("attempt_id") != row["id"]
        or receipt.get("computation_signature") != row["computation"]
    ):
        raise ContinuationError("runtime receipt identity mismatch")
    _verify_receipt(receipt, root)
    request = row["request"]["request"]
    outputs = {str(Path(request["output_dir"]) / name) for name in request["outputs"]}
    artifacts = [value for value in receipt["artifacts"] if value["path"] in outputs]
    if not artifacts:
        raise ContinuationError("completed computation lacks scientific artifacts")
    return artifacts


def _resolution(
    snapshot: object,
    scope_id: str,
    source: dict[str, Any],
    completed: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    """Bind interpretation to this scope and actual completed scientific outputs."""
    result = _read(root, snapshot)
    _object(
        result,
        {"scope_id", "source_issue", "outcome", "reason", "artifacts"},
        {"scope_id", "source_issue", "outcome", "reason", "artifacts"},
        "resolution",
    )
    if result["scope_id"] != scope_id or result["source_issue"] != source:
        raise ContinuationError("resolution must identify the original scope and Issue")
    if result["outcome"] not in {"supported", "refuted", "inconclusive"}:
        raise ContinuationError(
            "resolution is a local conclusion, not adoption or an official score"
        )
    _text(result["reason"], "resolution.reason")
    artifacts = _list(result["artifacts"], "resolution.artifacts")
    if not artifacts or any(value not in completed for value in artifacts):
        raise ContinuationError(
            "resolution needs verified scientific outputs from this scope"
        )
    return result


def plan_continuation(
    plan: dict[str, Any], root: Path, now: datetime | None = None
) -> dict[str, Any]:
    """Reconcile complete Issue coverage, runtime claims, checks and scoped waits."""
    root = root.expanduser().resolve()
    _object(
        plan,
        {"schema_version", "intake", "queue", "scopes", "work_items"},
        {"schema_version", "intake", "queue", "scopes", "work_items"},
        "continuation",
    )
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ContinuationError("continuation schema_version must be 1")
    intake = _intake(plan, root)
    scopes: dict[str, dict[str, Any]] = {}
    for scope in _list(plan["scopes"], "scopes"):
        _object(scope, {"id", "job_ids", "resolution"}, {"id", "job_ids"}, "scope")
        scope_id = _text(scope["id"], "scope.id")
        if scope_id in scopes:
            raise ContinuationError("duplicate scope")
        scopes[scope_id] = {
            **scope,
            "job_ids": _identifiers(scope["job_ids"], "scope.job_ids"),
        }
    if set(scopes) != set(intake):
        raise ContinuationError(
            "Issue coverage mismatch: every intake scope must appear exactly once"
        )
    queue = deepcopy(_read(root, plan["queue"]))
    if queue.get("daily_plan") is not None:
        queue, _ = bind_daily_quota(
            queue,
            root / "automation/quota.json",
            root / "automation/ledger.jsonl",
            root,
            now,
        )
    plan_work_queue(queue, now)
    jobs = {job["id"]: job for job in queue["jobs"]}
    items: dict[str, dict[str, Any]] = {}
    for item in _list(plan["work_items"], "work_items"):
        _object(
            item,
            {"job_id", "scope_id", "request", "validation", "blockers"},
            {"job_id", "scope_id"},
            "work_item",
        )
        job_id = _text(item["job_id"], "work_item.job_id")
        if job_id in items or job_id not in jobs:
            raise ContinuationError("duplicate or unknown work_item job")
        scope_id = item["scope_id"]
        if scope_id is not None and (
            not isinstance(scope_id, str) or scope_id not in scopes
        ):
            raise ContinuationError("work_item references an unknown scope")
        kind = jobs[job_id].get("kind", "unspecified")
        if kind == "unspecified":
            raise ContinuationError("strict continuation jobs need an explicit kind")
        if kind in {"hypothesis", "prerequisite", "repair"} and scope_id is None:
            raise ContinuationError(
                "scientific work and its prerequisites need an authorized scope"
            )
        if scope_id is not None and job_id not in scopes[scope_id]["job_ids"]:
            raise ContinuationError("scope does not include its work item")
        items[job_id] = item
    if set(items) != set(jobs):
        raise ContinuationError("every queue job needs a work item")
    for scope in scopes.values():
        if any(
            key not in items or items[key]["scope_id"] != scope["id"]
            for key in scope["job_ids"]
        ):
            raise ContinuationError(
                "scope references a missing or differently scoped job"
            )

    rows = inspect_registry(root)
    by_signature: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_signature.setdefault(row["computation"], []).append(row)
    work: dict[str, dict[str, Any]] = {}
    signatures: dict[str, str] = {}
    unresolved: list[dict[str, Any]] = []
    active_ids: set[str] = set()
    for job_id, item in items.items():
        job = jobs[job_id]
        blocked = _blockers(item, job, root)
        if any(set(value["affected_job_ids"]) - set(jobs) for value in blocked):
            raise ContinuationError("blocker names an unknown queue job")
        info: dict[str, Any] = {
            "job_id": job_id,
            "kind": job["kind"],
            "scope_id": item["scope_id"],
            "blockers": blocked,
            "artifacts": [],
        }
        if item.get("request") is not None:
            if job["kind"] not in {"hypothesis", "prerequisite", "repair"}:
                raise ContinuationError(
                    "runtime requests must be scoped scientific work, not monitor/submission commands"
                )
            request = _read(root, item["request"])
            if (
                request.get("queue") != plan["queue"]
                or request.get("queue_job_id") != job_id
            ):
                raise ContinuationError(
                    "request must bind the shared queue and its exact job"
                )
            controller = _read(root, request.get("controller"))
            if (
                controller.get("action", {}).get("source_issue")
                != intake[item["scope_id"]]
            ):
                raise ContinuationError(
                    "request Issue differs from the authorized scope"
                )
            if controller.get("action", {}).get("computation_spec") != request.get(
                "computation_spec"
            ):
                raise ContinuationError(
                    "request computation differs from its controller"
                )
            signature = computation_signature(request.get("computation_spec"))
            if signature in signatures:
                raise ContinuationError(
                    "duplicate computation in queue; share one job dependency instead"
                )
            signatures[signature] = job_id
            info.update(
                {"request": item["request"], "computation_signature": signature}
            )
            existing = by_signature.get(signature, [])
            running = next(
                (row for row in existing if row["status"] == "running"), None
            )
            complete = next(
                (row for row in existing if row["status"] == "completed"), None
            )
            if running is not None:
                reservation = running["request"].get("queue_reservation")
                if reservation is None or reservation["job_id"] != job_id:
                    raise ContinuationError(
                        "running computation needs its original shared resource reservation"
                    )
                if queue["resources"] != running["request"].get("queue_capacity"):
                    raise ContinuationError(
                        "queue capacity changed while a computation is running"
                    )
                job.update({"status": "running", "resources": reservation["resources"]})
                active_ids.add(running["id"])
                info.update(
                    {
                        "decision": "JOIN_RUNNING",
                        "attempt_id": running["id"],
                        "blockers": [],
                        "original_source_issue": running["request"]["source_issue"],
                    }
                )
            elif complete is not None:
                artifacts = _completed(complete, root)
                original = complete["request"]["request"]
                if not set(request["outputs"]).issubset(original["outputs"]):
                    raise ContinuationError(
                        "reused computation does not contain requested outputs"
                    )
                job["status"] = "completed"
                info.update(
                    {
                        "decision": "REUSE_COMPLETED",
                        "attempt_id": complete["id"],
                        "artifacts": artifacts,
                        "blockers": [],
                        "original_source_issue": complete["request"]["source_issue"],
                    }
                )
            elif existing and (
                request.get("retry_of") != existing[0]["id"]
                or existing[0]["status"] not in {"infra_failed", "preempted"}
            ):
                info["decision"] = "REVIEW_FAILED_ATTEMPT"
                unresolved.append(
                    {
                        "job_id": job_id,
                        "reason": "failed computation needs an explicit eligible retry or revised plan",
                    }
                )
                job["status"] = "cancelled"
            else:
                if job["status"] in {"completed", "running"}:
                    raise ContinuationError(
                        "queue status lacks an authoritative runtime attempt"
                    )
                if job["status"] in {"failed", "cancelled"} and not existing:
                    info["decision"] = "NEEDS_EXECUTABLE_PLAN"
                    unresolved.append(
                        {
                            "job_id": job_id,
                            "reason": "stopped work needs evidence and an explicit revised plan",
                        }
                    )
                else:
                    job["status"] = "waiting" if blocked else "ready"
                    info["decision"] = "BLOCKED_SCOPED" if blocked else "RUN_REQUIRED"
                    if not blocked:
                        decision = evaluate_controller_state(
                            controller, root, now, job_id=job_id
                        )
                        if not decision["allowed"]:
                            job["status"] = "cancelled"
                            info["decision"] = decision["decision"]
                            unresolved.append(
                                {"job_id": job_id, "reason": decision["decision"]}
                            )
        elif item.get("validation") is not None:
            if job["kind"] != "maintenance" or item["scope_id"] is not None:
                raise ContinuationError(
                    "maintenance checks cannot resolve scientific scopes"
                )
            check = _object(
                item["validation"],
                {"plan", "check_id"},
                {"plan", "check_id"},
                "validation",
            )
            validation = plan_validation(_read(root, check["plan"]), root)
            detail = next(
                (
                    value
                    for value in validation["checks"]
                    if value["check_id"] == check["check_id"]
                ),
                None,
            )
            if detail is None:
                raise ContinuationError("validation check does not exist")
            cached = detail["status"] == "CACHED_PASS"
            job["status"] = (
                "completed" if cached else ("waiting" if blocked else "ready")
            )
            info["decision"] = detail["status"]
            if cached:
                info["blockers"] = []
            if (
                not cached
                and check["check_id"] not in validation["runnable_checks"]
                and not blocked
            ):
                job["status"] = "cancelled"
                unresolved.append({"job_id": job_id, "reason": detail["status"]})
        elif job["kind"] == "monitor":
            if not blocked or not job.get("next_check_at"):
                raise ContinuationError(
                    "monitor needs a scoped waiting observation and next_check_at"
                )
            if len(blocked) != 1 or _time(blocked[0].get("next_check_at")) != _time(
                job["next_check_at"]
            ):
                raise ContinuationError(
                    "monitor check time must match its observed waiting condition"
                )
            job["status"] = "waiting"
            info["decision"] = "MONITOR_SCHEDULED"
        elif blocked:
            job["status"] = "waiting"
            info["decision"] = "BLOCKED_SCOPED"
        else:
            job["status"] = "cancelled"
            info["decision"] = "NEEDS_EXECUTABLE_PLAN"
            unresolved.append(
                {
                    "job_id": job_id,
                    "reason": "request, validation check or external handoff required",
                }
            )
        if blocked and job["status"] == "waiting" and job["kind"] != "monitor":
            job.pop("next_check_at", None)
        work[job_id] = info
    if {row["id"] for row in rows if row["status"] == "running"} != active_ids:
        raise ContinuationError(
            "queue omits a running registry claim or its resource reservation"
        )
    unresolved.extend(
        {
            "job_id": key,
            "reason": "running work needs next_check_at for a bounded scheduler wake",
        }
        for key, job in jobs.items()
        if job["status"] == "running" and not job.get("next_check_at")
    )

    resolved = []
    interpretations = []
    for scope_id, scope in scopes.items():
        completed = [
            artifact
            for key in scope["job_ids"]
            if jobs[key]["kind"] == "hypothesis"
            for artifact in work[key]["artifacts"]
        ]
        if scope.get("resolution") is not None:
            if any(jobs[key]["status"] != "completed" for key in scope["job_ids"]):
                raise ContinuationError("resolved scope still has unfinished work")
            _resolution(
                scope["resolution"], scope_id, intake[scope_id], completed, root
            )
            resolved.append(scope_id)
        elif not scope["job_ids"]:
            unresolved.append(
                {
                    "scope_id": scope_id,
                    "reason": "authorized scope has no executable work",
                }
            )
        elif all(jobs[key]["status"] == "completed" for key in scope["job_ids"]):
            interpretations.append(scope_id)
    queue_plan = plan_work_queue(queue, now)
    selected = queue_plan["selected_jobs"]
    monitors = [
        key for key in queue_plan["monitor_jobs"] if jobs[key]["kind"] == "monitor"
    ]
    unexplained = [
        value
        for value in queue_plan["jobs"]
        if value["reason"] in {"deadline_missed", "dependency_failed"}
    ]
    if unexplained:
        unresolved.extend(
            {"job_id": value["id"], "reason": value["reason"]} for value in unexplained
        )
    can_wait = not (selected or monitors or unresolved or interpretations)
    decision = (
        "CONTINUE_WORK"
        if selected
        else "MONITOR_ONCE"
        if monitors
        else "NEEDS_EXECUTABLE_PLAN"
        if unresolved
        else "INTERPRET_RESULTS"
        if interpretations
        else "WAITING_FOR_USER_ISSUE"
        if not intake
        else "WAIT_ALLOWED"
    )
    times = [queue_plan["next_wake_at"]] if queue_plan["next_wake_at"] else []
    for value in work.values():
        times.extend(
            block["next_check_at"]
            for block in value["blockers"]
            if block.get("next_check_at")
        )
    current = now or datetime.now(timezone.utc)
    future = [_time(value) for value in times]
    overdue = any(value <= current for value in future)
    if can_wait and overdue:
        can_wait = False
        decision = "RECHECK_SCOPED_BLOCKER"
    next_check = min(future).astimezone(timezone.utc).isoformat() if future else None
    return {
        "schema_version": 1,
        "decision": decision,
        "can_wait": can_wait,
        "goal_achieved": False,
        "next_check_at": next_check,
        "selected_jobs": selected,
        "monitor_jobs": monitors,
        "monitor_timeout_seconds": 45,
        "unresolved_work": unresolved,
        "interpret_scope_ids": interpretations,
        "resolved_scope_ids": resolved,
        "work_items": list(work.values()),
        "queue_plan": queue_plan,
        "progress": {
            "resolved_scientific_scopes": len(resolved),
            "total_authorized_scopes": len(scopes),
            "maintenance_completed": sum(
                value["kind"] == "maintenance" and jobs[key]["status"] == "completed"
                for key, value in work.items()
            ),
        },
        "state_fingerprint": canonical_json_digest(
            {"plan": plan, "work": work, "queue": queue, "resolutions": resolved}
        ),
    }


def check_dispatch(request: dict[str, Any], root: Path) -> dict[str, Any]:
    """Require strict-policy execution to be selected from the canonical plan."""
    plan = json.loads(
        _path(root, "automation/continuation.json").read_text(encoding="utf-8")
    )
    result = plan_continuation(plan, root)
    job_id = request.get("queue_job_id")
    if job_id not in result["selected_jobs"] or request.get("queue") != plan["queue"]:
        raise ContinuationError(
            "continuation policy did not select this shared-queue job"
        )
    item = next(value for value in result["work_items"] if value["job_id"] == job_id)
    if "request" not in item or _read(root, item["request"]) != request:
        raise ContinuationError("dispatch request differs from the frozen work item")
    return result
