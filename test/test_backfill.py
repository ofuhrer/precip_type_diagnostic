from __future__ import annotations

import json
from pathlib import Path

import pytest

from precip_type_diag import backfill
from precip_type_diag.constants import INPUT_PARAM_IDS


def test_reanalysis_inventory_intersects_every_required_field(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_list(filter_expr, **kwargs):
        calls.append(filter_expr)
        dates = ["20100101", "20100102"]
        if f"param={INPUT_PARAM_IDS['T_G']}" in filter_expr:
            dates = ["20100101"]
        return {"date": dates}

    monkeypatch.setattr(backfill, "_fdb_utils_list", fake_list)

    assert backfill.list_available_reanalysis_dates(start_date="20100101", end_date="20100102") == {"20100101"}
    assert len(calls) == 6
    assert all("date=20100101/to/20100102" in call for call in calls)


def test_build_manifest_records_cycle_and_valid_time_semantics(tmp_path: Path) -> None:
    manifest_path = tmp_path / "campaign" / "manifest.json"
    manifest = backfill.build_backfill_manifest(
        start_date="20100101",
        end_date="20100102",
        output_root=tmp_path / "products",
        manifest_path=manifest_path,
        available_dates={"20100101", "20100102"},
    )

    assert manifest["cycle_date_range"] == {"start": "20100101", "end": "20100102", "inclusive": True}
    assert manifest["valid_time_coverage"]["first_interval_end"] == "2010-01-01T01:00:00+00:00"
    assert manifest["valid_time_coverage"]["last_interval_end"] == "2010-01-03T00:00:00+00:00"
    assert manifest["cycles"] == [{"index": 0, "date": "20100101"}, {"index": 1, "date": "20100102"}]
    assert json.loads(manifest_path.read_text())["max_step"] == 24


def test_build_manifest_is_strict_about_missing_inventory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing 1 requested cycle"):
        backfill.build_backfill_manifest(
            start_date="20100101",
            end_date="20100102",
            output_root=tmp_path / "products",
            manifest_path=tmp_path / "manifest.json",
            available_dates={"20100101"},
        )


def test_build_manifest_can_record_deliberately_missing_dates(tmp_path: Path) -> None:
    manifest = backfill.build_backfill_manifest(
        start_date="20100101",
        end_date="20100102",
        output_root=tmp_path / "products",
        manifest_path=tmp_path / "manifest.json",
        available_dates={"20100101"},
        allow_missing_dates=True,
    )

    assert manifest["inventory"]["missing_cycles"] == ["20100102"]
    assert manifest["cycles"] == [{"index": 0, "date": "20100101"}]


def test_run_manifest_task_skips_only_verified_day(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "campaign" / "manifest.json"
    backfill.build_backfill_manifest(
        start_date="20100101",
        end_date="20100101",
        output_root=tmp_path / "products",
        manifest_path=manifest_path,
        available_dates={"20100101"},
    )
    monkeypatch.setattr(backfill, "_verified_day_complete", lambda manifest, date: True)
    monkeypatch.setattr(backfill, "run_operational", lambda **kwargs: pytest.fail("unexpected processing"))

    receipt = backfill.run_manifest_task(manifest_path=manifest_path, index=0)

    assert receipt["status"] == "verified_existing"
    assert receipt["ok"] is True
    assert (manifest_path.parent / "receipts" / "00000-20100101.json").is_file()


def test_run_manifest_task_uses_full_daily_cycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "campaign" / "manifest.json"
    backfill.build_backfill_manifest(
        start_date="20100101",
        end_date="20100101",
        output_root=tmp_path / "products",
        manifest_path=manifest_path,
        available_dates={"20100101"},
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(backfill, "_verified_day_complete", lambda manifest, date: False)

    def fake_run(**kwargs):
        calls.append(kwargs)
        return {"monitoring": {"ok": True}}

    monkeypatch.setattr(backfill, "run_operational", fake_run)

    receipt = backfill.run_manifest_task(manifest_path=manifest_path, index=0)

    assert receipt["ok"] is True
    assert calls[0]["date"] == "20100101"
    assert calls[0]["time_value"] == "0000"
    assert calls[0]["start_step"] == 1
    assert calls[0]["max_step"] == 24
    assert calls[0]["output_format"] == "grib2"


def test_run_manifest_task_records_raised_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest_path = tmp_path / "campaign" / "manifest.json"
    backfill.build_backfill_manifest(
        start_date="20100101",
        end_date="20100101",
        output_root=tmp_path / "products",
        manifest_path=manifest_path,
        available_dates={"20100101"},
    )
    monkeypatch.setattr(backfill, "_verified_day_complete", lambda manifest, date: False)
    monkeypatch.setattr(backfill, "run_operational", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        backfill.run_manifest_task(manifest_path=manifest_path, index=0)

    receipt = json.loads((manifest_path.parent / "receipts" / "00000-20100101.json").read_text())
    assert receipt["status"] == "critical"
    assert receipt["attempt"] == 1
    assert receipt["error"] == "RuntimeError: boom"


def test_campaign_status_and_slurm_array(tmp_path: Path) -> None:
    manifest_path = tmp_path / "campaign" / "manifest.json"
    output_root = tmp_path / "products"
    backfill.build_backfill_manifest(
        start_date="20100101",
        end_date="20100102",
        output_root=output_root,
        manifest_path=manifest_path,
        available_dates={"20100101", "20100102"},
    )
    complete_dir = output_root / "ICON-REA-L-CH1" / "20100101" / "0000"
    complete_dir.mkdir(parents=True)
    (complete_dir / "DONE.json").write_text("{}")

    status = backfill.campaign_status(manifest_path=manifest_path)
    assert status["complete_cycles"] == 1
    assert status["pending_dates"] == ["20100102"]

    script = backfill.write_slurm_array_script(
        manifest_path=manifest_path,
        script_path=tmp_path / "campaign.sbatch",
        concurrency=2,
    )
    text = script.read_text()
    assert "#SBATCH --partition=pp-long" in text
    assert "#SBATCH --array=0-1%2" in text
    assert 'run_balfrin.sh backfill-task' in text
