from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest

from precip_type_diag.constants import INPUT_PARAM_IDS
from precip_type_diag.operational import (
    FdbRun,
    _fields_by_step,
    _has_complete_param,
    _member_keys,
    _ml_fields_by_step,
    _parse_step,
    _step_expr,
    _step_token,
    config_for_model,
    parse_members,
    run_operational,
)


class FakeField:
    def __init__(self, metadata: dict[str, object], values: np.ndarray | None = None):
        self._metadata = metadata
        self._values = np.asarray([1.0] if values is None else values)

    def metadata(self, key: str):
        return self._metadata[key]

    def to_numpy(self, flatten: bool = False):
        if flatten:
            return self._values.reshape(-1)
        return self._values


def test_member_and_step_helpers() -> None:
    assert _member_keys("000") == ("cf", None)
    assert _member_keys("007") == ("pf", 7)
    assert _parse_step("60m") == 1
    assert _parse_step("2h") == 2
    assert _parse_step("3") == 3
    assert _step_expr([1, 2, 3]) == "1/to/3/by/1"
    assert _step_token(25) == "01010000"

    with pytest.raises(ValueError, match="hourly"):
        _parse_step("30m")
    with pytest.raises(ValueError, match="contiguous"):
        _step_expr([1, 3])


def test_parse_members_validates_model_members() -> None:
    assert parse_members("all", "ICON-CH1-EPS") == tuple(f"{member:03d}" for member in range(11))
    assert parse_members("000,010", "ICON-CH1-EPS") == ("000", "010")

    with pytest.raises(ValueError, match="Invalid member"):
        parse_members("0", "ICON-CH1-EPS")
    with pytest.raises(ValueError, match="not available"):
        parse_members("011", "ICON-CH1-EPS")


def test_field_grouping_uses_metadata_step_and_param() -> None:
    fields = [
        FakeField({"paramId": INPUT_PARAM_IDS["T"], "step": "1", "level": 2}),
        FakeField({"paramId": INPUT_PARAM_IDS["T"], "step": "1", "level": 1}),
        FakeField({"paramId": INPUT_PARAM_IDS["P"], "endStep": "2h", "level": 1}),
        FakeField({"paramId": INPUT_PARAM_IDS["TOT_PREC"], "step": "60m"}),
    ]

    ml = _ml_fields_by_step(fields[:3])
    by_step = _fields_by_step(fields[3:])

    assert sorted(ml) == [1, 2]
    assert len(ml[1]["T"]) == 2
    assert len(ml[2]["P"]) == 1
    assert sorted(by_step) == [1]


def test_has_complete_param_checks_steps_levels_and_timespan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "precip_type_diag.operational._fdb_utils_list",
        lambda expr: {"timespan": ["none"], "step": ["0", "1h"], "levelist": [1, 2, 3]},
    )

    assert _has_complete_param(
        model="ICON-CH2-EPS",
        member="000",
        date="20260531",
        time_value="1800",
        param=INPUT_PARAM_IDS["HHL"],
        levtype="ml",
        timespan="none",
        expected_steps={0, 1},
        expected_levels={1, 2},
    )
    assert not _has_complete_param(
        model="ICON-CH2-EPS",
        member="000",
        date="20260531",
        time_value="1800",
        param=INPUT_PARAM_IDS["HHL"],
        levtype="ml",
        timespan="fs",
        expected_steps={0},
    )
    assert not _has_complete_param(
        model="ICON-CH2-EPS",
        member="000",
        date="20260531",
        time_value="1800",
        param=INPUT_PARAM_IDS["HHL"],
        levtype="ml",
        timespan="none",
        expected_steps={0, 2},
    )


def test_config_for_model_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="Unsupported model"):
        config_for_model("ICON-CH3-EPS")
    with pytest.raises(ValueError, match="non-negative"):
        config_for_model("ICON-CH1-EPS", precip_mask_threshold_mm=-0.1)
    with pytest.raises(ValueError, match="positive"):
        config_for_model("ICON-CH1-EPS", chunk_size=0)
    with pytest.raises(ValueError, match="not available"):
        config_for_model("ICON-CH1-EPS", members=("011",))


