from __future__ import annotations

import json
from pathlib import Path

import pytest

from precip_type_diag import realtime
from precip_type_diag.constants import INPUT_PARAM_IDS


def test_available_contiguous_step_stops_at_first_incomplete_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    def listed_values(**kwargs):
        param = kwargs["param"]
        if param == INPUT_PARAM_IDS["HHL"]:
            return {"step": ["0"], "levelist": list(range(1, 82)), "timespan": ["none"]}
        steps = ["0", "1", "2", "3", "4"]
        if param == INPUT_PARAM_IDS["T_G"]:
            steps = ["1", "2"]
        payload = {"step": steps, "timespan": ["none", "fs"]}
        if param in (INPUT_PARAM_IDS["T"], INPUT_PARAM_IDS["P"], INPUT_PARAM_IDS["QV"]):
            payload["levelist"] = list(range(1, 81))
        return payload

    monkeypatch.setattr(realtime, "_listed_values", listed_values)

    assert realtime.available_contiguous_step(
        model="ICON-CH1-EPS",
        date="20260804",
        time_value="1800",
    ) == 2


def test_available_contiguous_icon_step_requires_microphysics(monkeypatch: pytest.MonkeyPatch) -> None:
    def listed_values(**kwargs):
        param = kwargs["param"]
        if param == INPUT_PARAM_IDS["HHL"]:
            return {"step": ["0"], "levelist": list(range(1, 82))}
        steps = ["0", "1", "2"]
        if param == INPUT_PARAM_IDS["GRAU_GSP"]:
            steps = ["0", "1"]
        payload = {"step": steps}
        if param in (INPUT_PARAM_IDS["T"], INPUT_PARAM_IDS["P"], INPUT_PARAM_IDS["QV"]):
            payload["levelist"] = list(range(1, 81))
        return payload

    monkeypatch.setattr(realtime, "_listed_values", listed_values)

    assert realtime.available_contiguous_step(
        model="ICON-CH1-EPS",
        date="20260804",
        time_value="1800",
        algorithm="icon",
    ) == 1


def test_available_contiguous_step_waits_for_the_slowest_member(monkeypatch: pytest.MonkeyPatch) -> None:
    def listed_values(**kwargs):
        param = kwargs["param"]
        member = kwargs["member"]
        if param == INPUT_PARAM_IDS["HHL"]:
            return {"step": ["0"], "levelist": list(range(1, 82))}
        steps = ["0", "1", "2", "3"] if member != "010" else ["0", "1"]
        payload = {"step": steps}
        if param in (INPUT_PARAM_IDS["T"], INPUT_PARAM_IDS["P"], INPUT_PARAM_IDS["QV"]):
            payload["levelist"] = list(range(1, 81))
        return payload

    monkeypatch.setattr(realtime, "_listed_values", listed_values)

    assert realtime.available_contiguous_step(
        model="ICON-CH1-EPS",
        date="20260804",
        time_value="1800",
    ) == 1


def test_discover_latest_candidate_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_list(expr, **kwargs):
        if "date=20260804" in expr:
            return {"time": ["0000", "0300"]}
        return {"time": ["2100"]}

    class FixedDateTime(realtime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 4, tzinfo=tz)

    monkeypatch.setattr(realtime, "datetime", FixedDateTime)
    monkeypatch.setattr(realtime, "_fdb_utils_list", fake_list)

    assert realtime.discover_latest_candidate_cycle(model="ICON-CH1-EPS", lookback_days=1) == ("20260804", "0300")


