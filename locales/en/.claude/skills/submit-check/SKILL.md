---
name: submit-check
description: Run pre-submission validation. Handles 5 formats — Kaggle CSV / Kaggle Code Competition / prediction-file zip / Docker container / Simulation agent. Delegates to the submission-validator agent for mechanical checks. Use right before any submission.
argument-hint: "<path to submit/v00X_<source-exp>_xxx> [--platform kaggle|gc|codabench]"
---

# Pre-submission Validation Skill

## Steps

1. **Parse arguments**:
   - Get submission folder path from `$ARGUMENTS` (required, e.g., `submit/v001_baseline`)
   - Use `--platform` flag if given, otherwise infer from `KAGGLE_DIRECTION.md`

2. **Detect format**:
   - From folder contents:
     - `notebook.py` present → (A) Kaggle CSV
     - `kernel-metadata.json` + `dataset-metadata.json` present → (A') Kaggle Code Competition
     - `predict.py` + `run.sh` present → (B) Prediction-file zip
     - `Dockerfile` + `process.py` present → (C) Docker container
     - `agent.py` / `main.py` / `submit_agent.py` (or similar agent entry) present and none of the above → (D) Simulation agent

3. **Run the deterministic common gate first**:
   - If a matching contract exists under `automation/contracts/`, run
     `PYTHONPATH=. python3 -m tools.competition_automation submission-check <contract> <artifact> [--sample <sample>] [--ledger automation/ledger.jsonl]`
     before invoking any LLM agent
   - If no contract exists, report "contract not registered" as Warning/Block; do not infer a PASS from the file shape
   - Run `fingerprint` and the ledger duplicate check; a repackaged artifact with the same SHA is not a new candidate. With `--ledger`, an existing SHA is a hard failure
   - A deterministic gate failure cannot be overridden by an agent narrative or a "completed" report
   - Also run `submission-plan`; when `upload_mode=manual`, hand off `READY_FOR_MANUAL_UPLOAD` to the user. This is a local PASS; never attempt an external LB submission

4. **Invoke submission-validator agent**:
   - Pass the detected format and submission folder path
   - Agent handles format-specific validation automatically
   - For (A'), have submission-validator run "1'. Kaggle Code Competition checks" (kernel-metadata / dataset-metadata / self-contained notebook validation)
   - For (D), have submission-validator run "3'. (D) Simulation agent checks" (self-contained / full-episode completion / timeout, illegal-move, stray-print validation)

5. **Aggregate results and report**:
   - Show Pass / Fail / Warning in that order
   - If any Fail, emphasize **do not submit**
   - List recommended fixes at the end
   - A missing remote score is not a Fail for a `manual` contract. Keep the artifact `ready` and record external state as `manual_pending`

6. **Prep `submit/SUBMISSIONS.md` entry**:
   - Extract metadata (experiment folder, model source path, fold definition, training data, key params, preprocessing, CV) from agent output
   - If no entry exists yet, propose a row to append (**do not auto-commit**)

## Notes

- **Never upload externally.** Actual uploads are done by the user manually
- **For non-Kaggle platforms, the agent must never attempt an LB/API submission.** Zindi, SIGNATE, grand-challenge, CodaBench, and custom sites are human-submission by default; do not auto-retry submission errors
- During validation, ensure model files / submission.csv / prediction zips are not removed from `.gitignore`
- For Docker validation, `bash test.sh` may take long — run with `run_in_background` if needed
