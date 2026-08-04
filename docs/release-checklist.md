# Release Checklist

Use this checklist for release candidates and accepted operational tags.

## Candidate Metadata

- Release candidate:
- Git commit:
- Git tag:
- Package version:
- Python version:
- FDB `uenv` image and tested views:
- ecCodes definition source:
- Release owner:
- Scientific approver:
- Operational approver:

## Local Gates

Run from a clean worktree:

```bash
python -m pip install -e ".[test,dev]"
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
```

Expected result: all commands pass. The pytest command enforces the configured
coverage threshold.

For changes to the ICON-adapted path, also record an executable comparison with
the pinned upstream Fortran source:

```bash
PYTHONPATH=src python tools/verify_icon_fortran.py --icon-repo /path/to/icon-nwp
```

- ICON checkout commit:
- Fortran source SHA-256:
- Harness result:

## Balfrin Smoke Tests

Run the smoke test once for each operational model. This loop keeps the output
directories separate:

If the smoke test is submitted through SLURM, use the generally open `pp-short`
partition and keep the requested wall time below one hour. Do not use restricted
elevated-rights partitions such as `pp-production`, `pp-prodntc`, or
`pp-dispntc` for release-candidate smoke tests.

```bash
for model in ICON-CH1-EPS ICON-CH2-EPS; do
  /usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
    env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
    .venv-fdb-5.21/bin/python -m precip_type_diag \
      --model "$model" \
      --members 000 \
      --max-step 1 \
      --max-wall-s 900 \
      --output-root "/users/$USER/work/ptype-fdb-smoke/$model"
done
```

Record:

- CH1 command output:
- CH1 `summary.json`:
- CH1 `monitoring.json`:
- CH2 command output:
- CH2 `summary.json`:
- CH2 `monitoring.json`:

Required result: `monitoring.json["ok"]` is `true`; at least one member output
file is re-read and checked for `PTYPE` metadata/variable shape and allowed
category codes.

For dual-mode science or FDB changes, repeat the loop with `--algorithm icon`
and record `summary.json["algorithm_fidelity"]` for both models.

Run one archived REA-L-CH1 day in its separate view:

```bash
/usr/bin/uenv run --view=rea-l-ch1 fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
    --model ICON-REA-L-CH1 \
    --date 20100101 \
    --time 0000 \
    --members 000 \
    --max-step 1 \
    --max-wall-s 900 \
    --output-root /users/$USER/work/ptype-fdb-smoke/ICON-REA-L-CH1
```

- REA-L-CH1 command output:
- REA-L-CH1 `summary.json` and `fdb_source`:
- REA-L-CH1 `monitoring.json`:

Repeat with `--algorithm icon` for dual-mode science or FDB changes. Confirm
the source contract is `rea-l-ch1`, the cycle is `0000`, and the accumulation
contract is daily through step 24.

## DEPL-Style Production Smoke

Run one explicit-cycle smoke with the wrapper used by DEPL:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS YYYYMMDD HH /users/$USER/work/ptype-fdb-depl-smoke --members 000 --max-step 1
```

The final two options deliberately override the wrapper's all-member,
full-forecast defaults to keep this smoke test small.

If submitted through SLURM, use the generally open `pp-short` partition unless
the expected runtime requires a longer generally open queue.

Record:

- Wrapper command:
- JSON log location:
- `summary.json`:
- `monitoring.json`:
- `DONE.json` or `FAILED.json`:

Required result: JSON logs are parseable, `RUNNING.json` is removed at the end,
`DONE.json` exists, `FAILED.json` does not exist, `monitoring.json["ok"]` is
`true`, and the probability NetCDF output for the tested step exists.

## Tagging

Tag only after the gates above pass:

```bash
git tag -a vX.Y.Z -m "precip_type_diag vX.Y.Z"
git push origin vX.Y.Z
```

Do not tag accepted operational releases from a dirty worktree unless the exact
diff is archived with the acceptance record.

## Rollback

- Previous accepted tag:
- Previous dependency/uenv record:
- Product publication boundary:
- Rollback command/location:
