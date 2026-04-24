# precip_type_diag

`precip_type_diag` is an ICON precipitation-type diagnostic for `ICON-CH1-EPS` and
`ICON-CH2-EPS`. It implements the MeteoSwiss MSc-thesis method by Firdewsa
Zukanovic, based on the Modified Bourgouin precipitation-type algorithm described
by Birk et al. (2021).

## Installation

```bash
python -m pip install .
```

This installs the package together with the required `numba`, `eccodes`, and
MeteoSwiss ecCodes definition resources.

## Running

Process a single member/hour from one ICON run:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-run /path/to/run/icon \
  --output-dir /path/to/output \
  --model ICON-CH2-EPS \
  --members 000 \
  --hours 04180000
```

Process one full operational run:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-root /opr/osm/inn/cache \
  --output-root /path/to/output \
  --model ICON-CH2-EPS \
  --run latest
```
