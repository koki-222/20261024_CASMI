"""Model-independent competition experiment controls.

The package deliberately uses only the Python standard library.  Competition
specific training code can remain in each project, while contracts, hashes,
submission checks, and evidence decisions stay deterministic.
"""

from .contract import ContractError, load_contract, validate_submission
from .fingerprint import fingerprint_path, sha256_file, sha256_tree
from .gate import evaluate_gate
from .record import RecordError, validate_record
from .quota import QuotaError, build_daily_plan, validate_quota_profile
from .research import ResearchError, validate_research_manifest
from .completion import CompletionError, validate_completion_state
from .compute import (
    ComputeError,
    append_compute_job,
    compute_job_signature,
    load_compute_job,
    load_compute_profile,
    load_compute_request,
    plan_compute_request,
    validate_compute_job,
    validate_compute_profile,
    validate_compute_request,
)
from .signature import experiment_signature
from .preflight import PreflightError, validate_preflight
from .submission import build_submission_plan

__all__ = [
    "ContractError",
    "evaluate_gate",
    "fingerprint_path",
    "load_contract",
    "RecordError",
    "QuotaError",
    "ResearchError",
    "CompletionError",
    "ComputeError",
    "append_compute_job",
    "compute_job_signature",
    "build_daily_plan",
    "load_compute_job",
    "load_compute_profile",
    "load_compute_request",
    "plan_compute_request",
    "sha256_file",
    "sha256_tree",
    "validate_record",
    "validate_quota_profile",
    "validate_research_manifest",
    "validate_completion_state",
    "validate_compute_job",
    "validate_compute_profile",
    "validate_compute_request",
    "experiment_signature",
    "PreflightError",
    "validate_preflight",
    "build_submission_plan",
    "validate_submission",
]

__version__ = "0.1.0"
