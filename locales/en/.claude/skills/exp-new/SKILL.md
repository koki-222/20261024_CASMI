---
name: exp-new
description: Create an experiment folder bound to the competition contract and a user-authored Issue, using an explicitly selected reference. Generates SESSION_NOTES.md and run.sh.
argument-hint: "<experiment_name> [--human] [--reference reference_generic|reference|reference_sim]"
---

# New experiment folder skill

## Procedure

1. Parse the experiment name, `--human`, and optional `--reference` from `$ARGUMENTS`. Use the dependency-free `reference_generic/` when no reference is given. Select DL, simulation, or RL references only when the Issue and competition contract require them; a reference choice is not a hypothesis.
2. Check `research/manifest.json`, the contract, controller action, and the target Issue snapshot. Stop unless `preflight` and `controller-check` pass and the Issue has a testable prediction, baseline, and stopping condition.

```bash
PYTHONPATH=. python3 -m tools.competition_automation research-check \
  research/manifest.json --root . --contract automation/contracts/template.json
PYTHONPATH=. python3 -m tools.competition_automation controller-check \
  automation/controller_state.json --root .
```

3. Inspect `workspace/` and choose the next unused `expA00_<name>` for an agent or `exp200_<name>` for a human. Do not create a folder for an existing name or computation signature.
   Before the first run, initialize `automation/issue_intake.json` and canonical `automation/continuation.json`. Bind the authorized scope, a shared-queue `kind: hypothesis` job, and its frozen request. Check selection with `work-plan`; use `wait-check` after execution and before any idle handoff. An empty queue alone is not proof of exhausted research.
4. Copy the selected reference. The default `reference_generic/` is request-first and selects no model, data, metric, or budget. Its linear-regression demo is an explicit integration-test opt-in; do not copy the DL reference automatically to tasks that do not need it.

```bash
cp -r reference_generic/ workspace/<new-folder>/
```

5. Create `SESSION_NOTES.md` with the Issue number and snapshot SHA, hypothesis/computation/attempt IDs, mechanism/input axis, baseline, selection/stress profiles, budget, stopping condition, and resume plan.

```markdown
# SESSION_NOTES — <experiment folder>

## Session
- Date: YYYY-MM-DD
- Folder: workspace/<experiment folder>
- Source Issue: #<number> / <snapshot SHA>
- hypothesis_id / computation_id / attempt_id: ...
- Goal: ...

## Plan
- mechanism/input axis: ...
- baseline: ...
- selection/stress profile: ...
- budget / stopping condition: ...
- resume/reuse plan: ...

## Results
<!-- metrics, evaluation population, uncertainty, failed checks, artifact SHA -->
```

6. Make `run.sh` pass an Issue-bound request to `experiment-run`; it must not call a training script directly. Stop when the request has not been materialized. Run the atomic claim and `fidelity-check` before invoking a launcher. Do not put long score waits in the script.

```bash
#!/usr/bin/env bash
set -euo pipefail
EXP_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git -C "$EXP_DIR" rev-parse --show-toplevel)"
EXP_NAME="$(basename "$EXP_DIR")"
REQUEST="${REQUEST_PATH:-automation/requests/${EXP_NAME}.json}"
test -f "$ROOT/$REQUEST"
PYTHONPATH="$ROOT" python3 -m tools.competition_automation experiment-run \
  "$ROOT/$REQUEST" --root "$ROOT" --registry "$ROOT/automation/runs.sqlite"
```

7. Long-running training checkpoints must include model, optimizer, scheduler, AMP scaler, RNG, sampler position, config, and data/code snapshots when applicable and actually used. For CPU, tabular, or simulation work, document which state does not apply. A wall-time stop is `budget_exhausted_inconclusive`.
8. After completion, inspect the state, metrics, checks, and artifact SHA recorded by `experiment-run`; run `validation-plan`, `exploration-check`, or `state-compact` as needed. A submission becomes `ready` only after `submission-check` and human handoff.

## Notes

- New seeds, thresholds, repackaging, and reruns of the same checkpoint are controls or attempts, not new hypotheses.
- Reuse completed computations and join running ones. A rerun requires a new Issue and mechanism/input signature unless the previous attempt was `infra_failed` or `preempted` and records `retry_of`.
- If hypotheses are exhausted, do not invent an Issue or RL idea. Return `WAITING_FOR_USER_ISSUE` or `blocked`.
- Do not copy datasets, models, credentials, or large artifacts into the experiment folder. Follow the contract and `.gitignore`.
