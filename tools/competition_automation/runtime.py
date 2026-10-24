"""Guarded local execution with atomic claims and content-bound receipts.

This is a local runner, not a sandbox or a remote submission adapter. Only an
explicit argv is executed; scientific validity still belongs to the task's
validators. SQLite protects claims across concurrent orchestrators on one host.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .controller import evaluate_controller_state
from .fingerprint import canonical_json_digest, sha256_file
from .signature import computation_payload, computation_signature


class RuntimeError(ValueError):
    """Raised when a dispatch cannot be bound to its approved computation."""


REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "computation_spec",
        "controller",
        "output_dir",
        "outputs",
        "timeout_seconds",
        "retry_of",
        "retry_reason",
        "control_of",
        "control_reason",
        "exploration",
        "fidelity",
        "validation",
        "queue",
        "queue_job_id",
        "preflight",
    }
)
TOKEN_RE = re.compile(
    r"\{(code\.[A-Za-z_][A-Za-z0-9_]*|data\.[A-Za-z_][A-Za-z0-9_]*|split\.[A-Za-z_][A-Za-z0-9_]*|config_path|output_dir|root|budget_units|budget_unit|checkpoint_manifest)\}"
)


def _now() -> str:
    """Return an aware timestamp for an append-only event."""
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    """Serialize finite, deterministic JSON."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("runtime values must be finite JSON") from exc


def _path(root: Path, relative: object) -> Path:
    """Resolve a non-symlink path beneath the workspace."""
    if (
        not isinstance(relative, str)
        or not relative.strip()
        or Path(relative).is_absolute()
    ):
        raise RuntimeError("paths must be non-empty and relative to root")
    raw = root / relative
    if raw.is_symlink() or any(
        parent.is_symlink()
        for parent in raw.parents
        if parent != root and root in parent.parents
    ):
        raise RuntimeError(f"symlink paths are not accepted: {relative}")
    resolved = raw.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError(f"path escapes root: {relative}")
    return resolved


def read_snapshot(root: Path, snapshot: object) -> dict[str, Any]:
    """Read a JSON object only after verifying its frozen content hash."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"path", "sha256"}:
        raise RuntimeError("snapshot needs path and sha256")
    path = _path(root, snapshot["path"])
    if sha256_file(path) != snapshot["sha256"]:
        raise RuntimeError(f"snapshot hash mismatch: {snapshot['path']}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("snapshot must contain an object")
    return value


def _verify_resume_inputs(spec: dict[str, Any], root: Path) -> None:
    """Bind resume identity to the manifest and every immutable component."""
    resume = spec.get("resume")
    if resume is None:
        return
    from .fidelity import verify_checkpoint_manifest

    manifest = read_snapshot(root, resume["manifest"])
    if manifest.get("resume_mode") != resume["mode"]:
        raise RuntimeError("resume mode differs from checkpoint manifest")
    result = verify_checkpoint_manifest(root, resume["manifest"]["path"])
    if result["status"] != "PASS":
        raise RuntimeError("resume checkpoint components changed or disappeared")


@contextmanager
def _database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a local transactional registry and initialize its compact schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        create table if not exists attempts (
            id text primary key, computation text not null, hypothesis text not null,
            status text not null, created_at text not null, pid integer,
            request text not null, receipt text, worker_pid integer
        );
        create unique index if not exists active_computation on attempts(computation)
            where status = 'running';
        create table if not exists events (
            sequence integer primary key, attempt_id text not null,
            created_at text not null, event text not null
        );
    """)
    try:
        yield connection
    finally:
        connection.close()


def _event(db: sqlite3.Connection, attempt: str, value: dict[str, Any]) -> None:
    """Append a durable transition within the caller's transaction."""
    db.execute(
        "insert into events(attempt_id,created_at,event) values (?,?,?)",
        (attempt, _now(), _json(value)),
    )


