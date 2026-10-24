"""Deterministic planning for dependency-aware local work queues.

The planner is deliberately a pure function.  It does not sleep, poll, start
processes, or mutate the supplied state.  A caller can dispatch
``selected_jobs`` and let its native scheduler wake it at ``next_wake_at``.

The accepted state has ``schema_version``, a named ``resources`` capacity
map, and a ``jobs`` list.  A job has ``id``, ``status``, ``depends_on``,
``resources``, ``estimated_seconds``, and ``priority``.  ``deadline`` and
``next_check_at`` are optional timezone-aware ISO-8601 timestamps.  Global
``preparation_seconds`` and ``downstream_seconds`` are included in critical
path and latest-start calculations.  ``daily_plan`` is an optional generic
candidate target containing ``target_ready_candidates``,
``ready_candidates``, ``distinct_candidates_validated``, ``reset_at``,
``reserve_seconds``, and optional ``candidate_job_ids``.  The ready counter
is used only when the caller explicitly validates distinctness.  Set the root
``allow_overdue`` policy explicitly when a missed-deadline job may still run.
"""

from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .ledger import read_ledger
from .quota import build_daily_plan, validate_quota_profile


class SchedulerError(ValueError):
    """Raised when a work-queue state violates the scheduler contract."""


JOB_STATUSES = frozenset(
    {"ready", "running", "waiting", "completed", "failed", "cancelled"}
)
JOB_KIND_PRIORITY = {
    "repair": 0,
    "hypothesis": 1,
    "prerequisite": 1,
    "submission": 2,
    "unspecified": 3,
    "monitor": 3,
    "maintenance": 4,
}
JOB_KINDS = frozenset(JOB_KIND_PRIORITY)
ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "resources",
        "jobs",
        "preparation_seconds",
        "downstream_seconds",
        "deadline",
        "daily_plan",
        "allow_overdue",
    }
)
JOB_FIELDS = frozenset(
    {
        "id",
        "status",
        "depends_on",
        "resources",
        "estimated_seconds",
        "priority",
        "deadline",
        "next_check_at",
        "candidate",
        "kind",
    }
)
DAILY_FIELDS = frozenset(
    {
        "target_ready_candidates",
        "ready_candidates",
        "reset_at",
        "reserve_seconds",
        "candidate_job_ids",
        "distinct_candidates_validated",
        "next_reset_at",
    }
)


def _mapping(value: object, name: str) -> dict[str, Any]:
    """Require a JSON object."""
    if not isinstance(value, dict):
        raise SchedulerError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    """Require a non-empty string value."""
    if not isinstance(value, str) or not value.strip():
        raise SchedulerError(f"{name} must be a non-empty string")
    return value.strip()


