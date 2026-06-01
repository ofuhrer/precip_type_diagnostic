from __future__ import annotations

from pathlib import Path

from precip_type_diag.monitoring import build_monitoring_status


def _summary(tmp_path: Path) -> dict[str, object]:
    return {
        "model": "ICON-CH2-EPS",
        "date": "20260531",
        "time": "1800",
        "members": ["000"],
        "processed_members": ["000"],
        "failed": {},
        "max_step": 1,
        "output_root": str(tmp_path),
        "data_quality": {
            "invalid_total_precip_columns": 0,
            "invalid_active_ground_temperature_columns": 0,
            "invalid_active_profile_columns": 0,
        },
        "wall_s": 10.0,
        "per_member": {
            "000": {
                "steps": 2,
                "written": 2,
            }
        },
    }


def test_monitoring_status_ok_for_complete_summary(tmp_path: Path) -> None:
    summary = _summary(tmp_path)

    status = build_monitoring_status(summary)

    assert status["ok"] is True
    assert status["status"] == "ok"
    assert status["alerts"] == []
    assert status["recommended_exit_code"] == 0


def test_monitoring_status_reports_operational_alerts(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    summary["failed"] = {"001": "RuntimeError: bad member"}
    summary["members"] = ["000", "001", "002"]
    summary["data_quality"] = {
        "invalid_total_precip_columns": 3,
        "invalid_active_ground_temperature_columns": 0,
        "invalid_active_profile_columns": 2,
    }
    summary["per_member"] = {"000": {"steps": 2, "written": 1}}

    status = build_monitoring_status(summary, max_wall_s=5.0)

    assert status["ok"] is False
    assert status["status"] == "critical"
    assert status["recommended_exit_code"] == 1
    codes = {alert["code"] for alert in status["alerts"]}
    assert codes == {
        "failed_members",
        "missing_member_results",
        "incomplete_member_outputs",
        "fatal_data_quality",
        "wall_clock_exceeded",
    }


def test_monitoring_status_can_check_output_files(tmp_path: Path) -> None:
    summary = _summary(tmp_path)
    output = tmp_path / "ICON-CH2-EPS" / "20260531" / "1800" / "000"
    output.mkdir(parents=True)
    (output / "lfff00000000.ptype.grib2").write_bytes(b"grib")

    status = build_monitoring_status(summary, check_output_files=True)

    assert status["ok"] is False
    assert status["observed"]["missing_output_files"] == 1
    assert status["alerts"][0]["code"] == "missing_output_files"
