"""Command-line interface for the model-independent competition controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .audit import AuditError, audit_repository
from .completion import CompletionError, validate_completion_state
from .compute import (
    ComputeError,
    append_compute_job,
    load_compute_job,
    load_compute_profile,
    load_compute_request,
    plan_compute_request,
    validate_compute_job,
)
from .continuation import ContinuationError, plan_continuation
from .contract import ContractError, load_contract, validate_submission
from .controller import (
    ControllerError,
    evaluate_controller_state,
    load_controller_state,
    provider_error_fingerprint,
)
from .exploration import ExplorationError, evaluate_exploration
from .feedback import FeedbackError, compare_official_feedback
from .fidelity import FidelityError, evaluate_fidelity
from .fingerprint import canonical_json_digest, fingerprint_path
from .gate import evaluate_gate
from .ledger import LedgerError, append_record, assert_new_submission
from .preflight import PreflightError, validate_preflight
from .quota import QuotaError, build_daily_plan, validate_quota_profile
from .research import ResearchError, validate_research_manifest
from .retrospective import (
    RetrospectiveError,
    evaluate_retrospective,
    load_retrospective,
)
from .runtime import RuntimeError as ExecutionError
from .runtime import reconcile_interrupted, registry_status, run_experiment
from .scheduler import SchedulerError, bind_daily_quota, plan_work_queue
from .selection import SelectionError, load_selection_plan, validate_selection_plan
from .signature import SignatureError, computation_payload, computation_signature
from .state import StateError, compact_state
from .submission import build_submission_plan
from .validation import ValidationError, plan_validation, run_check
from .worker_inputs import WorkerInputError, check_worker_inputs
from .workflow import (
    WorkflowError,
    close_pdca,
    handoff,
    initialize,
    mark_ready,
    observe_score,
    publish,
    review_pdca,
    start_pdca,
    workflow_status,
)


def _load_records(path: Path) -> list[dict[str, Any]]:
    """Load records from JSON/JSONL so quota planning uses the append-only ledger."""
    if path.suffix.lower() == ".jsonl":
        from .ledger import read_ledger

        return read_ledger(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw.get("records", []) if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise QuotaError("records JSON must be a list or an object with records")
    return records


def _dump(value: object, output: Path | None = None) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if output is None:
        print(encoded)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded + "\n", encoding="utf-8")
    print(output)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("bootstrap-init", "bootstrap-ready", "bootstrap-publish", "bootstrap-handoff", "workflow-status", "pdca-start", "pdca-review", "pdca-score", "pdca-close"):
        command = subparsers.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name == "bootstrap-init":
            command.add_argument("url")
        elif name == "bootstrap-ready":
            command.add_argument("package")
        elif name == "bootstrap-publish":
            command.add_argument("--remote", required=True)
            command.add_argument("--branch", required=True)
            command.add_argument("--authorize-push", action="store_true")
        elif name == "pdca-start":
            command.add_argument("--goal", required=True)
            command.add_argument("--intake", required=True)
            command.add_argument("--trigger", required=True)
        elif name in {"pdca-review", "pdca-score"}:
            command.add_argument("evidence")
        elif name == "pdca-close":
            command.add_argument("--reason", choices=("budget_exhausted", "deadline", "competition_ended"), required=True)
            command.add_argument("--evidence")

    contract = subparsers.add_parser("contract-check")
    contract.add_argument("contract", type=Path)

    fingerprint = subparsers.add_parser("fingerprint")
    fingerprint.add_argument("path", type=Path)

    submission = subparsers.add_parser("submission-check")
    submission.add_argument("contract", type=Path)
    submission.add_argument("submission", type=Path)
    submission.add_argument("--sample", type=Path)
    submission.add_argument("--ledger", type=Path, help="既提出SHAを照合するJSONL台帳")
    submission.add_argument("--root", type=Path, help="legacy submit payload再ハッシュのrepo root")

    submission_plan = subparsers.add_parser("submission-plan")
    submission_plan.add_argument("contract", type=Path)
    submission_plan.add_argument("submission", type=Path)
    submission_plan.add_argument("--sample", type=Path)
    submission_plan.add_argument("--ledger", type=Path, help="既提出SHAを照合するJSONL台帳")
    submission_plan.add_argument("--root", type=Path, help="legacy submit payload再ハッシュのrepo root")

    ledger = subparsers.add_parser("ledger-append")
    ledger.add_argument("ledger", type=Path)
    ledger.add_argument("record", type=Path, help="JSON object to append")
    ledger.add_argument("--root", type=Path, help="artifact pathの検証ルート")
    ledger.add_argument("--allow-duplicate-artifact", action="store_true")

    gate = subparsers.add_parser("gate")
    gate.add_argument("contract", type=Path)
    gate.add_argument("record", type=Path)
    gate.add_argument("--root", type=Path, help="check/official evidence artifactの再ハッシュルート")

    research = subparsers.add_parser("research-check")
    research.add_argument("manifest", type=Path)
    research.add_argument("--root", type=Path, required=True, help="research snapshotの検証ルート")
    research.add_argument("--contract", type=Path, help="required_sections/minimum_sourcesを読む契約")

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("contract", type=Path)
    preflight.add_argument("manifest", type=Path)
    preflight.add_argument("--root", type=Path, required=True, help="research/quota snapshotの検証ルート")
    preflight.add_argument("--quota", type=Path, help="Kaggle/SIGNATE等の日次quota profile")

    compute_profile = subparsers.add_parser("compute-profile-check")
    compute_profile.add_argument("profile", type=Path, help="competition compute profile")
    compute_profile.add_argument("--root", type=Path, required=True, help="rules snapshotの検証ルート")

    compute_plan = subparsers.add_parser("compute-plan")
    compute_plan.add_argument("profile", type=Path, help="competition compute profile")
    compute_plan.add_argument("request", type=Path, help="microbenchmark-backed resource request")
    compute_plan.add_argument("--root", type=Path, required=True, help="snapshotの検証ルート")

    compute_job = subparsers.add_parser("compute-job-check")
    compute_job.add_argument("profile", type=Path, help="competition compute profile")
    compute_job.add_argument("job", type=Path, help="immutable compute job state")
    compute_job.add_argument("--root", type=Path, required=True, help="snapshot/artifactの検証ルート")

    compute_append = subparsers.add_parser("compute-job-append")
    compute_append.add_argument("profile", type=Path, help="competition compute profile")
    compute_append.add_argument("job", type=Path, help="immutable compute job state")
    compute_append.add_argument("ledger", type=Path, help="append-only compute job JSONL")
    compute_append.add_argument("--root", type=Path, required=True, help="snapshot/artifactの検証ルート")

    quota_check = subparsers.add_parser("quota-check")
    quota_check.add_argument("profile", type=Path)
    quota_check.add_argument("--root", type=Path, required=True, help="official quota snapshotの検証ルート")

    quota_plan = subparsers.add_parser("quota-plan")
    quota_plan.add_argument("profile", type=Path)
    quota_plan.add_argument("records", type=Path, help="record list JSONまたはappend-only JSONL ledger")
    quota_plan.add_argument("--root", type=Path, required=True, help="official quota snapshotの検証ルート")
    quota_plan.add_argument("--now", type=str, help="period計算用ISO-8601時刻")

    completion = subparsers.add_parser("completion-check")
    completion.add_argument("state", type=Path)
    completion.add_argument("--root", type=Path, required=True, help="completion evidenceの検証ルート")
    completion.add_argument("--quota-plan", type=Path, help="quota-plan JSON（daily_quota_required時に必須）")

    controller = subparsers.add_parser("controller-check")
    controller.add_argument("state", type=Path, help="controller state JSON")
    controller.add_argument("--root", type=Path, required=True, help="state/provenance evidenceの検証ルート")
    controller.add_argument("--now", type=str, help="再開時刻/circuit判定用ISO-8601時刻")
    controller.add_argument("--job-id", help="scoped blockerを評価するqueue job")

    for name in ("work-plan", "wait-check"):
        continuation = subparsers.add_parser(name)
        continuation.add_argument("plan", type=Path)
        continuation.add_argument("--root", type=Path, required=True)
        continuation.add_argument("--now", help="timezone-aware planning time")

    selection = subparsers.add_parser("selection-check")
    selection.add_argument("plan", type=Path, help="frozen champion/portfolio selection plan")
    selection.add_argument("--root", type=Path, help="schema v2 evidence root")

    retrospective = subparsers.add_parser("retrospective-check")
    retrospective.add_argument("manifest", type=Path, help="final Public/Private result manifest")
    retrospective.add_argument("--root", type=Path, required=True, help="official/selection evidence root")

    feedback = subparsers.add_parser("official-feedback-check")
    feedback.add_argument("plan", type=Path, help="normalized evolving-score comparison")
    feedback.add_argument("--root", type=Path, required=True, help="official snapshot evidence root")

    worker_inputs = subparsers.add_parser("worker-input-check")
    worker_inputs.add_argument("manifest", type=Path, help="all dependent worker-stage inputs")
    worker_inputs.add_argument("--root", type=Path, required=True, help="materialized worker input root")

    provider_error = subparsers.add_parser("provider-error-fingerprint")
    provider_error.add_argument("--provider", required=True)
    provider_error.add_argument("--code", required=True)
    provider_error.add_argument("--message", required=True)

    signature = subparsers.add_parser("experiment-signature")
    signature.add_argument("spec", type=Path)
    run = subparsers.add_parser("experiment-run")
    run.add_argument("request", type=Path)
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--registry", type=Path, required=True)
    status = subparsers.add_parser("experiment-status")
    status.add_argument("--registry", type=Path, required=True)
    reconcile = subparsers.add_parser("experiment-reconcile")
    reconcile.add_argument("attempt_id")
    reconcile.add_argument("--registry", type=Path, required=True)
    reconcile.add_argument("--root", type=Path, required=True)
    reconcile.add_argument("--evidence", type=Path, required=True, help="path/SHA snapshot JSON for interruption")
    reconcile.add_argument("--reason", required=True)
    for name in ("queue-plan", "fidelity-check", "exploration-check", "validation-plan"):
        check = subparsers.add_parser(name)
        check.add_argument("plan", type=Path)
        if name != "queue-plan":
            check.add_argument("--root", type=Path, required=True)
        if name in {"queue-plan", "exploration-check"}:
            check.add_argument("--now")
        if name == "queue-plan":
            check.add_argument("--quota", type=Path, help="validated daily quota profile")
            check.add_argument("--ledger", type=Path, help="append-only submission ledger")
            check.add_argument("--root", type=Path, help="quota/ledger evidence root")
    validation_run = subparsers.add_parser("validation-run")
    validation_run.add_argument("plan", type=Path, help="validation plan JSON")
    validation_run.add_argument("check_id", help="declared check id")
    validation_run.add_argument("--root", type=Path, required=True, help="execution and evidence root")
    validation_run.add_argument("--report", type=Path, required=True, help="new JSON validation report path")
    validation_run.add_argument("--evidence", type=Path, required=True, help="new process evidence path")
    validation_run.add_argument("--timeout", type=float, default=60.0, help="bounded child timeout in seconds")
    compact = subparsers.add_parser("state-compact")
    compact.add_argument("state", type=Path)
    compact.add_argument("--archive", type=Path, required=True)
    compact.add_argument("--output", type=Path, required=True)

    audit = subparsers.add_parser("audit")
    audit.add_argument("root", type=Path)
    audit.add_argument("--layout", type=Path, help="layout profile JSON（省略時はroot/automation/layout.json）")
    audit.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a deterministic automation command."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "contract-check":
            _dump(load_contract(args.contract))
        elif args.command == "fingerprint":
            _dump(fingerprint_path(args.path))
        elif args.command == "submission-check":
            contract = load_contract(args.contract)
            result = validate_submission(args.submission, contract, args.sample)
            if args.ledger is not None:
                result.update(assert_new_submission(args.submission, args.ledger, root=args.root))
            _dump(result)
        elif args.command == "submission-plan":
            contract = load_contract(args.contract)
            _dump(
                build_submission_plan(
                    contract,
                    args.submission,
                    args.sample,
                    args.ledger,
                    root=args.root,
                )
            )
        elif args.command == "ledger-append":
            record = json.loads(args.record.read_text(encoding="utf-8"))
            digest = append_record(
                args.ledger,
                record,
                args.allow_duplicate_artifact,
                root=args.root,
            )
            _dump({"status": "PASS", "record_digest": digest})
        elif args.command == "gate":
            contract = load_contract(args.contract)
            record = json.loads(args.record.read_text(encoding="utf-8"))
            result = evaluate_gate(record, contract, root=args.root)
            _dump(result)
            return 0 if result["decision"] in {"candidate", "adopted", "rejected"} else 1
        elif args.command == "bootstrap-init":
            _dump(initialize(args.root, args.url))
        elif args.command == "bootstrap-ready":
            _dump(mark_ready(args.root, args.package))
        elif args.command == "bootstrap-publish":
            _dump(publish(args.root, args.remote, args.branch, args.authorize_push))
        elif args.command == "bootstrap-handoff":
            _dump(handoff(args.root))
        elif args.command == "workflow-status":
            _dump(workflow_status(args.root))
        elif args.command == "pdca-start":
            _dump(start_pdca(args.root, args.goal, args.intake, args.trigger))
        elif args.command == "pdca-review":
            _dump(review_pdca(args.root, args.evidence))
        elif args.command == "pdca-score":
            _dump(observe_score(args.root, args.evidence))
        elif args.command == "pdca-close":
            _dump(close_pdca(args.root, args.reason, args.evidence))
        elif args.command == "research-check":
            profile: dict[str, Any] = {}
            if args.contract is not None:
                profile = load_contract(args.contract).get("research", {})
            _dump(
                validate_research_manifest(
                    args.manifest,
                    args.root,
                    required_sections=profile.get("required_sections") or ("rules", "data", "metric", "submission", "timeline_quota", "leakage", "history"),
                    minimum_sources=int(profile.get("minimum_sources", 1)),
                )
            )
        elif args.command == "preflight":
            _dump(validate_preflight(args.contract, args.manifest, args.root, args.quota))
        elif args.command == "compute-profile-check":
            _dump(load_compute_profile(args.profile, args.root))
        elif args.command == "compute-plan":
            profile = load_compute_profile(args.profile, args.root)
            request = load_compute_request(args.request, args.root)
            _dump(plan_compute_request(profile, request, args.root))
        elif args.command == "compute-job-check":
            profile = load_compute_profile(args.profile, args.root)
            job = load_compute_job(args.job)
            _dump(validate_compute_job(job, profile, args.root))
        elif args.command == "compute-job-append":
            profile = load_compute_profile(args.profile, args.root)
            job = load_compute_job(args.job)
            _dump(append_compute_job(args.ledger, job, profile, args.root))
        elif args.command == "quota-check":
            _dump(validate_quota_profile(args.profile, args.root))
        elif args.command == "quota-plan":
            profile = validate_quota_profile(args.profile, args.root)
            records = _load_records(args.records)
            from datetime import datetime

            now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
            _dump(build_daily_plan(profile, records, now=now, root=args.root))
        elif args.command == "completion-check":
            quota_plan = None
            if args.quota_plan is not None:
                quota_plan = json.loads(args.quota_plan.read_text(encoding="utf-8"))
            _dump(validate_completion_state(args.state, args.root, quota_plan=quota_plan))
        elif args.command == "controller-check":
            from datetime import datetime

            now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
            result = evaluate_controller_state(
                load_controller_state(args.state), args.root, now=now, job_id=args.job_id
            )
            _dump(result)
            return 0 if result["allowed"] else 1
        elif args.command in {"work-plan", "wait-check"}:
            from datetime import datetime

            now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            result = plan_continuation(plan, args.root, now)
            _dump(result)
            return 0 if args.command == "work-plan" or result["can_wait"] else 1
        elif args.command == "selection-check":
            plan = load_selection_plan(args.plan, root=args.root)
            _dump(validate_selection_plan(plan, root=args.root))
        elif args.command == "retrospective-check":
            _dump(evaluate_retrospective(load_retrospective(args.manifest), args.root))
        elif args.command == "official-feedback-check":
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            result = compare_official_feedback(plan, args.root)
            _dump(result)
            return 0 if result["status"] == "COMPARABLE" else 1
        elif args.command == "worker-input-check":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            result = check_worker_inputs(manifest, args.root)
            _dump(result)
            return 0 if result["status"] == "PASS" else 1
        elif args.command == "provider-error-fingerprint":
            _dump(
                {
                    "provider": args.provider,
                    "error_fingerprint": provider_error_fingerprint(
                        args.provider,
                        args.code,
                        args.message,
                    ),
                }
            )
        elif args.command == "audit":
            _dump(audit_repository(args.root, args.layout), args.output)
        elif args.command == "experiment-signature":
            spec = json.loads(args.spec.read_text(encoding="utf-8"))
            payload = computation_payload(spec)
            _dump({
                "computation_signature": computation_signature(spec),
                "hypothesis_signature": computation_signature(spec, True),
                "mechanism_signature": canonical_json_digest({key: payload[key] for key in ("task_type", "code", "config", "evaluation")}),
                "input_signature": canonical_json_digest({key: payload[key] for key in ("data", "split")}),
                "run_snapshot_payloads": {key: payload[key] for key in ("code", "data", "split", "config")},
            })
        elif args.command == "experiment-run":
            result = run_experiment(json.loads(args.request.read_text(encoding="utf-8")), args.root, args.registry)
            _dump(result)
            return 0 if result.get("status", "completed") == "completed" else 1
        elif args.command == "experiment-status":
            _dump(registry_status(args.registry))
        elif args.command == "experiment-reconcile":
            _dump(reconcile_interrupted(args.registry, args.root, args.attempt_id, json.loads(args.evidence.read_text(encoding="utf-8")), args.reason))
        elif args.command == "validation-run":
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            validation = plan_validation(plan, args.root)
            decisions = {item["check_id"]: item for item in validation["checks"]}
            decision = decisions.get(args.check_id)
            if decision is None:
                _dump({"status": "BLOCKED", "check_id": args.check_id, "reason": "unknown check id", "validation": validation})
                return 1
            if decision["status"] == "CACHED_PASS":
                cached = next(item for item in plan.get("receipts", []) if item.get("check_id") == args.check_id)
                _dump({"status": "REUSE", "check_id": args.check_id, "receipt": cached, "validation": validation})
                return 0
            if args.check_id not in validation.get("runnable_checks", []):
                _dump({"status": "BLOCKED", "check_id": args.check_id, "reason": decision.get("reason", "check is not runnable"), "validation": validation})
                return 1
            check = next(item for item in plan["checks"] if item.get("id") == args.check_id)
            try:
                receipt = run_check(
                    check,
                    args.root,
                    report_path=args.report,
                    evidence_path=args.evidence,
                    timeout_seconds=args.timeout,
                )
            except ValidationError as exc:
                _dump({"status": "FAILED", "check_id": args.check_id, "reason": str(exc), "validation": validation})
                return 1
            _dump({"status": "EXECUTED" if receipt["status"] == "PASS" else "FAILED", "check_id": args.check_id, "receipt": receipt, "validation": validation})
            return 0 if receipt["status"] == "PASS" else 1
        elif args.command in {"queue-plan", "fidelity-check", "exploration-check", "validation-plan"}:
            from datetime import datetime

            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            if args.command == "queue-plan":
                now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
                quota = None
                if any(value is not None for value in (args.quota, args.ledger, args.root)):
                    if any(value is None for value in (args.quota, args.ledger, args.root)):
                        raise SchedulerError("--quota, --ledger and --root must be supplied together")
                    plan, quota = bind_daily_quota(plan, args.quota, args.ledger, args.root, now)
                result = plan_work_queue(plan, now)
                result["quota"] = quota
                result["daily_quota_evidence"] = "verified_profile_and_materialized_ledger" if quota is not None else "not_checked"
            elif args.command == "fidelity-check":
                result = evaluate_fidelity(plan, args.root)
            elif args.command == "exploration-check":
                now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
                result = evaluate_exploration(plan, args.root, now)
            else:
                result = plan_validation(plan, args.root)
            _dump(result)
            return 0 if result.get("status", "PASS") in {"PASS", "ready", "promote"} else 1
        elif args.command == "state-compact":
            _dump(compact_state(args.state, args.archive, args.output))
        else:
            raise AssertionError(f"unknown command: {args.command}")
    except (
        AuditError,
        ContractError,
        ControllerError,
        LedgerError,
        ResearchError,
        QuotaError,
        CompletionError,
        ComputeError,
        PreflightError,
        RetrospectiveError,
        FeedbackError,
        WorkerInputError,
        SelectionError,
        ExecutionError,
        SignatureError,
        SchedulerError,
        FidelityError,
        ExplorationError,
        ValidationError,
        StateError,
        ContinuationError,
        WorkflowError,
        FileNotFoundError,
        IsADirectoryError,
        NotADirectoryError,
    ) as exc:
        print(f"ERROR: {exc}")
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON: {exc}")
        return 2
    return 0
