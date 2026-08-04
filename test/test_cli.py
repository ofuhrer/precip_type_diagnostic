from __future__ import annotations

import json
from pathlib import Path

import pytest

from precip_type_diag.__main__ import main


def test_cli_passes_fdb_options(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_operational(**kwargs):
        calls.append(kwargs)
        return {"failed": {}, "ok": True}

    monkeypatch.setattr("precip_type_diag.__main__.run_operational", fake_run_operational)
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--model",
            "ICON-CH1-EPS",
            "--algorithm",
            "icon",
            "--output-root",
            "/products",
            "--members",
            "000,001",
            "--date",
            "20260531",
            "--time",
            "1800",
            "--start-step",
            "1",
            "--max-step",
            "3",
            "--lookback-days",
            "1",
            "--chunk-size",
            "2",
            "--workers",
            "4",
            "--summary-json",
            "/tmp/summary.json",
            "--monitoring-json",
            "/tmp/monitoring.json",
            "--output-format",
            "NetCDF",
            "--run-id",
            "run-123",
            "--event-id",
            "event-456",
            "--attempt",
            "2",
            "--log-level",
            "INFO",
            "--log-format",
            "json",
            "--log-file",
            "/tmp/ptype.log",
            "--fdb-retries",
            "3",
            "--fdb-retry-initial-s",
            "1.5",
            "--fdb-retry-max-s",
            "9",
            "--max-wall-s",
            "900",
            "--no-output-file-check",
            "--write-probability-products",
            "--no-prefetch",
            "--skip-input-checks",
            "--precip-mask-threshold-mm",
            "0.25",
        ],
    )

    assert main() == 0
    assert calls == [
        {
            "model": "ICON-CH1-EPS",
            "algorithm": "icon",
            "output_root": Path("/products"),
            "members": ("000", "001"),
            "date": "20260531",
            "time_value": "1800",
            "start_step": 1,
            "max_step": 3,
            "lookback_days": 1,
            "chunk_size": 2,
            "workers": 4,
            "prefetch": False,
            "check_inputs": False,
            "precip_mask_threshold_mm": 0.25,
            "vertical_cutoff_m": 12000.0,
            "summary_json": Path("/tmp/summary.json"),
            "monitoring_json": Path("/tmp/monitoring.json"),
            "output_format": "netcdf",
            "run_id": "run-123",
            "event_id": "event-456",
            "attempt": 2,
            "fdb_retries": 3,
            "fdb_retry_initial_s": 1.5,
            "fdb_retry_max_s": 9.0,
            "max_wall_s": 900.0,
            "check_output_files": False,
            "write_probability_products": True,
        }
    ]
    assert json.loads(capsys.readouterr().out) == {"failed": {}, "ok": True}


def test_cli_rejects_probability_products_with_grib_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--model",
            "ICON-CH2-EPS",
            "--output-root",
            str(tmp_path),
            "--write-probability-products",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2


def test_cli_requires_date_and_time_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--model",
            "ICON-CH2-EPS",
            "--output-root",
            "/products",
            "--date",
            "20260531",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2


def test_cli_requires_explicit_00_utc_reanalysis_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["precip_type_diag", "--model", "ICON-REA-L-CH1", "--output-root", "/products"],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2

    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--model",
            "ICON-REA-L-CH1",
            "--output-root",
            "/products",
            "--date",
            "20100101",
            "--time",
            "0100",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2


def test_cli_rejects_invalid_fixed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--model",
            "ICON-CH2-EPS",
            "--output-root",
            "/products",
            "--date",
            "../escape",
            "--time",
            "1800",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2


def test_cli_rejects_duplicate_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--model",
            "ICON-CH2-EPS",
            "--output-root",
            "/products",
            "--members",
            "000,000",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2


def test_cli_returns_failure_when_any_member_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "precip_type_diag.__main__.run_operational",
        lambda **kwargs: {
            "failed": {"001": "boom"},
            "monitoring": {"recommended_exit_code": 1},
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--model",
            "ICON-CH2-EPS",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert main() == 1


def test_cli_rejects_non_positive_monitoring_wall_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--model",
            "ICON-CH2-EPS",
            "--output-root",
            str(tmp_path),
            "--max-wall-s",
            "0",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
