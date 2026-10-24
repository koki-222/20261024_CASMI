"""Request-first reference scaffold with an explicit opt-in tiny demo.

The default mode performs no training. The optional demo is a deterministic,
standard-library-only linear regression used to test lifecycle and checkpoint
plumbing. It is not a competition baseline or a competition metric.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
CHECKPOINT_SCHEMA_VERSION = 1


def _finite_float(value: object, name: str, minimum: float | None = None) -> float:
    """Validate and return a finite floating-point configuration value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number <= minimum):
        suffix = f" > {minimum}" if minimum is not None else " finite"
        raise ValueError(f"{name} must be{suffix}")
    return number


def _positive_int(value: object, name: str) -> int:
    """Validate and return a positive integer configuration value."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _demo_config(raw: object) -> dict[str, Any]:
    """Validate the explicit demo configuration without choosing defaults."""
    if not isinstance(raw, dict):
        raise ValueError("config.demo must be an object")
    if raw.get("enabled") is not True:
        raise ValueError("the reference demo is opt-in: set config.demo.enabled=true")
    data = raw.get("data")
    target = raw.get("target")
    features = raw.get("features")
    if not isinstance(data, str) or not data.strip():
        raise ValueError("config.demo.data is required")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("config.demo.target is required")
    if (
        not isinstance(features, list)
        or not features
        or any(not isinstance(item, str) or not item.strip() for item in features)
    ):
        raise ValueError(
            "config.demo.features must be a non-empty list of column names"
        )
    if len(set(features)) != len(features) or target in features:
        raise ValueError("config.demo.features must be unique and exclude target")
    optimizer = raw.get("optimizer")
    if optimizer != "sgd":
        raise ValueError("the demo supports only optimizer=sgd")
    _finite_float(raw.get("learning_rate"), "config.demo.learning_rate", minimum=0.0)
    _positive_int(raw.get("steps"), "config.demo.steps")
    _positive_int(raw.get("checkpoint_interval"), "config.demo.checkpoint_interval")
    return dict(raw)


def load_rows(
    path: Path, target: str, features: list[str]
) -> tuple[list[list[float]], list[float], str]:
    """Load explicitly selected finite numeric features and labels from CSV."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        if target not in columns:
            raise ValueError(f"target column not found: {target}")
        missing = [column for column in features if column not in columns]
        if missing:
            raise ValueError(f"feature columns not found: {missing}")
        matrix: list[list[float]] = []
        labels: list[float] = []
        for row_number, row in enumerate(reader, start=2):
            try:
                values = [float(row[column]) for column in features]
                label = float(row[target])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"non-numeric value at CSV row {row_number}") from exc
            if not all(math.isfinite(value) for value in values + [label]):
                raise ValueError(f"non-finite value at CSV row {row_number}")
            matrix.append(values)
            labels.append(label)
    if not matrix:
        raise ValueError("CSV contains no rows")
    return matrix, labels, digest


def predict(
    matrix: list[list[float]], weights: list[float], bias: float
) -> list[float]:
    """Compute linear predictions for the explicitly selected features."""
    return [
        sum(weight * value for weight, value in zip(weights, row)) + bias
        for row in matrix
    ]


