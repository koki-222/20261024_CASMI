"""Validate distribution-aware champion and final-portfolio selection."""

from __future__ import annotations

from datetime import datetime
import json
import math
import re
from pathlib import Path
from typing import Any

from .fingerprint import fingerprint_path


class SelectionError(ValueError):
    """Raised when a final selection plan violates its frozen policy."""


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PROFILE_ROLES = frozenset({"diagnostic", "selection", "stress", "official_feedback"})
ELIGIBILITY_STATUSES = frozenset({"verified", "unverified", "ineligible"})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SelectionError(f"{field} must be a non-empty string")
    return value.strip()


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise SelectionError(f"{field} must be a lowercase SHA-256")
    return value


def _number(value: object, field: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectionError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise SelectionError(f"{field} must be finite and >= {minimum}")
    return result


def _integer(value: object, field: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SelectionError(f"{field} must be an integer >= {minimum}")
    return value


def _timestamp(value: object, field: str) -> datetime:
    """Validate and return one timezone-aware ISO-8601 timestamp."""
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SelectionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SelectionError(f"{field} must include a timezone")
    return parsed


def _reject_unknown(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise SelectionError(f"unknown {field} field(s): {unknown}")


def _snapshot(root: Path | None, value: object, field: str) -> Path:
    """Resolve and hash-check one immutable evidence file below ``root``."""
    if root is None:
        raise SelectionError(f"root is required to verify {field}")
    if not isinstance(value, dict):
        raise SelectionError(f"{field} must be an object")
    _reject_unknown(value, {"path", "sha256"}, field)
    relative = _text(value.get("path"), f"{field}.path")
    digest = _sha(value.get("sha256"), f"{field}.sha256")
    root_resolved = root.expanduser().resolve()
    raw = root_resolved / relative
    if raw.is_symlink():
        raise SelectionError(f"{field} must not be a symlink")
    path = raw.resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise SelectionError(f"{field} escapes root") from exc
    if not path.is_file():
        raise SelectionError(f"{field} is missing: {relative}")
    if fingerprint_path(path)["sha256"] != digest:
        raise SelectionError(f"{field} hash mismatch")
    return path


def _load_evidence(root: Path | None, value: object, field: str) -> dict[str, Any]:
    """Load one hash-bound JSON evidence object."""
    path = _snapshot(root, value, field)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SelectionError(f"{field} is not valid JSON") from exc
    if not isinstance(result, dict):
        raise SelectionError(f"{field} content must be an object")
    return result


def _count_scored_cohort(path: Path, field: str) -> int:
    """Validate a JSONL cohort of unique scored row keys and references."""
    if path.suffix != ".jsonl":
        raise SelectionError(f"{field} must be a .jsonl file")
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SelectionError(f"{field} line {line_number} is invalid JSON") from exc
            try:
                json.dumps(row, allow_nan=False)
            except ValueError as exc:
                raise SelectionError(f"{field} line {line_number} has a non-finite value") from exc
            if not isinstance(row, dict) or set(row) != {"row_key", "reference"}:
                raise SelectionError(f"{field} line {line_number} needs row_key and reference")
            key = _text(row["row_key"], f"{field} line {line_number}.row_key")
            if row["reference"] is None or key in seen:
                raise SelectionError(
                    f"{field} line {line_number} has a missing reference or duplicate key"
                )
            seen.add(key)
    if not seen:
        raise SelectionError(f"{field} must contain scored rows")
    return len(seen)


def _same_number(left: object, right: object) -> bool:
    """Compare two finite numeric values without accepting booleans."""
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-12)


def _validate_candidate_evidence(
    candidate: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    root: Path | None,
    field: str,
    cohort_hashes: dict[str, str],
    cohort_counts: dict[str, int],
) -> None:
    """Bind declared candidate statistics and provenance to hashed evidence."""
    evidence = _load_evidence(
        root, candidate.get("result_evidence"), f"{field}.result_evidence"
    )
    _reject_unknown(
        evidence,
        {
            "schema_version",
            "record_id",
            "artifact_sha256",
            "family",
            "lineage_signature",
            "complexity_rank",
            "profile_results",
            "interval_vs_champion",
            "correlation_with_champion",
            "correlation_champion_record_id",
        },
        f"{field}.result_evidence content",
    )
    if evidence.get("schema_version") != 1:
        raise SelectionError(f"{field}.result_evidence schema_version must be 1")
    for key in ("record_id", "artifact_sha256", "family", "lineage_signature", "complexity_rank"):
        if evidence.get(key) != candidate.get(key):
            raise SelectionError(f"{field}.{key} does not match result evidence")
    raw_results = evidence.get("profile_results")
    if not isinstance(raw_results, dict) or set(raw_results) != set(candidate["metric_values"]):
        raise SelectionError(f"{field}.profile_results do not cover declared metric values")
    counts: dict[str, int] = {}
    for name, declared in candidate["metric_values"].items():
        result = raw_results.get(name)
        if not isinstance(result, dict):
            raise SelectionError(f"{field}.profile_results.{name} must be an object")
        _reject_unknown(
            result,
            {
                "metric_value", "distribution_signature", "group_key",
                "n_observations", "scored_cohort_sha256",
            },
            f"{field}.profile_results.{name}",
        )
        if not _same_number(result.get("metric_value"), declared):
            raise SelectionError(f"{field}.metric_values.{name} does not match result evidence")
        if result.get("distribution_signature") != profiles[name]["distribution_signature"]:
            raise SelectionError(f"{field}.profile_results.{name} uses the wrong distribution")
        if result.get("group_key") != profiles[name].get("group_key"):
            raise SelectionError(f"{field}.profile_results.{name} uses the wrong group_key")
        counts[name] = _integer(
            result.get("n_observations"),
            f"{field}.profile_results.{name}.n_observations",
            1,
        )
        if name in cohort_hashes and _sha(
            result.get("scored_cohort_sha256"),
            f"{field}.profile_results.{name}.scored_cohort_sha256",
        ) != cohort_hashes[name]:
            raise SelectionError(f"{field}.profile_results.{name} scored cohort mismatch")
        if name in cohort_counts and counts[name] != cohort_counts[name]:
            raise SelectionError(
                f"{field}.profile_results.{name} scored cohort observation count differs"
            )
    if evidence.get("interval_vs_champion") != candidate.get("interval_vs_champion"):
        raise SelectionError(f"{field}.interval_vs_champion does not match result evidence")
    if not _same_number(
        evidence.get("correlation_with_champion"),
        candidate.get("correlation_with_champion"),
    ):
        raise SelectionError(f"{field}.correlation_with_champion does not match result evidence")
    if evidence.get("correlation_champion_record_id") != candidate.get(
        "correlation_champion_record_id"
    ):
        raise SelectionError(f"{field}.correlation champion does not match result evidence")


def _validate_eligibility(
    candidate: dict[str, Any], root: Path | None, field: str
) -> str:
    """Validate rule eligibility and its independent, hash-bound evidence."""
    eligibility = candidate.get("eligibility")
    if not isinstance(eligibility, dict):
        raise SelectionError(f"{field}.eligibility must be an object")
    _reject_unknown(eligibility, {"status", "evidence"}, f"{field}.eligibility")
    status = eligibility.get("status")
    if status not in ELIGIBILITY_STATUSES:
        raise SelectionError(f"{field}.eligibility.status is invalid")
    if status != "verified":
        if eligibility.get("evidence") is not None:
            _snapshot(root, eligibility["evidence"], f"{field}.eligibility.evidence")
        return str(status)
    evidence = _load_evidence(
        root, eligibility.get("evidence"), f"{field}.eligibility.evidence"
    )
    _reject_unknown(
        evidence,
        {
            "schema_version",
            "record_id",
            "artifact_sha256",
            "status",
            "rules_snapshot_sha256",
            "checks",
        },
        f"{field}.eligibility.evidence content",
    )
    if evidence.get("schema_version") != 1:
        raise SelectionError(f"{field}.eligibility.evidence schema_version must be 1")
    if evidence.get("record_id") != candidate.get("record_id"):
        raise SelectionError(f"{field}.eligibility record_id mismatch")
    if evidence.get("artifact_sha256") != candidate.get("artifact_sha256"):
        raise SelectionError(f"{field}.eligibility artifact mismatch")
    if evidence.get("status") != "verified":
        raise SelectionError(f"{field}.eligibility evidence is not verified")
    _sha(evidence.get("rules_snapshot_sha256"), f"{field}.eligibility.rules_snapshot_sha256")
    checks = evidence.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(value, str) or not value.strip() for value in checks)
    ):
        raise SelectionError(f"{field}.eligibility.checks must be a non-empty string list")
    return "verified"


