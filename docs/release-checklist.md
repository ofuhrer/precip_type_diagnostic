# Release Checklist

Complete this record for every candidate and accepted operational tag. Commands
and interpretation are documented in the [README](../README.md) and
[release guide](release-and-operations.md).

## Candidate

- Version / tag:
- Commit:
- Clean worktree: yes / no (attach approved diff if no)
- Python version:
- FDB image and `uenv` version:
- Tested views: `realtime` / `rea-l-ch1`
- ecCodes definition source:
- Release owner:
- Scientific approver:
- Operational approver:

## Automated Gates

```bash
python -m pip install -e ".[test,dev]"
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
git diff --check
```

- Local gate result:
- GitHub Actions `tests` result:
- Wheel contents checked, if packaging changed:

For ICON science changes:

```bash
PYTHONPATH=src python tools/verify_icon_fortran.py --icon-repo /path/to/icon-nwp
```

- ICON checkout commit:
- Fortran source SHA-256:
- Parity result:

## Balfrin Evidence

Run the README smoke commands from the candidate revision. Use `pp-short` for
manual jobs that fit its limit.

| Source | Algorithm | Command/log | `summary.json` | `monitoring.json` | Output inspection |
| --- | --- | --- | --- | --- | --- |
| CH1 realtime | Firdewsa | | | | |
| CH2 realtime | Firdewsa | | | | |
| REA-L-CH1 | Firdewsa | | | | |
| CH1 realtime | ICON, when required | | | | |
| CH2 realtime | ICON, when required | | | | |
| REA-L-CH1 | ICON, when required | | | | |

Required for each executed row:

- process exit is zero and `monitoring.json["ok"]` is true;
- `DONE.json` exists; `RUNNING.json` and `FAILED.json` do not;
- source, cycle, algorithm, requested members/steps, and fidelity are correct;
- at least one output has the expected `PTYPE` metadata, shape, step, and codes.

For REA, confirm view `rea-l-ch1`, cycle `0000`, and the daily-through-step-24
accumulation contract. For ICON mode, confirm all three archived grid-scale
microphysics fields passed completeness checks.

## Production-Path Smoke

For realtime releases, exercise the DEPL wrapper with a small override:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS YYYYMMDD HH \
  /users/$USER/work/ptype-fdb-depl-smoke --members 000 --max-step 1
```

- Wrapper command and JSON log:
- Summary / monitoring / final marker:
- Probability NetCDF for the tested step:

## Decision and Rollback

- Release decision and date:
- Accepted tag:
- Previous accepted tag:
- Previous runtime record:
- Product publication boundary:
- Rollback command/location:

Tag only after acceptance:

```bash
git tag -a vX.Y.Z -m "precip_type_diag vX.Y.Z"
git push origin vX.Y.Z
```
