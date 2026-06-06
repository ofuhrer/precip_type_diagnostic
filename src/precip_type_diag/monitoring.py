"""Machine-readable operational monitoring status for completed runs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

FATAL_DATA_QUALITY_KEYS = (
    "invalid_total_precip_columns",
    "invalid_active_ground_temperature_columns",
    "invalid_active_profile_columns",
)


def _alert(code: str, message: str, *, details: Mapping[str, object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": code,
        "severity": "critical",
        "message": message,
    }
    if details:
        payload["details"] = details
    return payload


def _step_token(step: int) -> str:
    days, hours = divmod(step, 24)
    return f"{days:02d}{hours:02d}0000"


def _missing_output_files(summary: dict[str, Any]) -> list[str]:
    output_root = Path(str(summary["output_root"]))
    model = str(summary["model"])
    date = str(summary["date"])
    time_value = str(summary["time"])
    start_step = int(summary.get("start_step", 0))
    max_step = int(summary["max_step"])
    missing: list[str] = []

    for member in summary.get("processed_members", []):
        for step in range(start_step, max_step + 1):
            path = output_root / model / date / time_value / str(member) / f"lfff{_step_token(step)}.ptype.grib2"
            if not path.exists():
                missing.append(str(path))
    return missing


def build_monitoring_status(
    summary: dict[str, Any],
    *,
    max_wall_s: float | None = None,
    check_output_files: bool = False,
) -> dict[str, object]:
    """Build deterministic alert status from an operational summary."""

    alerts: list[dict[str, object]] = []
    members = [str(member) for member in summary.get("members", [])]
    processed_members = [str(member) for member in summary.get("processed_members", [])]
    failed = summary.get("failed", {})
    failed_members = sorted(str(member) for member in failed) if isinstance(failed, dict) else []
    max_step = int(summary.get("max_step", -1))
    start_step = int(summary.get("start_step", 0))
    expected_steps = max_step - start_step + 1

    if failed_members:
        alerts.append(
            _alert(
                "failed_members",
                "One or more ensemble members failed.",
                details={"members": failed_members},
            )
        )

    accounted_members = set(processed_members) | set(failed_members)
    missing_member_results = [member for member in members if member not in accounted_members]
    if missing_member_results:
        alerts.append(
            _alert(
                "missing_member_results",
                "One or more requested members have no processed or failed result.",
                details={"members": missing_member_results},
            )
        )

    per_member = summary.get("per_member", {})
    if isinstance(per_member, dict):
        bad_steps: dict[str, dict[str, int]] = {}
        for member in processed_members:
            result = per_member.get(member)
            if not isinstance(result, dict):
                bad_steps[member] = {"steps": -1, "written": -1, "expected": expected_steps}
                continue
            steps = int(result.get("steps", -1))
            written = int(result.get("written", -1))
            if steps != expected_steps or written != expected_steps:
                bad_steps[member] = {
                    "steps": steps,
                    "written": written,
                    "expected": expected_steps,
                }
        if bad_steps:
            alerts.append(
                _alert(
                    "incomplete_member_outputs",
                    "One or more processed members have incomplete step or output counts.",
                    details={"members": bad_steps},
                )
            )

    data_quality = summary.get("data_quality", {})
    if isinstance(data_quality, dict):
        fatal_quality = {key: int(data_quality.get(key, 0)) for key in FATAL_DATA_QUALITY_KEYS}
        nonzero_quality = {key: value for key, value in fatal_quality.items() if value}
        if nonzero_quality:
            alerts.append(
                _alert(
                    "fatal_data_quality",
                    "Operationally fatal data-quality counters are non-zero.",
                    details=nonzero_quality,
                )
            )

    wall_s = float(summary.get("wall_s", 0.0))
    if max_wall_s is not None and wall_s > max_wall_s:
        alerts.append(
            _alert(
                "wall_clock_exceeded",
                "Run wall-clock time exceeded the configured monitoring limit.",
                details={"wall_s": round(wall_s, 3), "max_wall_s": round(float(max_wall_s), 3)},
            )
        )

    probability_products = summary.get("probabilistic_products", {})
    if isinstance(probability_products, dict) and probability_products.get("enabled") is True:
        probability_status = str(probability_products.get("status", ""))
        if probability_status != "ok":
            probability_details: dict[str, object] = {"status": probability_status}
            error = probability_products.get("error")
            if error:
                probability_details["error"] = str(error)
            missing_members = probability_products.get("missing_members")
            if missing_members:
                probability_details["missing_members"] = missing_members
            alerts.append(
                _alert(
                    "probability_products_failed",
                    "Requested ensemble probability products were not generated successfully.",
                    details=probability_details,
                )
            )

    missing_files: list[str] = []
    if check_output_files and processed_members:
        missing_files = _missing_output_files(summary)
        if missing_files:
            preview = missing_files[:10]
            details: dict[str, object] = {
                "count": len(missing_files),
                "preview": preview,
            }
            if len(missing_files) > len(preview):
                details["truncated"] = True
            alerts.append(
                _alert(
                    "missing_output_files",
                    "One or more expected output GRIB files are missing on disk.",
                    details=details,
                )
            )

    status = "critical" if alerts else "ok"
    return {
        "status": status,
        "ok": not alerts,
        "recommended_exit_code": 1 if alerts else 0,
        "alerts": alerts,
        "expected": {
            "members": len(members),
            "steps_per_member": expected_steps,
            "output_files": len(processed_members) * expected_steps,
        },
        "observed": {
            "processed_members": len(processed_members),
            "failed_members": len(failed_members),
            "wall_s": round(wall_s, 3),
            "checked_output_files": bool(check_output_files),
            "missing_output_files": len(missing_files),
        },
    }