def _number(value: object, name: str, minimum: float = 0.0) -> float:
    """Require a finite numeric value above the supplied minimum."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchedulerError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise SchedulerError(f"{name} must be finite and >= {minimum}")
    return number


def _integer(value: object, name: str, minimum: int = 0) -> int:
    """Require an integer value above the supplied minimum."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SchedulerError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: object, name: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp and normalize it to UTC."""
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchedulerError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SchedulerError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime | None) -> str | None:
    """Serialize a timestamp in canonical UTC form."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], name: str) -> None:
    """Reject fields outside a declared JSON schema."""
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise SchedulerError(f"unknown {name} field(s): {unknown}")


def _resources(value: object, name: str) -> dict[str, float]:
    """Validate a named resource quantity map."""
    mapping = _mapping(value, name)
    result: dict[str, float] = {}
    for key, raw in mapping.items():
        resource = _text(key, f"{name} resource name")
        if resource in result:
            raise SchedulerError(f"duplicate {name} resource: {resource}")
        result[resource] = _number(raw, f"{name}.{resource}")
    return result


def _depends_on(value: object, name: str) -> list[str]:
    """Validate a dependency identifier list and reject duplicates."""
    if not isinstance(value, list):
        raise SchedulerError(f"{name} must be an array")
    result: list[str] = []
    for index, item in enumerate(value):
        dependency = _text(item, f"{name}[{index}]")
        if dependency in result:
            raise SchedulerError(f"duplicate dependency in {name}: {dependency}")
        result.append(dependency)
    return result


def _validate_now(now: datetime | None) -> datetime:
    """Validate the planning clock and normalize it to UTC."""
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime):
        raise SchedulerError("now must be a datetime")
    if current.tzinfo is None:
        raise SchedulerError("now must include a timezone")
    return current.astimezone(timezone.utc)


def _validate_state(
    state: dict[str, Any],
) -> tuple[
    dict[str, float],
    list[dict[str, Any]],
    dict[str, Any] | None,
    float,
    float,
    datetime | None,
    bool,
]:
    """Validate and normalize the complete queue state."""
    if not isinstance(state, dict):
        raise SchedulerError("state must be an object")
    _reject_unknown(state, ROOT_FIELDS, "state")
    if state.get("schema_version") != 1:
        raise SchedulerError("schema_version must be 1")
    capacities = _resources(state.get("resources", {}), "resources")
    preparation = _number(state.get("preparation_seconds", 0), "preparation_seconds")
    downstream = _number(state.get("downstream_seconds", 0), "downstream_seconds")
    deadline = (
        None
        if state.get("deadline") is None
        else _timestamp(state["deadline"], "deadline")
    )
    allow_overdue = state.get("allow_overdue", False)
    if not isinstance(allow_overdue, bool):
        raise SchedulerError("allow_overdue must be boolean")
    raw_jobs = state.get("jobs")
    if not isinstance(raw_jobs, list):
        raise SchedulerError("jobs must be an array")
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_job in enumerate(raw_jobs):
        job = _mapping(raw_job, f"jobs[{index}]")
        _reject_unknown(job, JOB_FIELDS, f"jobs[{index}]")
        job_id = _text(job.get("id"), f"jobs[{index}].id")
        if job_id in seen:
            raise SchedulerError(f"duplicate job id: {job_id}")
        seen.add(job_id)
        status = _text(job.get("status"), f"jobs[{index}].status")
        if status not in JOB_STATUSES:
            raise SchedulerError(f"jobs[{index}].status is invalid: {status}")
        depends = _depends_on(job.get("depends_on", []), f"jobs[{index}].depends_on")
        if job_id in depends:
            raise SchedulerError(f"job cannot depend on itself: {job_id}")
        resources = _resources(job.get("resources", {}), f"jobs[{index}].resources")
        estimated = _number(
            job.get("estimated_seconds"), f"jobs[{index}].estimated_seconds"
        )
        priority = job.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise SchedulerError(f"jobs[{index}].priority must be an integer")
        job_deadline = (
            None
            if job.get("deadline") is None
            else _timestamp(job["deadline"], f"jobs[{index}].deadline")
        )
        next_check = (
            None
            if job.get("next_check_at") is None
            else _timestamp(job["next_check_at"], f"jobs[{index}].next_check_at")
        )
        candidate = job.get("candidate", False)
        if not isinstance(candidate, bool):
            raise SchedulerError(f"jobs[{index}].candidate must be boolean")
        kind = job.get("kind", "unspecified")
        if not isinstance(kind, str) or kind not in JOB_KINDS:
            raise SchedulerError(f"jobs[{index}].kind is invalid")
        jobs.append(
            {
                "id": job_id,
                "status": status,
                "depends_on": depends,
                "resources": resources,
                "estimated_seconds": estimated,
                "priority": priority,
                "deadline": job_deadline,
                "next_check_at": next_check,
                "candidate": candidate,
                "kind": kind,
            }
        )
    by_id = {job["id"]: job for job in jobs}
    for job in jobs:
        for dependency in job["depends_on"]:
            if dependency not in by_id:
                raise SchedulerError(
                    f"missing dependency {dependency!r} for job {job['id']!r}"
                )

    children: dict[str, list[str]] = defaultdict(list)
    for job in jobs:
        for dependency in job["depends_on"]:
            children[dependency].append(job["id"])
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(job_id: str) -> None:
        """Traverse the dependency graph and reject back edges."""
        if job_id in visiting:
            raise SchedulerError(f"dependency cycle includes job: {job_id}")
        if job_id in visited:
            return
        visiting.add(job_id)
        for child in children[job_id]:
            visit(child)
        visiting.remove(job_id)
        visited.add(job_id)

    for job in jobs:
        visit(job["id"])

    raw_daily = state.get("daily_plan")
    daily: dict[str, Any] | None = None
    if raw_daily is not None:
        daily_map = _mapping(raw_daily, "daily_plan")
        _reject_unknown(daily_map, DAILY_FIELDS, "daily_plan")
        daily = {
            "target_ready_candidates": _integer(
                daily_map.get("target_ready_candidates"),
                "daily_plan.target_ready_candidates",
            ),
            "ready_candidates": _integer(
                daily_map.get("ready_candidates"), "daily_plan.ready_candidates"
            ),
            "reset_at": _timestamp(daily_map.get("reset_at"), "daily_plan.reset_at"),
            "reserve_seconds": _number(
                daily_map.get("reserve_seconds"), "daily_plan.reserve_seconds"
            ),
            "candidate_job_ids": _depends_on(
                daily_map.get("candidate_job_ids", []), "daily_plan.candidate_job_ids"
            ),
            "distinct_candidates_validated": daily_map.get(
                "distinct_candidates_validated", False
            ),
            "next_reset_at": (
                None
                if daily_map.get("next_reset_at") is None
                else _timestamp(daily_map["next_reset_at"], "daily_plan.next_reset_at")
            ),
        }
        if not isinstance(daily["distinct_candidates_validated"], bool):
            raise SchedulerError(
                "daily_plan.distinct_candidates_validated must be boolean"
            )
        unknown_candidates = sorted(set(daily["candidate_job_ids"]).difference(by_id))
        if unknown_candidates:
            raise SchedulerError(
                f"daily_plan references missing job(s): {unknown_candidates}"
            )
    return capacities, jobs, daily, preparation, downstream, deadline, allow_overdue


def _job_duration(job: dict[str, Any]) -> float:
    """Return remaining work; terminal jobs contribute no remaining duration."""
    if job["status"] in {"completed", "failed", "cancelled"}:
        return 0.0
    return job["estimated_seconds"]


def _critical_paths(
    jobs: list[dict[str, Any]],
    preparation: float,
    downstream: float,
    root_deadline: datetime | None,
    extra_deadlines: dict[str, datetime] | None = None,
) -> dict[str, dict[str, Any]]:
    """Calculate remaining graph paths and latest starts for every job."""
    extra_deadlines = extra_deadlines or {}
    by_id = {job["id"]: job for job in jobs}
    children: dict[str, list[str]] = defaultdict(list)
    for job in jobs:
        for dependency in job["depends_on"]:
            children[dependency].append(job["id"])
    path_cache: dict[str, float] = {}
    edge_cache: dict[tuple[str, str], float] = {}

    def path_from(job_id: str) -> float:
        """Return remaining work on the longest dependent path."""
        if job_id in path_cache:
            return path_cache[job_id]
        child_paths = [path_from(child) for child in children[job_id]]
        result = _job_duration(by_id[job_id]) + (
            max(child_paths) if child_paths else 0.0
        )
        path_cache[job_id] = result
        return result

    def path_to(source: str, target: str) -> float:
        """Return remaining work between a node and one descendant."""
        if source == target:
            return _job_duration(by_id[source])
        key = (source, target)
        if key in edge_cache:
            return edge_cache[key]
        values = [
            path_to(child, target)
            for child in children[source]
            if _reachable(child, target)
        ]
        if not values:
            return -math.inf
        value = _job_duration(by_id[source]) + max(values)
        edge_cache[key] = value
        return value

    reach_cache: dict[tuple[str, str], bool] = {}

    def _reachable(source: str, target: str) -> bool:
        """Memoize whether the target descends from the source."""
        key = (source, target)
        if key in reach_cache:
            return reach_cache[key]
        if source == target:
            return True
        answer = any(_reachable(child, target) for child in children[source])
        reach_cache[key] = answer
        return answer

    result: dict[str, dict[str, Any]] = {}
    for job in jobs:
        job_id = job["id"]
        path = path_from(job_id)
        constraints: list[tuple[datetime, float]] = []
        if root_deadline is not None:
            constraints.append((root_deadline, path))
        for descendant in by_id.values():
            descendant_deadline = descendant["deadline"]
            if descendant_deadline is None or not _reachable(job_id, descendant["id"]):
                continue
            constraints.append((descendant_deadline, path_to(job_id, descendant["id"])))
        if job_id in extra_deadlines:
            constraints.append((extra_deadlines[job_id], path))
        for candidate_id, candidate_deadline in extra_deadlines.items():
            if candidate_id != job_id and _reachable(job_id, candidate_id):
                constraints.append((candidate_deadline, path_to(job_id, candidate_id)))
        latest_start = None
        if constraints and path > 0:
            latest_start = min(
                deadline - timedelta(seconds=preparation + elapsed + downstream)
                for deadline, elapsed in constraints
            )
        result[job_id] = {
            "critical_path_seconds": preparation + path + downstream
            if path > 0
            else 0.0,
            "latest_start": latest_start,
        }
    return result


def plan_work_queue(
    state: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """Return a deterministic dispatch and wake plan without mutating ``state``.

    Completed dependencies unlock a ``ready`` job.  ``running`` jobs reserve
    their declared resources; ``waiting`` jobs reserve none, so a future
    score poll cannot prevent independent CPU/GPU work.  Due waits are
    reported in ``due_waiting_jobs``. One due check is also returned in
    ``monitor_jobs`` even when local work is selected, preventing starvation.
    The caller performs that bounded observation, advances its next check,
    and continues local work. If no job is selected,
    ``handoff`` explicitly tells the caller to wake at ``next_wake_at``.
    """
    current = _validate_now(now)
    capacities, jobs, daily, preparation, downstream, root_deadline, allow_overdue = (
        _validate_state(state)
    )
    by_id = {job["id"]: job for job in jobs}

    daily_summary: dict[str, Any] | None = None
    candidate_ids: set[str] = set()
    if daily is not None:
        candidate_ids.update(daily["candidate_job_ids"])
        candidate_ids.update(job["id"] for job in jobs if job["candidate"])
        shortfall = None
        if daily["distinct_candidates_validated"]:
            shortfall = max(
                0, daily["target_ready_candidates"] - daily["ready_candidates"]
            )
        reserve_deadline = daily["reset_at"] - timedelta(
            seconds=daily["reserve_seconds"]
        )
        daily_summary = {
            "target_ready_candidates": daily["target_ready_candidates"],
            "ready_candidates": daily["ready_candidates"],
            "distinct_candidates_validated": daily["distinct_candidates_validated"],
            "shortfall": shortfall,
            "reset_at": _format_timestamp(daily["reset_at"]),
            "reserve_seconds": daily["reserve_seconds"],
            "reserve_deadline": _format_timestamp(reserve_deadline),
            "next_reset_at": _format_timestamp(
                daily.get("next_reset_at") or daily["reset_at"]
            ),
            "candidate_job_ids": sorted(candidate_ids),
        }
    priority_candidate_ids = set(candidate_ids)
    pending_ancestors = list(candidate_ids)
    while pending_ancestors:
        candidate_id = pending_ancestors.pop()
        for dependency in by_id[candidate_id]["depends_on"]:
            if dependency not in priority_candidate_ids:
                priority_candidate_ids.add(dependency)
                pending_ancestors.append(dependency)
    extra_deadlines = {}
    if daily_summary is not None and candidate_ids:
        reserve_deadline = daily["reset_at"] - timedelta(
            seconds=daily["reserve_seconds"]
        )
        extra_deadlines = {
            candidate_id: reserve_deadline for candidate_id in candidate_ids
        }
    critical = _critical_paths(
        jobs, preparation, downstream, root_deadline, extra_deadlines
    )

    usage = {name: 0.0 for name in capacities}
    for job in jobs:
        for resource, amount in job["resources"].items():
            if resource not in capacities:
                raise SchedulerError(
                    f"job {job['id']} requests undeclared resource: {resource}"
                )
            if amount > capacities[resource]:
                raise SchedulerError(
                    f"job {job['id']} exceeds capacity for resource: {resource}"
                )
    for job in jobs:
        if job["status"] != "running":
            continue
        for resource, amount in job["resources"].items():
            usage[resource] += amount
    overcommitted = sorted(
        resource for resource, amount in usage.items() if amount > capacities[resource]
    )

    due_waiting: list[str] = []
    eligible: list[dict[str, Any]] = []
    dependency_reasons: dict[str, str] = {}
    for job in jobs:
        status = job["status"]
        if status == "waiting":
            if (
                job["next_check_at"] is not None
                and job["next_check_at"] <= current
                and all(by_id[key]["status"] == "completed" for key in job["depends_on"])
            ):
                due_waiting.append(job["id"])
            continue
        if status != "ready":
            continue
        dependency_statuses = [
            by_id[dependency]["status"] for dependency in job["depends_on"]
        ]
        if any(status in {"failed", "cancelled"} for status in dependency_statuses):
            dependency_reasons[job["id"]] = "dependency_failed"
            continue
        if any(status != "completed" for status in dependency_statuses):
            dependency_reasons[job["id"]] = "waiting_dependencies"
            continue
        latest_start = critical[job["id"]]["latest_start"]
        if not allow_overdue and latest_start is not None and latest_start < current:
            dependency_reasons[job["id"]] = "deadline_missed"
            continue
        eligible.append(job)

    def sort_key(job: dict[str, Any]) -> tuple[Any, ...]:
        """Prioritize deadline-bound candidate preparation deterministically."""
        urgency = critical[job["id"]]["latest_start"]
        urgency_value = urgency.timestamp() if urgency is not None else math.inf
        shortfall = daily_summary["shortfall"] if daily_summary is not None else None
        daily_first = (
            0
            if shortfall is not None
            and shortfall > 0
            and job["id"] in priority_candidate_ids
            else 1
        )
        return (
            daily_first, urgency_value, JOB_KIND_PRIORITY[job["kind"]],
            -job["priority"], job["id"],
        )

    selected: list[str] = []
    for job in sorted(eligible, key=sort_key):
        if any(
            usage.get(resource, 0.0) + amount > capacities.get(resource, 0.0)
            for resource, amount in job["resources"].items()
        ):
            continue
        selected.append(job["id"])
        for resource, amount in job["resources"].items():
            usage[resource] = usage.get(resource, 0.0) + amount

    selected_set = set(selected)
    runnable_ids = [job["id"] for job in sorted(eligible, key=sort_key)]
    wake_candidates: list[datetime] = []
    for job in jobs:
        if job["status"] in {"waiting", "running"} and job["next_check_at"] is not None:
            wake_candidates.append(max(current, job["next_check_at"]))
        latest_start = critical[job["id"]]["latest_start"]
        if (
            job["status"] == "ready"
            and latest_start is not None
            and latest_start > current
        ):
            wake_candidates.append(latest_start)
    next_wake = min(wake_candidates) if wake_candidates else None

    job_plans: list[dict[str, Any]] = []
    for job in jobs:
        details = critical[job["id"]]
        status = job["status"]
        if job["id"] in selected_set:
            reason = "selected"
        elif status == "ready" and job["id"] in dependency_reasons:
            reason = dependency_reasons[job["id"]]
        elif status == "ready" and job["id"] in {item["id"] for item in eligible}:
            reason = "resource_contention"
        elif status == "waiting" and job["id"] in due_waiting:
            reason = "poll_due"
        elif status == "waiting":
            reason = "waiting_for_check"
        else:
            reason = f"status_{status}"
        slack = None
        if details["latest_start"] is not None:
            slack = (details["latest_start"] - current).total_seconds()
        deadline_feasible = (
            status in {"completed", "failed", "cancelled"}
            or details["latest_start"] is None
            or details["latest_start"] >= current
        )
        job_plans.append(
            {
                "id": job["id"],
                "status": status,
                "selected": job["id"] in selected_set,
                "reason": reason,
                "critical_path_seconds": details["critical_path_seconds"],
                "latest_start_at": _format_timestamp(details["latest_start"]),
                "deadline_slack_seconds": slack,
                "deadline_feasible": deadline_feasible,
            }
        )

    handoff = None
    if not selected:
        if due_waiting:
            handoff_reason = "poll_due"
            next_wake = current
        elif runnable_ids:
            handoff_reason = "resource_contention"
        elif any(
            item["id"] in dependency_reasons
            and dependency_reasons[item["id"]] == "deadline_missed"
            for item in jobs
        ):
            handoff_reason = "deadline_missed"
        elif any(job["status"] in {"ready", "running", "waiting"} for job in jobs):
            handoff_reason = "dependencies_or_waiting"
        else:
            handoff_reason = "no_pending_work"
        handoff = {
            "kind": "native_scheduler",
            "reason": handoff_reason,
            "wake_at": _format_timestamp(next_wake),
        }

    remaining = {
        resource: capacities[resource] - usage.get(resource, 0.0)
        for resource in capacities
    }
    roots = [job["id"] for job in jobs if not job["depends_on"]]
    graph_critical_path = max(
        (critical[job_id]["critical_path_seconds"] for job_id in roots), default=0.0
    )
    return {
        "schema_version": 1,
        "planned_at": _format_timestamp(current),
        "selected_jobs": selected,
        "runnable_jobs": runnable_ids,
        "due_waiting_jobs": sorted(due_waiting),
        "monitor_jobs": sorted(
            due_waiting, key=lambda key: (by_id[key]["next_check_at"], key)
        )[:1],
        "next_wake_at": _format_timestamp(next_wake),
        "handoff": handoff,
        "resource_capacity": capacities,
        "resource_usage": usage,
        "resource_remaining": remaining,
        "resource_overcommit": overcommitted,
        "deadline_missed_jobs": sorted(
            job["id"]
            for job in jobs
            if job["status"] in {"ready", "running", "waiting"}
            and critical[job["id"]]["latest_start"] is not None
            and critical[job["id"]]["latest_start"] < current
        ),
        "critical_path_seconds": graph_critical_path,
        "jobs": job_plans,
        "daily_plan": daily_summary,
    }


def _root_file(
    root: Path, value: Path, name: str, *, allow_missing: bool = False
) -> Path:
    """Resolve a quota/profile reference below root and reject symlinks."""
    root_absolute = root.expanduser().absolute()
    root_resolved = root_absolute.resolve()
    candidate = value.expanduser()
    if not candidate.is_absolute():
        candidate = root_absolute / candidate
    candidate = candidate.absolute()
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SchedulerError(f"{name} must be below root: {value}") from exc
    current = candidate
    while current != root_absolute:
        if current.is_symlink():
            raise SchedulerError(f"{name} must not use symlinks: {value}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    if not allow_missing and not resolved.is_file():
        raise SchedulerError(f"{name} is missing: {value}")
    return resolved


def bind_daily_quota(
    state: dict[str, Any],
    profile_path: Path,
    ledger_path: Path,
    root: Path,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind rule-backed daily quota facts to a copied work-queue state.

    The quota profile and ledger are revalidated below ``root``. The returned
    queue preserves only the caller's candidate IDs and reserve duration;
    target, ready count, reset, and distinctness are always derived from the
    validated profile and materialized ledger artifacts.
    """
    if not isinstance(state, dict):
        raise SchedulerError("state must be an object")
    profile_file = _root_file(root, profile_path, "quota profile")
    ledger_file = _root_file(root, ledger_path, "quota ledger", allow_missing=True)
    profile = validate_quota_profile(profile_file, root.expanduser().resolve())
    records = read_ledger(ledger_file)
    quota_result = build_daily_plan(
        profile,
        records,
        now=now,
        root=root.expanduser().resolve(),
    )
    bound = deepcopy(state)
    existing = bound.get("daily_plan")
    if existing is None:
        existing = {}
    if not isinstance(existing, dict):
        raise SchedulerError("daily_plan must be an object or null")
    candidate_job_ids = existing.get("candidate_job_ids", [])
    if (
        not isinstance(candidate_job_ids, list)
        or any(
            not isinstance(item, str) or not item.strip() for item in candidate_job_ids
        )
        or len(candidate_job_ids) != len(set(candidate_job_ids))
    ):
        raise SchedulerError("daily_plan.candidate_job_ids must be distinct strings")
    reserve_seconds = existing.get("reserve_seconds", 0)
    reserve_seconds = _number(reserve_seconds, "daily_plan.reserve_seconds")
    bound["daily_plan"] = {
        "target_ready_candidates": quota_result["target_ready_artifacts"],
        "ready_candidates": quota_result["distinct_ready_artifacts"],
        "distinct_candidates_validated": True,
        "reset_at": quota_result["next_reset_at"],
        "next_reset_at": quota_result["next_reset_at"],
        "reserve_seconds": reserve_seconds,
        "candidate_job_ids": list(candidate_job_ids),
    }
    return bound, quota_result
