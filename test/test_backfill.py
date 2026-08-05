from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from precip_type_diag import backfill
from precip_type_diag.constants import INPUT_PARAM_IDS, OUTPUT_PARAM_ID, OUTPUT_SHORT_NAME
from precip_type_diag.probabilities import member_grib_path


def _build_manifest(
    tmp_path: Path,
    *,
    start_date: str = "20100101",
    end_date: str = "20100101",
    available_dates: set[str] | None = None,
) -> tuple[Path, dict[str, object]]:
    manifest_path = tmp_path / "campaign" / "manifest.json"
    manifest = backfill.build_backfill_manifest(
        start_date=start_date,
        end_date=end_date,
        output_root=tmp_path / "products",
        staging_root=tmp_path / "staging",
        manifest_path=manifest_path,
        available_dates=available_dates or set(backfill._date_range(start_date, end_date)),
    )
    return manifest_path, manifest


def _archive_metadata(dates: list[str]) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    for date in dates:
        cycle = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc)
        for step in range(1, 25):
            valid = cycle + timedelta(hours=step)
            messages.append(
                {
                    "shortName": OUTPUT_SHORT_NAME,
                    "paramId": OUTPUT_PARAM_ID,
                    "dataDate": int(date),
                    "dataTime": 0,
                    "endStep": step,
                    "validityDate": int(valid.strftime("%Y%m%d")),
                    "validityTime": int(valid.strftime("%H%M")),
                }
            )
    return messages


def _write_complete_receipt(manifest_path: Path, manifest: dict[str, object], period: dict[str, object]) -> None:
    archive = backfill._archive_path(manifest, period)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archive")
    backfill._atomic_write_json(
        {
            "ok": True,
            "status": "complete",
            "archive_path": str(archive),
            "size_bytes": archive.stat().st_size,
            "message_count": period["message_count"],
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        },
        backfill._receipt_path(manifest_path, period),
    )


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


def test_build_manifest_groups_cycles_into_monthly_archives(tmp_path: Path) -> None:
    manifest_path, manifest = _build_manifest(tmp_path, start_date="20100131", end_date="20100202")

    assert manifest["schema_version"] == 2
    assert manifest["mode"] == "rea_l_ch1_monthly_backfill"
    assert manifest["cycle_date_range"] == {"start": "20100131", "end": "20100202", "inclusive": True}
    assert manifest["valid_time_coverage"]["first_interval_end"] == "2010-01-31T01:00:00+00:00"
    assert manifest["valid_time_coverage"]["last_interval_end"] == "2010-02-03T00:00:00+00:00"
    assert manifest["periods"] == [
        {
            "index": 0,
            "period": "201001",
            "dates": ["20100131"],
            "message_count": 24,
            "archive": "ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201001.grib2",
        },
        {
            "index": 1,
            "period": "201002",
            "dates": ["20100201", "20100202"],
            "message_count": 48,
            "archive": "ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201002.grib2",
        },
    ]
    assert manifest["file_count_projection"]["estimated_persistent_files"] == 12
    assert json.loads(manifest_path.read_text())["messages_per_cycle"] == 24


