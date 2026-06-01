from __future__ import annotations

import numpy as np

from precip_type_diag.constants import PrecipitationTypeCode
from precip_type_diag.operational import FdbChunk
from precip_type_diag.profile_samples import (
    ProfileSamplePoint,
    _parse_steps,
    extract_profile_samples,
)


class FakeField:
    def __init__(self, metadata: dict[str, object], values: np.ndarray):
        self._metadata = metadata
        self._values = values

    def metadata(self, key: str):
        return self._metadata[key]

    def to_numpy(self, flatten: bool = False):
        if flatten:
            return self._values.reshape(-1)
        return self._values


def _level_fields(step: int, values: list[np.ndarray]) -> list[FakeField]:
    return [FakeField({"step": step, "level": level}, value) for level, value in enumerate(values, start=1)]


def _chunk() -> FdbChunk:
    cold_levels = [np.array([263.15, 264.15]), np.array([265.15, 266.15])]
    pressure_levels = [np.array([80000.0, 80000.0]), np.array([90000.0, 90000.0])]
    humidity_levels = [np.array([0.0015, 0.0015]), np.array([0.0015, 0.0015])]
    ml_by_step = {
        step: {
            "T": _level_fields(step, cold_levels),
            "P": _level_fields(step, pressure_levels),
            "QV": _level_fields(step, humidity_levels),
        }
        for step in (0, 1)
    }
    return FdbChunk(
        steps=[0, 1],
        ml_by_step=ml_by_step,
        total_precip_by_step={
            0: FakeField({"step": 0}, np.array([0.25, 0.5])),
            1: FakeField({"step": 1}, np.array([1.25, 1.75])),
        },
        ground_temperature_by_step={
            0: FakeField({"step": 0}, np.array([268.15, 268.15])),
            1: FakeField({"step": 1}, np.array([268.15, 268.15])),
        },
        request_s=0.0,
    )


def test_parse_steps_supports_lists_and_ranges() -> None:
    assert _parse_steps("0,2,1,1") == [0, 1, 2]
    assert _parse_steps("0/to/4/by/2") == [0, 2, 4]


def test_extract_profile_samples_for_explicit_point(monkeypatch) -> None:
    monkeypatch.setattr(
        "precip_type_diag.profile_samples._fetch_hhl",
        lambda run, timings: np.array([[2000.0, 2000.0], [1000.0, 1000.0], [0.0, 0.0]]),
    )
    monkeypatch.setattr("precip_type_diag.profile_samples._fetch_chunk", lambda *args, **kwargs: _chunk())

    payload = extract_profile_samples(
        model="ICON-CH1-EPS",
        member="000",
        date="20260531",
        time_value="1800",
        steps=[1],
        points=[
            ProfileSamplePoint(
                name="test_point",
                flat_index=1,
                expected=PrecipitationTypeCode.SNOW,
                metadata={"station": "synthetic"},
            )
        ],
        validate_inputs=False,
    )

    assert len(payload["cases"]) == 1
    case = payload["cases"][0]
    assert case["name"] == "test_point_step001"
    assert case["expected"] == "snow"
    assert case["total_precip_mm"] == 1.25
    assert case["metadata"]["flat_index"] == 1
    assert case["metadata"]["station"] == "synthetic"
    assert case["metadata"]["model"] == "ICON-CH1-EPS"


def test_extract_profile_samples_can_auto_select_diagnostic_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        "precip_type_diag.profile_samples._fetch_hhl",
        lambda run, timings: np.array([[2000.0, 2000.0], [1000.0, 1000.0], [0.0, 0.0]]),
    )
    monkeypatch.setattr("precip_type_diag.profile_samples._fetch_chunk", lambda *args, **kwargs: _chunk())

    payload = extract_profile_samples(
        model="ICON-CH1-EPS",
        member="000",
        date="20260531",
        time_value="1800",
        steps=[0],
        auto_select_codes=(PrecipitationTypeCode.NO_PRECIP,),
        samples_per_type=1,
        validate_inputs=False,
    )

    assert len(payload["cases"]) == 1
    case = payload["cases"][0]
    assert "expected" not in case
    assert case["metadata"]["selection"] == "diagnostic_category_candidate"
    assert case["metadata"]["diagnostic_name"] == "no_precip"
