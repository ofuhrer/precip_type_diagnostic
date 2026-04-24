from __future__ import annotations

import json
from pathlib import Path

import pytest

from precip_type_diag.__main__ import main


def test_report_only_prints_run_report_without_output_dir(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    report = {"members": ["000"], "hours_by_member": {"000": ["00000000"]}}
    calls: list[Path] = []

    def fake_report_for_run(input_run: Path) -> dict[str, object]:
        calls.append(input_run)
        return report

    monkeypatch.setattr("precip_type_diag.__main__.report_for_run", fake_report_for_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--input-run",
            "/tmp/icon",
            "--model",
            "ICON-CH2-EPS",
            "--report-only",
        ],
    )

    assert main() == 0
    assert calls == [Path("/tmp/icon")]
    assert json.loads(capsys.readouterr().out) == report


def test_debug_mode_requires_output_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--input-run",
            "/tmp/icon",
            "--model",
            "ICON-CH2-EPS",
            "--members",
            "000",
            "--hours",
            "00000000",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2


def test_operational_mode_passes_cli_options(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_operational(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("precip_type_diag.__main__.run_operational", fake_run_operational)
    monkeypatch.setattr(
        "sys.argv",
        [
            "precip_type_diag",
            "--input-root",
            "/cache",
            "--output-root",
            "/products",
            "--model",
            "ICON-CH1-EPS",
            "--run",
            "26042315_639",
            "--summary-json",
            "/tmp/summary.json",
            "--overwrite",
            "--precip-mask-threshold-mm",
            "0.25",
        ],
    )

    assert main() == 0

    assert calls == [
        {
            "model": "ICON-CH1-EPS",
            "run": "26042315_639",
            "input_root": Path("/cache"),
            "output_root": Path("/products"),
            "precip_mask_threshold_mm": 0.25,
            "overwrite": True,
            "summary_json": Path("/tmp/summary.json"),
        }
    ]
    assert json.loads(capsys.readouterr().out) == {"ok": True}