def registry_status(path: Path) -> dict[str, Any]:
    """Return compact run state; full evidence remains in the registry."""
    if not path.exists():
        return {"attempts": [], "counts": {}}
    with _database(path) as db:
        rows = [
            dict(row)
            for row in db.execute(
                "select id,computation,hypothesis,status,created_at,pid,worker_pid from attempts order by created_at"
            )
        ]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"attempts": rows, "counts": counts}


def inspect_registry(root: Path) -> list[dict[str, Any]]:
    """Read authoritative attempts without creating or modifying the registry."""
    path = _path(root, "automation/runs.sqlite")
    if not path.exists():
        return []
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        return [
            {
                **dict(row), "request": json.loads(row["request"]),
                "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            }
            for row in connection.execute("select * from attempts order by created_at desc, id")
        ]
    except sqlite3.Error as exc:
        raise RuntimeError("cannot inspect the authoritative run registry") from exc
    finally:
        connection.close()


def _check_request(
    request: dict[str, Any], root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind the controller, frozen inputs and proposed command before claim."""
    if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
        raise RuntimeError("unknown runtime request fields")
    if type(request.get("schema_version")) is not int or request["schema_version"] != 1:
        raise RuntimeError("runtime request schema_version must be 1")
    _json(request)
    if (request.get("queue") is None) != (request.get("queue_job_id") is None):
        raise RuntimeError("queue and queue_job_id must be supplied together")
    spec = request.get("computation_spec")
    computation_payload(spec)
    if "seed" in spec["config"] and spec["config"]["seed"] != spec["seed"]:
        raise RuntimeError("config.seed must match the explicit computation seed")
    for group in ("code", "data", "split"):
        for snapshot in spec[group].values():
            if sha256_file(_path(root, snapshot["path"])) != snapshot["sha256"]:
                raise RuntimeError(f"{group} input hash mismatch")
    _verify_resume_inputs(spec, root)
    controller = read_snapshot(root, request.get("controller"))
    action = controller.get("action", {})
    if action.get("type") != "experiment":
        raise RuntimeError(
            "experiment-run requires action.type=experiment; audit/resume_check cannot authorize execution"
        )
    if not controller.get("hypothesis_source"):
        raise RuntimeError(
            "experiment-run requires a user Issue intake policy; migrate legacy controllers first"
        )
    if action.get("computation_spec") != spec:
        raise RuntimeError(
            "controller action.computation_spec differs from the executed computation"
        )
    outputs = request.get("outputs")
    if (
        not isinstance(outputs, list)
        or not outputs
        or any(not isinstance(item, str) for item in outputs)
        or len(outputs) != len(set(outputs))
    ):
        raise RuntimeError("outputs must be a non-empty distinct list")
    output = _path(root, request.get("output_dir"))
    if output == root:
        raise RuntimeError("output_dir must be a new subdirectory")
    for item in outputs:
        if item in {"process.log", "effective_config.json"}:
            raise RuntimeError(
                "outputs must name scientific artifacts, not runner files"
            )
        _path(output, item)
    timeout = request.get("timeout_seconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise RuntimeError("timeout_seconds must be positive")
    command = spec["command"]
    if spec["task_type"] == "training" and not any(
        "{budget_units}" in arg for arg in command
    ):
        raise RuntimeError(
            "training command must bind {budget_units} to the adapter's cumulative training limit"
        )
    if spec.get("resume") is not None:
        if not any("{checkpoint_manifest}" in arg for arg in command):
            raise RuntimeError("resume command must consume {checkpoint_manifest}")
        if request.get("fidelity") is None:
            raise RuntimeError("resume dispatch requires a fidelity plan")
    elif any("{checkpoint_manifest}" in arg for arg in command):
        raise RuntimeError("checkpoint_manifest requires computation_spec.resume")
    for token, key in (("{budget_units}", "units"), ("{budget_unit}", "unit")):
        if any(token in arg for arg in command) and key not in spec["budget"]:
            raise RuntimeError(f"{token} requires budget.{key}")
    if not any("{code." in token for token in command):
        raise RuntimeError("command must reference a hashed code role")
    for arg in command:
        for group, role in re.findall(
            r"\{(code|data|split)\.([A-Za-z_][A-Za-z0-9_]*)\}", arg
        ):
            if role not in spec[group]:
                raise RuntimeError(f"unknown command role: {group}.{role}")
        if "{" in TOKEN_RE.sub("", arg) or "}" in TOKEN_RE.sub("", arg):
            raise RuntimeError(
                "unknown command placeholder; use argv without shell expansion"
            )
    for field in ("retry_of", "control_of"):
        if request.get(field) is not None:
            reason = request.get(field.replace("_of", "_reason"))
            if (
                not isinstance(request[field], str)
                or not request[field]
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                raise RuntimeError(f"{field} requires an attempt ID and reason")
    return spec, controller


def _plan_gates(
    request: dict[str, Any],
    controller: dict[str, Any],
    root: Path,
    active: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply selected planning gates to the exact action, without dispatching."""
    results: dict[str, Any] = {}
    from .preflight import validate_preflight

    preflight = request.get("preflight")
    if (
        not isinstance(preflight, dict)
        or not {"contract", "research"}.issubset(preflight)
        or set(preflight) - {"contract", "research", "quota"}
    ):
        raise RuntimeError(
            "preflight needs contract/research snapshots and optional quota"
        )
    for snapshot in preflight.values():
        read_snapshot(root, snapshot)
    results["preflight"] = validate_preflight(
        _path(root, preflight["contract"]["path"]),
        _path(root, preflight["research"]["path"]),
        root,
        _path(root, preflight["quota"]["path"]) if "quota" in preflight else None,
    )
    if request.get("exploration") is None:
        raise RuntimeError("an Issue-bound exploration plan is required")
    from .exploration import evaluate_exploration

    plan = read_snapshot(root, request["exploration"])
    if plan.get("source_issue") != controller["action"].get("source_issue"):
        raise RuntimeError(
            "exploration plan and controller must reference the same Issue"
        )
    results["exploration"] = evaluate_exploration(plan, root)
    evaluation = request["computation_spec"]["evaluation"]
    if any(
        plan["objective"].get(key) != evaluation.get(key)
        for key in ("metric", "direction")
    ):
        raise RuntimeError("exploration objective differs from dispatched evaluation")
    if results["exploration"]["status"] != "ready":
        raise RuntimeError("exploration constraints need review")
    family = controller["action"]["family"]
    families = {item["id"]: item for item in results["exploration"]["families"]}
    if family not in families:
        raise RuntimeError("action family is outside the approved exploration plan")
    axis = next(
        item
        for item in results["exploration"]["axes"]
        if item["id"] == families[family]["axis"]
    )
    if axis["id"] in results["exploration"]["exhausted_axes"]:
        raise RuntimeError("action axis is closed or exhausted")
    if axis["classification"] == "non_mechanism" and not request.get("control_of"):
        raise RuntimeError("cosmetic/seed changes require a referenced control")
    if "rl" in plan["capabilities"] and not results["exploration"]["rl"]["eligible"]:
        raise RuntimeError(
            "RL capability lacks a defined environment, reward or cheap baseline"
        )
    if request.get("fidelity") is not None:
        from .fidelity import evaluate_fidelity

        fidelity = read_snapshot(root, request["fidelity"])
        checkpoint = fidelity.get("checkpoint")
        resume = request["computation_spec"].get("resume")
        if checkpoint is not None and checkpoint.get("resume_mode") != "new_run":
            expected_resume = {
                "mode": checkpoint["resume_mode"],
                "manifest": {
                    "path": checkpoint["manifest_path"],
                    "sha256": checkpoint["manifest_sha256"],
                },
            }
            if resume != expected_resume:
                raise RuntimeError(
                    "fidelity checkpoint must be bound in computation_spec.resume"
                )
        elif resume is not None:
            raise RuntimeError("resume computation cannot use a new-run fidelity plan")
        if any(
            fidelity["objective"].get(key) != evaluation.get(key)
            for key in ("metric", "direction")
        ):
            raise RuntimeError("fidelity objective differs from dispatched evaluation")
        payload = computation_payload(request["computation_spec"])
        expected_lineage = {
            f"{key}_sha256": canonical_json_digest(payload[key])
            for key in ("code", "data", "split", "config")
        }
        if any(
            fidelity["lineage"].get(key) != value
            for key, value in expected_lineage.items()
        ):
            raise RuntimeError(
                "fidelity plan lineage differs from dispatched computation"
            )
        if not set(fidelity.get("approved_issue_axes", [])).issubset(
            {item["id"] for item in results["exploration"]["axes"]}
        ):
            raise RuntimeError("partial artifact reuse axes must be Issue-authorized")
        results["fidelity"] = evaluate_fidelity(fidelity, root)
        if results["fidelity"]["status"] in {
            "blocked",
            "infeasible",
            "prune",
            "paused",
            "inconclusive",
        }:
            raise RuntimeError("fidelity plan does not authorize this dispatch")
        budget = request["computation_spec"]["budget"]
        next_budget = results["fidelity"]["feasibility"].get("next_stage_budget")
        allowed = results["fidelity"]["feasibility"].get("allowed_budget")
        if budget.get("unit") != fidelity["unit"] or type(budget.get("units")) not in {
            int,
            float,
        }:
            raise RuntimeError("dispatch budget needs matching unit and numeric units")
        if (
            next_budget is None
            or allowed is None
            or not 0 < budget["units"] <= min(next_budget, allowed)
        ):
            raise RuntimeError(
                "dispatch exceeds the authorized next rung or measured runtime budget"
            )
    elif request["computation_spec"]["task_type"] == "training":
        raise RuntimeError("training dispatch requires a fidelity plan")
    if request.get("validation") is not None:
        from .validation import plan_validation

        results["validation"] = plan_validation(
            read_snapshot(root, request["validation"]), root
        )
        if results["validation"].get("status") != "PASS":
            raise RuntimeError(
                "required validation is not ready; run the selected checks first"
            )
    continuation_path = _path(root, "automation/continuation.json")
    if continuation_path.exists():
        from .continuation import check_dispatch

        results["continuation"] = check_dispatch(request, root)
    if request.get("queue") is not None:
        from .scheduler import bind_daily_quota, plan_work_queue

        queue = read_snapshot(root, request["queue"])
        if "quota" in preflight:
            queue, results["quota"] = bind_daily_quota(
                queue,
                _path(root, preflight["quota"]["path"]),
                root / "automation/ledger.jsonl",
                root,
            )
        elif queue.get("daily_plan") is not None:
            raise RuntimeError(
                "daily queue dispatch requires a rule-backed quota profile"
            )
        by_id = {job["id"]: job for job in queue["jobs"]}
        for running in active:
            reservation = running.get("queue_reservation")
            if reservation is None:
                raise RuntimeError(
                    "a running standalone job has no resource reservation; wait for completion"
                )
            job_id = reservation["job_id"]
            if job_id not in by_id:
                raise RuntimeError(
                    "work queue omits a running job; retain its resource reservation"
                )
            if queue["resources"] != running.get("queue_capacity"):
                raise RuntimeError(
                    "queue capacity cannot change while jobs are running"
                )
            by_id[job_id]["status"] = "running"
            by_id[job_id]["resources"] = reservation["resources"]
        results["queue"] = (
            results["continuation"]["queue_plan"]
            if "continuation" in results else plan_work_queue(queue)
        )
        if request.get("queue_job_id") not in results["queue"]["selected_jobs"]:
            raise RuntimeError("queue did not select the requested job")
        results["queue_reservation"] = {
            "job_id": request["queue_job_id"],
            "resources": by_id[request["queue_job_id"]]["resources"],
        }
        results["queue_capacity"] = queue["resources"]
    elif active:
        raise RuntimeError("concurrent dispatch requires a shared resource queue")
    return results


def _verify_receipt(receipt: dict[str, Any], root: Path) -> None:
    """Reject reuse if any saved output or execution log changed or disappeared."""
    for item in receipt["artifacts"]:
        if sha256_file(_path(root, item["path"])) != item["sha256"]:
            raise RuntimeError(
                "completed output changed; restore it or record an explicit recovery"
            )


def _claim(
    request: dict[str, Any], root: Path, registry: Path, controller: dict[str, Any]
) -> dict[str, Any]:
    """Atomically join/reuse a computation or allocate exactly one attempt."""
    spec = request["computation_spec"]
    exact, hypothesis = computation_signature(spec), computation_signature(spec, True)
    with _database(registry) as db:
        db.execute("begin immediate")
        rows = list(
            db.execute(
                "select * from attempts where computation=? order by created_at desc",
                (exact,),
            )
        )
        for row in rows:
            origin = json.loads(row["request"])
            reuse = {
                "attempt_id": row["id"],
                "computation_signature": exact,
                "original_output_dir": origin["request"]["output_dir"],
                "original_source_issue": origin["source_issue"],
                "requested_source_issue": controller["action"]["source_issue"],
                "original_request_sha256": canonical_json_digest(origin["request"]),
            }
            if row["status"] == "running":
                _event(db, row["id"], {"transition": "joined", **reuse})
                db.execute("commit")
                return {"decision": "JOIN_RUNNING", **reuse}
            if row["status"] == "completed" and not request.get("control_of"):
                receipt = json.loads(row["receipt"])
                _verify_receipt(receipt, root)
                if not set(request["outputs"]).issubset(origin["request"]["outputs"]):
                    raise RuntimeError(
                        "completed computation lacks the newly requested outputs; inspect original artifacts"
                    )
                _event(db, row["id"], {"transition": "reused", **reuse})
                db.execute("commit")
                return {
                    "decision": "REUSE_COMPLETED",
                    **reuse,
                    "reused_artifact_paths": [
                        item["path"] for item in receipt["artifacts"]
                    ],
                    "receipt": receipt,
                }
        from .workflow import check_dispatch as check_workflow_dispatch

        check_workflow_dispatch(
            root,
            controller["action"]["source_issue"],
            [dict(row) for row in db.execute("select created_at from attempts")],
            request["timeout_seconds"],
            spec["evaluation"],
            request["preflight"]["contract"],
        )
        retry = request.get("retry_of")
        control = request.get("control_of")
        if rows and not retry and not control:
            raise RuntimeError(
                "computation was already attempted; retry needs an infrastructure/preemption reference"
            )
        if retry:
            previous = db.execute(
                "select * from attempts where id=?", (retry,)
            ).fetchone()
            if (
                previous is None
                or previous["computation"] != exact
                or previous["status"] not in {"infra_failed", "preempted"}
            ):
                raise RuntimeError(
                    "retry_of must reference the same computation's infrastructure failure or preemption"
                )
        if control:
            previous = db.execute(
                "select * from attempts where id=?", (control,)
            ).fetchone()
            if (
                previous is None
                or previous["hypothesis"] != hypothesis
                or previous["status"] == "running"
            ):
                raise RuntimeError(
                    "control_of must reference a stopped matching hypothesis"
                )
            if (
                controller["action"].get("control", {}).get("reference_record_id")
                != control
            ):
                raise RuntimeError(
                    "explicit controls must also be declared in the controller"
                )
        elif (
            not retry
            and db.execute(
                "select 1 from attempts where hypothesis=? limit 1", (hypothesis,)
            ).fetchone()
        ):
            raise RuntimeError(
                "seed-only change is not a new hypothesis; use a referenced control"
            )
        decision = evaluate_controller_state(
            controller, root, job_id=request.get("queue_job_id")
        )
        if not decision["allowed"]:
            raise RuntimeError(f"controller denied execution: {decision['decision']}")
        active = [
            json.loads(row["request"])
            for row in db.execute("select request from attempts where status='running'")
        ]
        plans = _plan_gates(request, controller, root, active)
        # Reconcile family budget against actual claims, not a caller-supplied counter.
        family = controller["action"]["family"]
        limit = next(
            item["budget"]
            for item in plans["exploration"]["families"]
            if item["id"] == family
        )
        issue = controller["action"]["source_issue"]
        issue_key = f"{controller['hypothesis_source']['repository']}#{issue['number']}"
        mechanism_key = canonical_json_digest(
            {
                key: controller["action"][key]
                for key in ("mechanism_signature", "input_signature")
            }
        )
        used: set[str] = set()
        for row in db.execute("select hypothesis,status,request from attempts"):
            if row["status"] in {"infra_failed", "preempted"}:
                continue
            stored = json.loads(row["request"])
            if (
                stored["family"] == family
                and stored["issue_key"] == issue_key
                and not stored.get("control_of")
            ):
                used.add(stored.get("mechanism_key", row["hypothesis"]))
        if (
            not retry
            and not control
            and mechanism_key not in used
            and len(used) >= limit
        ):
            raise RuntimeError("registered family budget is exhausted")
        attempt = uuid4().hex
        stored = {
            "request": request,
            "family": family,
            "issue_key": issue_key,
            "source_issue": issue,
            "control_of": control,
            "queue_reservation": plans.get("queue_reservation"),
            "queue_capacity": plans.get("queue_capacity"),
            "mechanism_key": mechanism_key,
        }
        db.execute(
            "insert into attempts(id,computation,hypothesis,status,created_at,pid,request) values (?,?,?,?,?,?,?)",
            (attempt, exact, hypothesis, "running", _now(), os.getpid(), _json(stored)),
        )
        _event(
            db,
            attempt,
            {"transition": "claimed", "controller": decision, "plans": plans},
        )
        db.execute("commit")
    return {
        "decision": "CLAIMED",
        "attempt_id": attempt,
        "computation_signature": exact,
        "hypothesis_signature": hypothesis,
        "controller": decision,
    }


def _finish(registry: Path, attempt: str, status: str, receipt: dict[str, Any]) -> None:
    """Persist a terminal transition and immutable evidence in one transaction."""
    with _database(registry) as db:
        db.execute("begin immediate")
        updated = db.execute(
            "update attempts set status=?,receipt=? where id=? and status='running'",
            (status, _json(receipt), attempt),
        )
        if updated.rowcount != 1:
            raise RuntimeError("attempt is not running")
        _event(db, attempt, {"transition": status, "receipt": receipt})
        db.execute("commit")


def run_experiment(
    request: dict[str, Any], root: Path, registry: Path
) -> dict[str, Any]:
    """Claim and execute one authorized local argv, or reuse an existing result."""
    root = root.expanduser().resolve()
    if (
        registry.resolve() != (root / "automation/runs.sqlite").resolve()
        or registry.is_symlink()
    ):
        raise RuntimeError(
            "use the single workspace registry at automation/runs.sqlite"
        )
    spec, controller = _check_request(request, root)
    claim = _claim(request, root, registry, controller)
    if claim["decision"] != "CLAIMED":
        return claim
    attempt = claim["attempt_id"]
    output = _path(root, request["output_dir"])
    receipt: dict[str, Any] = {
        **claim,
        "started_at": _now(),
        "source_issue": controller["action"]["source_issue"],
        "run_snapshot": controller["action"]["run_snapshot"],
        "artifacts": [],
        "official_status": "not_submitted",
        "output_dir": request["output_dir"],
        "retry_of": request.get("retry_of"),
        "resume_verified": False,
        "scientific_decision": "not_evaluated",
    }
    status = "infra_failed"
    process: subprocess.Popen[bytes] | None = None
    try:
        output.mkdir(parents=True, exist_ok=False)
        config = output / "effective_config.json"
        config.write_text(
            _json({**spec["config"], "seed": spec["seed"]}) + "\n", encoding="utf-8"
        )
        values = {
            "output_dir": str(output),
            "root": str(root),
            "config_path": str(config),
            "budget_units": str(spec["budget"].get("units", "")),
            "budget_unit": str(spec["budget"].get("unit", "")),
        }
        if spec.get("resume") is not None:
            values["checkpoint_manifest"] = str(
                _path(root, spec["resume"]["manifest"]["path"])
            )
        for group in ("code", "data", "split"):
            values.update(
                {
                    f"{group}.{role}": str(_path(root, snapshot["path"]))
                    for role, snapshot in spec[group].items()
                }
            )
        command = [
            TOKEN_RE.sub(lambda match: values[match[1]], arg) for arg in spec["command"]
        ]
        receipt["command"] = command
        receipt["request_sha256"] = canonical_json_digest(request)
        with (output / "process.log").open("xb") as log:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            with _database(registry) as db:
                db.execute(
                    "update attempts set worker_pid=? where id=?",
                    (process.pid, attempt),
                )
            receipt["worker_pid"] = process.pid
            try:
                returncode = process.wait(timeout=request["timeout_seconds"])
                status = "completed" if returncode == 0 else "failed"
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                returncode = process.returncode
                status = "preempted"
                receipt["incomplete_reason"] = type(exc).__name__
                receipt["hypothesis_result"] = "inconclusive"
            receipt["returncode"] = returncode
        for relative in ["process.log", "effective_config.json", *request["outputs"]]:
            path = _path(output, relative)
            if path.is_file():
                receipt["artifacts"].append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": sha256_file(path),
                    }
                )
            elif status == "completed":
                status = "failed"
                receipt.setdefault("missing_outputs", []).append(relative)
        # A producer modifying its frozen sources cannot receive a reusable receipt.
        for group in ("code", "data", "split"):
            for snapshot in spec[group].values():
                if sha256_file(_path(root, snapshot["path"])) != snapshot["sha256"]:
                    status = "failed"
                    receipt["input_mutated"] = True
        _verify_resume_inputs(spec, root)
    except (OSError, ValueError) as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        status = "infra_failed" if process is None else "failed"
    receipt.update({"status": status, "finished_at": _now()})
    _finish(registry, attempt, status, receipt)
    return {
        "decision": "EXECUTED",
        "attempt_id": attempt,
        "status": status,
        "receipt": receipt,
    }


def reconcile_interrupted(
    registry: Path, root: Path, attempt: str, evidence: dict[str, str], reason: str
) -> dict[str, Any]:
    """Close a dead local runner only with an explicit interruption record."""
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeError("recovery needs a reason")
    observed = read_snapshot(root.resolve(), evidence)
    with _database(registry) as db:
        row = db.execute("select * from attempts where id=?", (attempt,)).fetchone()
    if row is None or row["status"] != "running":
        raise RuntimeError("only an abandoned running attempt can be reconciled")
    original = json.loads(row["request"])["request"]
    if (
        observed.get("attempt_id") != attempt
        or observed.get("computation_signature") != row["computation"]
        or observed.get("request_sha256") != canonical_json_digest(original)
    ):
        raise RuntimeError("interruption evidence does not identify this attempt")
    for pid in (row["pid"], row["worker_pid"]):
        if pid is None:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise RuntimeError(
            "runner or worker is still alive; refusing to release its claim"
        )
    receipt = {
        "status": "preempted",
        "reason": reason,
        "evidence": evidence,
        "hypothesis_result": "inconclusive",
        "artifacts": [],
    }
    _finish(registry, attempt, "preempted", receipt)
    return receipt