def test_progressive_cycle_processes_only_new_range(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(realtime, "_state_completed_through", lambda *args, **kwargs: 1)
    monkeypatch.setattr(realtime, "available_contiguous_step", lambda **kwargs: 3)

    def fake_run_operational(**kwargs):
        calls.append(kwargs)
        return {"monitoring": {"ok": True, "status": "ok", "recommended_exit_code": 0}}

    monkeypatch.setattr(realtime, "run_operational", fake_run_operational)

    state = realtime.run_progressive_cycle(
        model="ICON-CH1-EPS",
        output_root=tmp_path,
        date="20260804",
        time_value="1800",
    )

    assert state["completed_through"] == 3
    assert state["status"] == "ingesting"
    assert calls[0]["start_step"] == 2
    assert calls[0]["max_step"] == 3
    assert calls[0]["members"] == tuple(f"{member:03d}" for member in range(11))
    saved = json.loads((tmp_path / "ICON-CH1-EPS" / "20260804" / "1800" / "CYCLE.json").read_text())
    assert saved["increments"][0]["start_step"] == 2


def test_progressive_cycle_can_bound_a_controlled_increment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(realtime, "_state_completed_through", lambda *args, **kwargs: 1)
    monkeypatch.setattr(realtime, "available_contiguous_step", lambda **kwargs: 8)
    monkeypatch.setattr(
        realtime,
        "run_operational",
        lambda **kwargs: calls.append(kwargs) or {"monitoring": {"ok": True}},
    )

    state = realtime.run_progressive_cycle(
        model="ICON-CH1-EPS",
        output_root=tmp_path,
        date="20260804",
        time_value="1800",
        through_step=3,
    )

    assert state["discovered_available_through"] == 8
    assert state["available_through"] == 3
    assert state["completed_through"] == 3
    assert calls[0]["start_step"] == 2
    assert calls[0]["max_step"] == 3


def test_progressive_cycle_rejects_invalid_bound(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 1 and 33"):
        realtime.run_progressive_cycle(
            model="ICON-CH1-EPS",
            output_root=tmp_path,
            date="20260804",
            time_value="1800",
            through_step=34,
        )


def test_progressive_long_ch1_cycle_uses_45_hour_horizon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(realtime, "_state_completed_through", lambda *args, **kwargs: 45)
    monkeypatch.setattr(realtime, "available_contiguous_step", lambda **kwargs: 45)
    monkeypatch.setattr(realtime, "run_operational", lambda **kwargs: pytest.fail("unexpected processing"))

    state = realtime.run_progressive_cycle(
        model="ICON-CH1-EPS",
        output_root=tmp_path,
        date="20260804",
        time_value="0300",
    )

    assert state["horizon"] == 45
    assert state["completed_through"] == 45
    assert state["status"] == "complete"


def test_progressive_cycle_noops_when_no_new_hour(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(realtime, "_state_completed_through", lambda *args, **kwargs: 4)
    monkeypatch.setattr(realtime, "available_contiguous_step", lambda **kwargs: 4)
    monkeypatch.setattr(realtime, "run_operational", lambda **kwargs: pytest.fail("unexpected processing"))

    state = realtime.run_progressive_cycle(
        model="ICON-CH2-EPS",
        output_root=tmp_path,
        date="20260804",
        time_value="1200",
    )

    assert state["completed_through"] == 4
    assert state["available_through"] == 4
    assert state["increments"] == []


def test_progressive_cycle_does_not_advance_on_critical_increment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(realtime, "_state_completed_through", lambda *args, **kwargs: 2)
    monkeypatch.setattr(realtime, "available_contiguous_step", lambda **kwargs: 3)
    monkeypatch.setattr(
        realtime,
        "run_operational",
        lambda **kwargs: {"monitoring": {"ok": False, "status": "critical", "recommended_exit_code": 1}},
    )

    state = realtime.run_progressive_cycle(
        model="ICON-CH1-EPS",
        output_root=tmp_path,
        date="20260804",
        time_value="1800",
    )

    assert state["completed_through"] == 2
    assert state["status"] == "critical"


def test_progressive_cycle_records_raised_increment_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(realtime, "_state_completed_through", lambda *args, **kwargs: 2)
    monkeypatch.setattr(realtime, "available_contiguous_step", lambda **kwargs: 3)
    monkeypatch.setattr(realtime, "run_operational", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        realtime.run_progressive_cycle(
            model="ICON-CH1-EPS",
            output_root=tmp_path,
            date="20260804",
            time_value="1800",
        )

    saved = json.loads((tmp_path / "ICON-CH1-EPS" / "20260804" / "1800" / "CYCLE.json").read_text())
    assert saved["status"] == "critical"
    assert saved["completed_through"] == 2
    assert saved["last_error"] == "RuntimeError: boom"