def test_full_archive_manifest_projects_248_monthly_files(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    dates = set(backfill._date_range("20050101", "20250831"))
    manifest = backfill.build_backfill_manifest(
        start_date="20050101",
        end_date="20250831",
        output_root=tmp_path / "products",
        staging_root=tmp_path / "staging",
        manifest_path=manifest_path,
        available_dates=dates,
    )

    assert len(manifest["periods"]) == 248
    assert manifest["file_count_projection"] == {
        "archive_files": 248,
        "archive_contract_files": 1,
        "estimated_archive_root_files": 249,
        "monthly_receipts": 248,
        "monthly_locks": 248,
        "slurm_logs": 248,
        "campaign_control_files": 3,
        "estimated_campaign_root_files": 747,
        "estimated_persistent_files": 996,
        "single_message_grib_files_avoided": 181152,
    }


def test_build_manifest_is_strict_about_missing_inventory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing 1 requested cycle"):
        backfill.build_backfill_manifest(
            start_date="20100101",
            end_date="20100102",
            output_root=tmp_path / "products",
            staging_root=tmp_path / "staging",
            manifest_path=tmp_path / "manifest.json",
            available_dates={"20100101"},
        )


def test_build_manifest_can_record_deliberately_missing_dates(tmp_path: Path) -> None:
    manifest = backfill.build_backfill_manifest(
        start_date="20100101",
        end_date="20100102",
        output_root=tmp_path / "products",
        staging_root=tmp_path / "staging",
        manifest_path=tmp_path / "manifest.json",
        available_dates={"20100101"},
        allow_missing_dates=True,
    )

    assert manifest["inventory"]["missing_cycles"] == ["20100102"]
    assert manifest["periods"][0]["dates"] == ["20100101"]


def test_build_manifest_rejects_overlapping_staging_and_output_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        backfill.build_backfill_manifest(
            start_date="20100101",
            end_date="20100101",
            output_root=tmp_path / "products",
            staging_root=tmp_path / "products" / "staging",
            manifest_path=tmp_path / "manifest.json",
            available_dates={"20100101"},
        )


def test_schema_v1_daily_manifest_is_rejected_explicitly(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": 1, "mode": "rea_l_ch1_daily_backfill"}))

    with pytest.raises(RuntimeError, match="Unsupported backfill manifest"):
        backfill.load_manifest(manifest_path)


def test_inspect_monthly_archive_enforces_message_order_and_validity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "month.grib2"
    archive.write_bytes(b"GRIB7777")
    period = {"period": "201001", "dates": ["20100131"], "message_count": 24}
    metadata = _archive_metadata(["20100131"])
    monkeypatch.setattr(backfill, "read_grib_archive_metadata", lambda path, keys: metadata)

    info = backfill._inspect_monthly_archive(archive, period)
    assert info["message_count"] == 24
    assert info["last_message"]["validityDate"] == 20100201
    assert info["last_message"]["validityTime"] == 0

    metadata[1]["endStep"] = 3
    with pytest.raises(RuntimeError, match="message 2 metadata mismatch"):
        backfill._inspect_monthly_archive(archive, period)


def test_run_manifest_task_skips_only_verified_month(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest_path, manifest = _build_manifest(tmp_path)
    period = manifest["periods"][0]
    archive = backfill._archive_path(manifest, period)
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"monthly")
    stale_partial = archive.with_name(f".{archive.name}.partial")
    stale_partial.write_bytes(b"stale")
    monkeypatch.setattr(
        backfill,
        "_inspect_monthly_archive",
        lambda path, value: {"archive_path": str(path), "size_bytes": path.stat().st_size, "message_count": 24},
    )
    monkeypatch.setattr(backfill, "run_operational", lambda **kwargs: pytest.fail("unexpected processing"))

    receipt = backfill.run_manifest_task(manifest_path=manifest_path, index=0)

    assert receipt["status"] == "verified_existing"
    assert receipt["sha256"] == hashlib.sha256(b"monthly").hexdigest()
    assert (manifest_path.parent / "receipts" / "00000-201001.json").is_file()
    assert not stale_partial.exists()


def test_run_manifest_task_processes_daily_cycles_and_publishes_one_month(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, manifest = _build_manifest(tmp_path, start_date="20100101", end_date="20100102")
    calls: list[dict[str, object]] = []
    stale_staging = Path(manifest["staging_root"]) / f"{backfill._staging_prefix(manifest_path, 0, '201001')}stale"
    stale_staging.mkdir(parents=True)
    (stale_staging / "partial").write_text("stale")

    def fake_run(**kwargs):
        if calls:
            previous_date = str(calls[-1]["date"])
            assert not (Path(kwargs["output_root"]) / backfill.REA_MODEL / previous_date).exists()
        calls.append(kwargs)
        date = str(kwargs["date"])
        root = Path(kwargs["output_root"])
        for step in range(1, 25):
            output = member_grib_path(root, backfill.REA_MODEL, date, "0000", "000", step)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(f"{date}:{step:02d}\n".encode())
        return {
            "monitoring": {"ok": True},
            "wall_s": 1.5,
            "data_quality": {"active_columns": 10},
            "retry_stats": {"retry_attempts": 0},
            "algorithm_fidelity": {"status": "firdewsa_reference"},
            "provenance": {"git": {"revision": "abc123"}},
        }

    monkeypatch.setattr(backfill, "run_operational", fake_run)
    monkeypatch.setattr(backfill, "_verified_day_complete", lambda **kwargs: True)
    monkeypatch.setattr(
        backfill,
        "_inspect_monthly_archive",
        lambda path, period: {
            "archive_path": str(path),
            "size_bytes": path.stat().st_size,
            "message_count": period["message_count"],
        },
    )

    receipt = backfill.run_manifest_task(manifest_path=manifest_path, index=0)

    archive = backfill._archive_path(manifest, manifest["periods"][0])
    expected = b"".join(f"{date}:{step:02d}\n".encode() for date in ("20100101", "20100102") for step in range(1, 25))
    assert archive.read_bytes() == expected
    assert receipt["ok"] is True
    assert receipt["message_count"] == 48
    assert receipt["sha256"] == hashlib.sha256(expected).hexdigest()
    assert receipt["diagnostic_algorithm"] == "firdewsa"
    assert receipt["algorithm_fidelity"] == {"status": "firdewsa_reference"}
    assert receipt["provenance"] == {"git": {"revision": "abc123"}}
    assert [call["date"] for call in calls] == ["20100101", "20100102"]
    assert all(call["time_value"] == "0000" for call in calls)
    assert all(call["start_step"] == 1 and call["max_step"] == 24 for call in calls)
    assert all(call["output_format"] == "grib2" for call in calls)
    assert all(Path(call["output_root"]).is_relative_to(Path(manifest["staging_root"])) for call in calls)
    assert not (archive.parent / f".{archive.name}.partial").exists()
    assert not stale_staging.exists()


def test_run_manifest_task_failure_keeps_final_archive_unpublished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path, manifest = _build_manifest(tmp_path)
    monkeypatch.setattr(backfill, "run_operational", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        backfill.run_manifest_task(manifest_path=manifest_path, index=0)

    period = manifest["periods"][0]
    archive = backfill._archive_path(manifest, period)
    receipt = json.loads(backfill._receipt_path(manifest_path, period).read_text())
    assert receipt["status"] == "critical"
    assert receipt["attempt"] == 1
    assert receipt["error"] == "RuntimeError: boom"
    assert not archive.exists()
    assert not (archive.parent / f".{archive.name}.partial").exists()


def test_campaign_status_and_slurm_array_are_monthly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest_path, manifest = _build_manifest(tmp_path, start_date="20100131", end_date="20100201")
    periods = manifest["periods"]
    _write_complete_receipt(manifest_path, manifest, periods[0])

    status = backfill.campaign_status(manifest_path=manifest_path)
    assert status["total_periods"] == 2
    assert status["complete_periods"] == 1
    assert status["complete_cycles"] == 1
    assert status["pending_period_names"] == ["201002"]
    assert status["pending_dates"] == ["20100201"]

    monkeypatch.setattr(backfill, "_inspect_monthly_archive", lambda path, period: {})
    verified = backfill.campaign_status(manifest_path=manifest_path, verify_outputs=True)
    assert verified["complete_periods"] == 1
    assert verified["verified_outputs"] is True

    backfill._archive_path(manifest, periods[0]).write_bytes(b"corrupt")
    corrupted = backfill.campaign_status(manifest_path=manifest_path, verify_outputs=True)
    assert corrupted["complete_periods"] == 0
    assert corrupted["pending_period_names"] == ["201001", "201002"]

    script = backfill.write_slurm_array_script(
        manifest_path=manifest_path,
        script_path=tmp_path / "campaign.sbatch",
        concurrency=2,
    )
    text = script.read_text()
    assert "#SBATCH --partition=pp-long" in text
    assert "#SBATCH --array=0-1%2" in text
    assert "#SBATCH --output=" in text
    assert "%A_%a.out" in text
    assert "run_balfrin.sh backfill-task" in text


def test_archive_contract_prevents_algorithm_mixing(tmp_path: Path) -> None:
    _, manifest = _build_manifest(tmp_path)
    backfill._ensure_archive_contract(manifest)
    manifest["diagnostic_algorithm"] = "icon"

    with pytest.raises(RuntimeError, match="Archive contract mismatch"):
        backfill._ensure_archive_contract(manifest)
