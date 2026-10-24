"""Single command for the mandatory pre-analysis research and quota checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import ContractError, load_contract
from .quota import QuotaError, validate_quota_profile
from .research import DEFAULT_REQUIRED_SECTIONS, ResearchError, validate_research_manifest


QUOTA_PLATFORMS = frozenset({"kaggle", "kaggle_code_competition", "signate", "zindi"})


class PreflightError(ValueError):
    """Raised when analysis preconditions are incomplete."""


def validate_preflight(
    contract_path: Path,
    manifest_path: Path,
    root: Path,
    quota_path: Path | None = None,
) -> dict[str, Any]:
    """Validate contract, research snapshots, and rule-backed quota before analysis."""
    try:
        contract = load_contract(contract_path)
    except ContractError as exc:
        raise PreflightError(f"contract preflight failed: {exc}") from exc
    research_profile = contract.get("research", {})
    try:
        research = validate_research_manifest(
            manifest_path,
            root,
            required_sections=research_profile.get("required_sections") or DEFAULT_REQUIRED_SECTIONS,
            minimum_sources=int(research_profile.get("minimum_sources", 1)),
        )
    except (ResearchError, ValueError) as exc:
        raise PreflightError(f"research preflight failed: {exc}") from exc
    platform = str(contract["platform"]["kind"]).lower()
    quota: dict[str, Any] | None = None
    requires_quota = (
        platform in QUOTA_PLATFORMS
        or platform.startswith("kaggle")
        or platform.startswith("signate")
    )
    if requires_quota:
        if quota_path is None:
            raise PreflightError(
                f"{platform} requires a rule-backed quota profile; pass --quota automation/quota.json"
            )
        try:
            quota = validate_quota_profile(quota_path, root)
        except QuotaError as exc:
            raise PreflightError(f"quota preflight failed: {exc}") from exc
    elif quota_path is not None:
        try:
            quota = validate_quota_profile(quota_path, root)
        except QuotaError as exc:
            raise PreflightError(f"quota preflight failed: {exc}") from exc
    return {
        "status": "PASS",
        "contract": str(contract_path.expanduser().resolve()),
        "research": research,
        "quota": quota,
    }