def _improvement(direction: str, champion: float, candidate: float) -> float:
    """Return positive values when the candidate improves the objective."""
    return champion - candidate if direction == "min" else candidate - champion


def _interval_is_favorable(direction: str, interval: dict[str, Any]) -> bool:
    """Return whether a candidate-minus-champion interval is wholly favorable."""
    lower = float(interval["lower"])
    upper = float(interval["upper"])
    return upper < 0.0 if direction == "min" else lower > 0.0


def _interval_allows_equivalence(
    direction: str, interval: dict[str, Any], margin: float
) -> bool:
    """Return whether an interval excludes material regression beyond ``margin``."""
    lower = float(interval["lower"])
    upper = float(interval["upper"])
    return upper <= margin if direction == "min" else lower >= -margin


def validate_selection_plan(
    plan: dict[str, Any], root: Path | None = None
) -> dict[str, Any]:
    """Validate a frozen evaluation policy and its final candidate portfolio."""
    if not isinstance(plan, dict) or plan.get("schema_version") not in {1, 2}:
        raise SelectionError("selection plan schema_version must be 1 or 2")
    schema_version = int(plan["schema_version"])
    _reject_unknown(
        plan,
        {
            "schema_version",
            "frozen_at",
            "metric",
            "profiles",
            "policy",
            "champion_before",
            "champion_after",
            "preexisting_submissions",
            "selected",
            "candidates",
        },
        "selection plan",
    )
    _timestamp(plan.get("frozen_at"), "frozen_at")
    metric = plan.get("metric")
    if not isinstance(metric, dict):
        raise SelectionError("metric must be an object")
    _reject_unknown(metric, {"name", "direction"}, "metric")
    metric_name = _text(metric.get("name"), "metric.name")
    direction = metric.get("direction")
    if direction not in {"min", "max"}:
        raise SelectionError("metric.direction must be 'min' or 'max'")

    raw_profiles = plan.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise SelectionError("profiles must be a non-empty list")
    profiles: dict[str, dict[str, Any]] = {}
    for index, profile in enumerate(raw_profiles):
        if not isinstance(profile, dict):
            raise SelectionError(f"profiles[{index}] must be an object")
        _reject_unknown(
            profile,
            {"name", "role", "distribution_signature", "group_key", "manifest", "scored_cohort"},
            f"profiles[{index}]",
        )
        name = _text(profile.get("name"), f"profiles[{index}].name")
        if name in profiles:
            raise SelectionError(f"duplicate profile: {name}")
        role = profile.get("role")
        if role not in PROFILE_ROLES:
            raise SelectionError(f"profiles[{index}].role is invalid")
        signature = _sha(
            profile.get("distribution_signature"),
            f"profiles[{index}].distribution_signature",
        )
        if profile.get("group_key") is not None:
            _text(profile["group_key"], f"profiles[{index}].group_key")
        if schema_version == 2:
            manifest_path = _snapshot(
                root, profile.get("manifest"), f"profiles[{index}].manifest"
            )
            if fingerprint_path(manifest_path)["sha256"] != signature:
                raise SelectionError(
                    f"profiles[{index}].distribution_signature must hash its manifest"
                )
        profiles[name] = profile

    policy = plan.get("policy")
    if not isinstance(policy, dict):
        raise SelectionError("policy must be an object")
    _reject_unknown(
        policy,
        {
            "selection_profile",
            "required_stress_profiles",
            "equivalence_margin",
            "replacement_requires_interval",
            "max_hedge_regret",
            "max_hedge_correlation",
            "max_stress_regret",
            "minimum_distinct_families",
            "minimum_distinct_lineages",
            "max_official_feedback_rounds_per_family",
            "max_official_feedback_rounds_per_lineage",
            "require_matched_scored_cohort",
        },
        "policy",
    )
    selection_profile = _text(policy.get("selection_profile"), "policy.selection_profile")
    if selection_profile not in profiles or profiles[selection_profile]["role"] != "selection":
        raise SelectionError("policy.selection_profile must reference a selection-role profile")
    raw_stress = policy.get("required_stress_profiles")
    if not isinstance(raw_stress, list) or not raw_stress:
        raise SelectionError("policy.required_stress_profiles must be a non-empty list")
    stress_profiles = [
        _text(value, f"policy.required_stress_profiles[{index}]")
        for index, value in enumerate(raw_stress)
    ]
    if len(stress_profiles) != len(set(stress_profiles)):
        raise SelectionError("policy.required_stress_profiles contains duplicates")
    if any(name not in profiles or profiles[name]["role"] != "stress" for name in stress_profiles):
        raise SelectionError("required stress profiles must reference stress-role profiles")
    equivalence_margin = _number(
        policy.get("equivalence_margin"), "policy.equivalence_margin", 0.0
    )
    max_hedge_regret = _number(
        policy.get("max_hedge_regret"), "policy.max_hedge_regret", 0.0
    )
    max_hedge_correlation = _number(
        policy.get("max_hedge_correlation"), "policy.max_hedge_correlation", 0.0
    )
    if max_hedge_correlation > 1.0:
        raise SelectionError("policy.max_hedge_correlation must be <= 1")
    raw_stress_regret = policy.get("max_stress_regret")
    if not isinstance(raw_stress_regret, dict):
        raise SelectionError("policy.max_stress_regret must be an object")
    if set(raw_stress_regret) != set(stress_profiles):
        raise SelectionError("policy.max_stress_regret must cover every required stress profile")
    max_stress_regret = {
        name: _number(
            raw_stress_regret[name],
            f"policy.max_stress_regret.{name}",
            0.0,
        )
        for name in stress_profiles
    }
    minimum_families = _integer(
        policy.get("minimum_distinct_families"),
        "policy.minimum_distinct_families",
        1,
    )
    minimum_lineages = _integer(
        policy.get("minimum_distinct_lineages", 1),
        "policy.minimum_distinct_lineages",
        1,
    )
    feedback_budget = _integer(
        policy.get("max_official_feedback_rounds_per_family", 0),
        "policy.max_official_feedback_rounds_per_family",
    )
    lineage_feedback_budget = _integer(
        policy.get("max_official_feedback_rounds_per_lineage", feedback_budget),
        "policy.max_official_feedback_rounds_per_lineage",
    )
    requires_interval = policy.get("replacement_requires_interval")
    if not isinstance(requires_interval, bool):
        raise SelectionError("policy.replacement_requires_interval must be boolean")
    require_matched_cohort = policy.get("require_matched_scored_cohort", False)
    if not isinstance(require_matched_cohort, bool):
        raise SelectionError("policy.require_matched_scored_cohort must be boolean")
    if require_matched_cohort and schema_version != 2:
        raise SelectionError("matched scored cohort requires selection plan schema_version 2")
    cohort_hashes: dict[str, str] = {}
    cohort_counts: dict[str, int] = {}
    if require_matched_cohort:
        for name in {selection_profile, *stress_profiles}:
            cohort_field = f"profiles.{name}.scored_cohort"
            cohort_path = _snapshot(root, profiles[name].get("scored_cohort"), cohort_field)
            cohort_hashes[name] = fingerprint_path(cohort_path)["sha256"]
            cohort_counts[name] = _count_scored_cohort(cohort_path, cohort_field)

    raw_candidates = plan.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise SelectionError("candidates must be a non-empty list")
    candidates: dict[str, dict[str, Any]] = {}
    artifacts: set[str] = set()
    for index, candidate in enumerate(raw_candidates):
        if not isinstance(candidate, dict):
            raise SelectionError(f"candidates[{index}] must be an object")
        _reject_unknown(
            candidate,
            {
                "record_id",
                "family",
                "lineage_signature",
                "artifact_sha256",
                "complexity_rank",
                "metric_values",
                "interval_vs_champion",
                "correlation_with_champion",
                "correlation_champion_record_id",
                "official_feedback_round",
                "eligibility",
                "result_evidence",
            },
            f"candidates[{index}]",
        )
        record_id = _text(candidate.get("record_id"), f"candidates[{index}].record_id")
        if record_id in candidates:
            raise SelectionError(f"duplicate candidate: {record_id}")
        artifact = _sha(candidate.get("artifact_sha256"), f"candidates[{index}].artifact_sha256")
        if artifact in artifacts:
            raise SelectionError("candidate artifacts must be distinct")
        artifacts.add(artifact)
        _text(candidate.get("family"), f"candidates[{index}].family")
        if schema_version == 2:
            _sha(
                candidate.get("lineage_signature"),
                f"candidates[{index}].lineage_signature",
            )
        _integer(candidate.get("complexity_rank"), f"candidates[{index}].complexity_rank")
        values = candidate.get("metric_values")
        if not isinstance(values, dict):
            raise SelectionError(f"candidates[{index}].metric_values must be an object")
        required_profiles = {selection_profile, *stress_profiles}
        missing = sorted(required_profiles.difference(values))
        if missing:
            raise SelectionError(f"candidate {record_id} is missing profiles: {missing}")
        unknown_values = sorted(set(values).difference(profiles))
        if unknown_values:
            raise SelectionError(f"candidate {record_id} has unknown profiles: {unknown_values}")
        for name, value in values.items():
            _number(value, f"candidates[{index}].metric_values.{name}")
        interval = candidate.get("interval_vs_champion")
        if interval is not None:
            if not isinstance(interval, dict):
                raise SelectionError(f"candidates[{index}].interval_vs_champion must be an object")
            _reject_unknown(
                interval,
                {"lower", "upper", "confidence", "group_key", "champion_record_id"},
                f"candidates[{index}].interval_vs_champion",
            )
            lower = _number(interval.get("lower"), f"candidates[{index}].interval.lower")
            upper = _number(interval.get("upper"), f"candidates[{index}].interval.upper")
            if lower > upper:
                raise SelectionError(f"candidate {record_id} interval lower exceeds upper")
            confidence = _number(
                interval.get("confidence"), f"candidates[{index}].interval.confidence"
            )
            if not 0.0 < confidence < 1.0:
                raise SelectionError(f"candidate {record_id} confidence must be between 0 and 1")
            _text(interval.get("group_key"), f"candidates[{index}].interval.group_key")
            expected_group = profiles[selection_profile].get("group_key")
            if interval.get("group_key") != expected_group:
                raise SelectionError(
                    f"candidate {record_id} interval group_key must match selection profile"
                )
            if schema_version == 2:
                _text(
                    interval.get("champion_record_id"),
                    f"candidates[{index}].interval.champion_record_id",
                )
        correlation = candidate.get("correlation_with_champion")
        if correlation is not None:
            correlation_value = _number(
                correlation, f"candidates[{index}].correlation_with_champion"
            )
            if abs(correlation_value) > 1.0:
                raise SelectionError(f"candidate {record_id} correlation must be within [-1, 1]")
        feedback_round = _integer(
            candidate.get("official_feedback_round", 0),
            f"candidates[{index}].official_feedback_round",
        )
        active_feedback_budget = (
            lineage_feedback_budget if schema_version == 2 else feedback_budget
        )
        if feedback_round > active_feedback_budget:
            raise SelectionError(f"candidate {record_id} exceeds official feedback budget")
        if schema_version == 2:
            _text(
                candidate.get("correlation_champion_record_id"),
                f"candidates[{index}].correlation_champion_record_id",
            )
            _validate_candidate_evidence(
                candidate, profiles, root, f"candidates[{index}]", cohort_hashes, cohort_counts
            )
            _validate_eligibility(candidate, root, f"candidates[{index}]")
        candidates[record_id] = candidate

    champion_before = _text(plan.get("champion_before"), "champion_before")
    champion_after = _text(plan.get("champion_after"), "champion_after")
    if champion_before not in candidates or champion_after not in candidates:
        raise SelectionError("champion references must exist in candidates")
    preexisting = plan.get("preexisting_submissions", [])
    if not isinstance(preexisting, list):
        raise SelectionError("preexisting_submissions must be a list")
    preexisting_ids = [
        _text(value, f"preexisting_submissions[{index}]")
        for index, value in enumerate(preexisting)
    ]
    if len(preexisting_ids) != len(set(preexisting_ids)):
        raise SelectionError("preexisting_submissions contains duplicates")
    if any(value not in candidates for value in preexisting_ids):
        raise SelectionError("preexisting_submissions contains an unknown candidate")
    selected = plan.get("selected")
    if not isinstance(selected, list) or not selected:
        raise SelectionError("selected must be a non-empty list")
    selected_ids = [_text(value, f"selected[{index}]") for index, value in enumerate(selected)]
    if len(selected_ids) != len(set(selected_ids)):
        raise SelectionError("selected contains duplicates")
    if any(value not in candidates for value in selected_ids):
        raise SelectionError("selected contains an unknown candidate")
    if champion_after not in selected_ids:
        raise SelectionError("champion_after must be selected")
    if schema_version == 2:
        required_eligible = {champion_before, champion_after, *selected_ids}
        unverified = sorted(
            record_id
            for record_id in required_eligible
            if candidates[record_id]["eligibility"]["status"] != "verified"
        )
        if unverified:
            raise SelectionError(
                f"champions and selected candidates must be verified eligible: {unverified}"
            )
        wrong_intervals = sorted(
            record_id
            for record_id, candidate in candidates.items()
            if candidate.get("interval_vs_champion") is not None
            and candidate["interval_vs_champion"].get("champion_record_id")
            != champion_before
        )
        if wrong_intervals:
            raise SelectionError(
                f"interval comparison target must be champion_before: {wrong_intervals}"
            )

    before = candidates[champion_before]
    after = candidates[champion_after]
    champion_score = float(before["metric_values"][selection_profile])
    after_score = float(after["metric_values"][selection_profile])
    champion_improvement = _improvement(direction, champion_score, after_score)
    replacement_reason = "retained"
    if champion_after != champion_before:
        interval = after.get("interval_vs_champion")
        significant = champion_improvement > equivalence_margin
        if requires_interval:
            significant = significant and isinstance(interval, dict) and _interval_is_favorable(
                direction, interval
            )
        simpler_equivalent = (
            int(after["complexity_rank"]) < int(before["complexity_rank"])
            and champion_improvement >= -equivalence_margin
            and (
                not requires_interval
                or isinstance(interval, dict)
                and _interval_allows_equivalence(direction, interval, equivalence_margin)
            )
        )
        if not significant and not simpler_equivalent:
            raise SelectionError(
                "champion replacement requires material interval-supported "
                "improvement or a simpler equivalent"
            )
        for profile in stress_profiles:
            stress_regret = max(
                0.0,
                -_improvement(
                    direction,
                    float(before["metric_values"][profile]),
                    float(after["metric_values"][profile]),
                ),
            )
            if stress_regret > max_stress_regret[profile]:
                raise SelectionError(
                    f"champion replacement exceeds max_stress_regret for {profile}"
                )
        replacement_reason = "material_improvement" if significant else "simpler_equivalent"

    champion_after_score = float(after["metric_values"][selection_profile])
    for record_id in selected_ids:
        selected_candidate = candidates[record_id]
        for profile in stress_profiles:
            stress_regret = max(
                0.0,
                -_improvement(
                    direction,
                    float(after["metric_values"][profile]),
                    float(selected_candidate["metric_values"][profile]),
                ),
            )
            if stress_regret > max_stress_regret[profile]:
                raise SelectionError(
                    f"selected candidate {record_id} exceeds max_stress_regret for {profile}"
                )
        if record_id == champion_after:
            continue
        hedge = selected_candidate
        if schema_version == 2 and hedge.get("correlation_champion_record_id") != champion_after:
            raise SelectionError(
                f"hedge {record_id} correlation target must be champion_after"
            )
        if hedge["family"] == after["family"]:
            raise SelectionError(f"hedge {record_id} must use a distinct family")
        hedge_score = float(hedge["metric_values"][selection_profile])
        regret = max(0.0, -_improvement(direction, champion_after_score, hedge_score))
        if regret > max_hedge_regret:
            raise SelectionError(f"hedge {record_id} exceeds max_hedge_regret")
        correlation = hedge.get("correlation_with_champion")
        if correlation is None or abs(float(correlation)) > max_hedge_correlation:
            raise SelectionError(f"hedge {record_id} lacks sufficient measured diversity")
    distinct_families = {candidates[record_id]["family"] for record_id in selected_ids}
    if len(distinct_families) < minimum_families:
        raise SelectionError("selected portfolio lacks the required distinct families")
    distinct_lineages = (
        {candidates[record_id]["lineage_signature"] for record_id in selected_ids}
        if schema_version == 2
        else set()
    )
    if schema_version == 2 and len(distinct_lineages) < minimum_lineages:
        raise SelectionError("selected portfolio lacks the required distinct lineages")
    if schema_version == 2:
        feedback_by_lineage: dict[str, int] = {}
        for candidate in candidates.values():
            if int(candidate.get("official_feedback_round", 0)) > 0:
                lineage = str(candidate["lineage_signature"])
                feedback_by_lineage[lineage] = feedback_by_lineage.get(lineage, 0) + 1
        exceeded = sorted(
            lineage
            for lineage, count in feedback_by_lineage.items()
            if count > lineage_feedback_budget
        )
        if exceeded:
            raise SelectionError(
                "official feedback budget is exceeded by shared lineage: "
                f"{exceeded}"
            )
    return {
        "status": "PASS",
        "metric": metric_name,
        "direction": direction,
        "selection_profile": selection_profile,
        "champion_before": champion_before,
        "champion_after": champion_after,
        "champion_improvement": champion_improvement,
        "replacement_reason": replacement_reason,
        "selected": selected_ids,
        "preexisting_submissions": preexisting_ids,
        "distinct_families": len(distinct_families),
        "distinct_lineages": len(distinct_lineages) if schema_version == 2 else None,
        "eligibility_verified": schema_version == 2,
    }


def load_selection_plan(path: Path, root: Path | None = None) -> dict[str, Any]:
    """Load and validate a selection-plan JSON file."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SelectionError(f"selection plan is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SelectionError(f"invalid selection plan: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SelectionError("selection plan must be an object")
    validate_selection_plan(value, root=root)
    return value
