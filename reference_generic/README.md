# Generic reference

This directory is a request-first scaffold. Its default configuration does not select a model, data, metric, or training budget. Create an Issue-bound runtime request and execute it through `experiment-run`; do not call a training script directly from an experiment launcher.

```bash
PYTHONPATH=. python3 -m tools.competition_automation experiment-run \
  automation/requests/<experiment>.json --root . --registry automation/runs.sqlite
```

For integration tests only, `run.py --demo` enables a tiny deterministic linear-regression demonstration after explicitly supplying a CSV, target, features, optimizer, learning rate, steps, and checkpoint interval. The demo reports `train_mse` with `evaluation_scope=train_only`; it is not a competition baseline or a competition metric. Resume rejects data, feature, optimizer, and learning-rate changes and can be used to verify exact partial/full continuation.