def test_run_operational_writes_summary_for_fixed_fdb_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    processed: list[FdbRun] = []
    caplog.set_level(logging.INFO, logger="precip_type_diag.operational")

    def fake_process_member(**kwargs):
        run = kwargs["run"] if "run" in kwargs else None
        assert run is None
        processed.append(
            FdbRun(
                date=kwargs["date"],
                time=kwargs["time_value"],
                model="icon-ch1-eps",
                member=kwargs["member"],
                type="cf" if kwargs["member"] == "000" else "pf",
                number=None if kwargs["member"] == "000" else int(kwargs["member"]),
                max_step=kwargs["max_step"],
            )
        )
        return {
            "run": {"member": kwargs["member"]},
            "steps": 2,
            "written": 2,
            "timings_s": {
                "discovery_s": 0.0,
                "static_request_s": 1.0,
                "static_decode_s": 0.0,
                "request_s": 2.0,
                "decode_s": 3.0,
                "diagnose_s": 4.0,
                "write_s": 5.0,
            },
            "wall_s": 1.0,
        }

    monkeypatch.setattr("precip_type_diag.operational._configure_meteoswiss_definitions", lambda: None)
    monkeypatch.setattr("precip_type_diag.operational._warm_diagnostic", lambda: None)
    monkeypatch.setattr("precip_type_diag.operational._process_member", fake_process_member)
    monkeypatch.setattr("precip_type_diag.operational.collect_runtime_provenance", lambda: {"git": {"commit": "abc"}})

    summary = run_operational(
        model="ICON-CH1-EPS",
        members=("000", "001"),
        date="20260531",
        time_value="1800",
        max_step=1,
        output_root=tmp_path,
        workers=1,
        prefetch=False,
    )

    summary_path = tmp_path / "ICON-CH1-EPS" / "20260531" / "1800" / "summary.json"
    assert summary_path.exists()
    assert summary["failed"] == {}
    assert summary["processed_members"] == ["000", "001"]
    assert summary["timings_s"]["request_s"] == 4.0
    assert summary["data_quality"]["total_columns"] == 0
    assert summary["provenance"] == {"git": {"commit": "abc"}}
    assert [run.member for run in processed] == ["000", "001"]
    assert "starting operational run model=ICON-CH1-EPS" in caplog.text
    assert "finished operational run model=ICON-CH1-EPS processed=2 failed=0" in caplog.text


def test_run_operational_discovers_latest_complete_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    discovered = FdbRun(
        date="20260531",
        time="1800",
        model="icon-ch2-eps",
        member="000",
        type="cf",
        number=None,
        max_step=1,
        discovery_s=7.0,
    )

    monkeypatch.setattr("precip_type_diag.operational._configure_meteoswiss_definitions", lambda: None)
    monkeypatch.setattr("precip_type_diag.operational._warm_diagnostic", lambda: None)
    monkeypatch.setattr("precip_type_diag.operational.discover_complete_run", lambda **kwargs: discovered)
    monkeypatch.setattr(
        "precip_type_diag.operational._process_member",
        lambda **kwargs: {"run": {"member": kwargs["member"]}, "timings_s": {}, "written": 0, "steps": 0, "wall_s": 0.0},
    )

    summary = run_operational(
        model="ICON-CH2-EPS",
        members=("000",),
        max_step=1,
        output_root=tmp_path,
        workers=1,
    )

    assert summary["date"] == "20260531"
    assert summary["time"] == "1800"
    assert summary["discovery_s"] == 7.0


def test_run_operational_records_member_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_process_member(**kwargs):
        if kwargs["member"] == "001":
            raise RuntimeError("bad member")
        return {"run": {"member": kwargs["member"]}, "timings_s": {}, "written": 0, "steps": 0, "wall_s": 0.0}

    monkeypatch.setattr("precip_type_diag.operational._configure_meteoswiss_definitions", lambda: None)
    monkeypatch.setattr("precip_type_diag.operational._warm_diagnostic", lambda: None)
    monkeypatch.setattr("precip_type_diag.operational._process_member", fake_process_member)

    summary = run_operational(
        model="ICON-CH1-EPS",
        members=("000", "001"),
        date="20260531",
        time_value="1800",
        max_step=0,
        output_root=tmp_path,
        workers=1,
    )

    assert summary["processed_members"] == ["000"]
    assert summary["failed"] == {"001": "RuntimeError: bad member"}