def write_checkpoint(path: Path, state: dict[str, Any]) -> None:
    """Write a checkpoint atomically, retaining the complete deterministic state."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _resume_contract(
    data_sha256: str,
    target: str,
    features: list[str],
    optimizer: str,
    learning_rate: float,
) -> dict[str, Any]:
    """Build the fields whose equality is required for an exact resume."""
    return {
        "data_sha256": data_sha256,
        "target": target,
        "features": features,
        "optimizer": optimizer,
        "learning_rate": learning_rate,
    }


def train(
    matrix: list[list[float]],
    labels: list[float],
    steps: int,
    learning_rate: float,
    checkpoint_interval: int,
    checkpoint_path: Path,
    resume_path: Path | None,
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Run deterministic SGD and support exact continuation from a checkpoint."""
    _positive_int(steps, "steps")
    _finite_float(learning_rate, "learning_rate", minimum=0.0)
    _positive_int(checkpoint_interval, "checkpoint_interval")
    if not matrix or len(matrix) != len(labels):
        raise ValueError("matrix and labels must be non-empty and have equal length")
    feature_count = len(matrix[0])
    if feature_count == 0 or any(len(row) != feature_count for row in matrix):
        raise ValueError("matrix must have a stable positive feature count")
    state: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "step": 0,
        "weights": [0.0] * feature_count,
        "bias": 0.0,
        "resume_contract": contract,
    }
    if resume_path is not None:
        try:
            state = json.loads(resume_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid resume checkpoint: {resume_path}") from exc
        if not isinstance(state, dict):
            raise ValueError("resume checkpoint must be a JSON object")
        if state.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported checkpoint schema")
        if state.get("resume_contract") != contract:
            raise ValueError(
                "resume data/features/optimizer/learning_rate do not match"
            )
        if not isinstance(state.get("step"), int) or state["step"] < 0:
            raise ValueError("checkpoint step must be a non-negative integer")
        if (
            not isinstance(state.get("weights"), list)
            or len(state["weights"]) != feature_count
        ):
            raise ValueError("resume checkpoint feature count does not match input")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in state["weights"]
        ):
            raise ValueError("resume checkpoint weights must be finite numbers")
        if (
            isinstance(state.get("bias"), bool)
            or not isinstance(state.get("bias"), (int, float))
            or not math.isfinite(float(state["bias"]))
        ):
            raise ValueError("resume checkpoint bias must be a finite number")
        if state["step"] >= steps:
            raise ValueError("requested steps must be greater than the checkpoint step")
    start = int(state["step"])
    weights = [float(value) for value in state["weights"]]
    bias = float(state["bias"])
    for step in range(start, steps):
        index = step % len(matrix)
        row = matrix[index]
        error = (
            sum(weight * value for weight, value in zip(weights, row))
            + bias
            - labels[index]
        )
        scale = 2.0 / len(matrix)
        for feature_index, value in enumerate(row):
            weights[feature_index] -= learning_rate * scale * error * value
        bias -= learning_rate * scale * error
        completed = step + 1
        if completed % checkpoint_interval == 0 or completed == steps:
            write_checkpoint(
                checkpoint_path,
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "step": completed,
                    "weights": weights,
                    "bias": bias,
                    "resume_contract": contract,
                },
            )
    predictions = predict(matrix, weights, bias)
    train_mse = sum(
        (prediction - label) ** 2 for prediction, label in zip(predictions, labels)
    ) / len(labels)
    return {
        "status": "demo_completed",
        "steps": steps,
        "evaluation_scope": "train_only",
        "metric_name": "train_mse",
        "train_mse": train_mse,
        "checkpoint": str(checkpoint_path),
        "weights": weights,
        "bias": bias,
    }


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument(
        "--demo", action="store_true", help="explicitly opt into the lifecycle demo"
    )
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--steps", type=int)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    """Run the opt-in demo, or reject direct model execution in request mode."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if not args.demo:
        raise SystemExit(
            "reference_generic is request-first; use experiment-run, or pass --demo explicitly"
        )
    demo = _demo_config(config.get("demo"))
    data_path = Path(demo["data"])
    matrix, labels, data_sha = load_rows(data_path, demo["target"], demo["features"])
    steps = args.steps if args.steps is not None else demo["steps"]
    output_dir = args.output_dir or Path(demo.get("output_dir", "results"))
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "last.json"
    contract = _resume_contract(
        data_sha,
        demo["target"],
        demo["features"],
        demo["optimizer"],
        float(demo["learning_rate"]),
    )
    result = train(
        matrix,
        labels,
        steps,
        float(demo["learning_rate"]),
        int(demo["checkpoint_interval"]),
        checkpoint,
        args.resume,
        contract,
    )
    result.pop("weights")
    result.pop("bias")
    (output_dir / "metrics.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    LOGGER.info("completed steps=%s train_mse=%s", result["steps"], result["train_mse"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
