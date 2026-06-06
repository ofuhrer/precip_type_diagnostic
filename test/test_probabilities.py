from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from precip_type_diag.netcdfio import read_netcdf
from precip_type_diag.probabilities import (
    aggregate_member_diagnostics,
    generate_probability_products,
    member_diagnostic_variables,
    write_member_diagnostic_netcdf,
)


def _probabilities(value: float, shape: tuple[int, int] = (2, 2)) -> dict[str, np.ndarray]:
    return {
        "prob_rain_mm": np.full(shape, value),
        "prob_snow_mm": np.zeros(shape),
        "prob_ice_pellets_mm": np.zeros(shape),
        "prob_freezing_drizzle_mm": np.zeros(shape),
        "prob_freezing_rain_on_ground_mm": np.zeros(shape),
        "prob_freezing_rain_mm": np.zeros(shape),
    }


def test_member_diagnostic_variables_builds_thresholded_precipitation() -> None:
    ptype = np.array([[1, 5], [0, 3]], dtype=np.int32)
    hourly_precip = np.array([[0.2, 0.005], [0.4, 0.6]])
    probabilities = _probabilities(0.0)
    probabilities["prob_rain_mm"] = np.array([[31.0, 40.0], [29.0, 0.0]])
    probabilities["prob_freezing_rain_mm"] = np.array([[0.0, 0.0], [0.0, 75.0]])

    variables = member_diagnostic_variables(
        ptype=ptype,
        hourly_precip_mm=hourly_precip,
        probabilities=probabilities,
    )

    np.testing.assert_allclose(variables["precip_rain_th_mm"], np.array([[0.2, 0.0], [0.0, 0.0]]))
    np.testing.assert_allclose(variables["precip_freezing_rain_th_mm"], np.array([[0.0, 0.0], [0.0, 0.6]]))


def test_member_diagnostic_variables_rejects_invalid_probability_scale() -> None:
    probabilities = _probabilities(101.0, shape=(1, 1))
    with pytest.raises(ValueError, match="percent range"):
        member_diagnostic_variables(
            ptype=np.array([[1]], dtype=np.int32),
            hourly_precip_mm=np.array([[1.0]]),
            probabilities=probabilities,
        )


def test_aggregate_member_diagnostics_computes_means_and_categorical_frequencies() -> None:
    member_0 = member_diagnostic_variables(
        ptype=np.array([[0, 1], [5, 8]], dtype=np.int32),
        hourly_precip_mm=np.array([[0.0, 1.0], [2.0, 3.0]]),
        probabilities={**_probabilities(20.0), "prob_snow_mm": np.full((2, 2), 80.0)},
    )
    member_1 = member_diagnostic_variables(
        ptype=np.array([[1, 1], [5, 13]], dtype=np.int32),
        hourly_precip_mm=np.array([[1.0, 3.0], [4.0, 5.0]]),
        probabilities={**_probabilities(60.0), "prob_snow_mm": np.full((2, 2), 40.0)},
    )

    products = aggregate_member_diagnostics([member_0, member_1])

    np.testing.assert_allclose(products.probability_means["prob_rain_mm_ens"], np.full((2, 2), 40.0))
    np.testing.assert_allclose(products.probability_means["prob_snow_mm_ens"], np.full((2, 2), 60.0))
    np.testing.assert_allclose(products.categorical_probabilities[0], np.array([[50.0, 0.0], [0.0, 0.0]]))
    np.testing.assert_allclose(products.categorical_probabilities[1], np.array([[50.0, 100.0], [0.0, 0.0]]))
    np.testing.assert_allclose(products.valid_member_count, np.full((2, 2), 2.0))
    np.testing.assert_allclose(products.hourly_precip_mean_mm, np.array([[0.5, 2.0], [3.0, 4.0]]))


def test_write_member_diagnostic_netcdf_roundtrips(tmp_path: Path) -> None:
    output = tmp_path / "member.ptype_diag.nc"
    write_member_diagnostic_netcdf(
        output,
        ptype=np.array([[1, 0], [5, 3]], dtype=np.int32),
        hourly_precip_mm=np.array([[1.0, 0.0], [2.0, 3.0]]),
        probabilities={**_probabilities(50.0), "prob_freezing_rain_mm": np.full((2, 2), 75.0)},
        attrs={"model": "ICON-CH1-EPS", "date": "20260531", "time": "1800", "member": "000", "step": 1},
    )

    variables = read_netcdf(output)
    assert sorted(variables) == sorted(
        [
            "ptype",
            "hourly_precip_mm",
            "prob_rain_mm",
            "prob_snow_mm",
            "prob_ice_pellets_mm",
            "prob_freezing_drizzle_mm",
            "prob_freezing_rain_on_ground_mm",
            "prob_freezing_rain_mm",
            "precip_rain_th_mm",
            "precip_snow_th_mm",
            "precip_ice_pellets_th_mm",
            "precip_freezing_drizzle_th_mm",
            "precip_freezing_rain_on_ground_th_mm",
            "precip_freezing_rain_th_mm",
        ]
    )
    np.testing.assert_array_equal(variables["ptype"], np.array([[1, 0], [5, 3]], dtype=np.int32))
    np.testing.assert_allclose(variables["prob_freezing_rain_mm"], np.full((2, 2), 75.0))


def test_generate_probability_products_requires_all_sidecars(tmp_path: Path) -> None:
    summary = generate_probability_products(
        output_root=tmp_path,
        model="ICON-CH1-EPS",
        date="20260531",
        time_value="1800",
        members=("000", "001"),
        processed_members=("000",),
        failed_members=("001",),
        start_step=0,
        max_step=0,
    )

    assert summary["enabled"] is True
    assert summary["status"] == "failed"
    assert summary["format"] == "netcdf"
    assert summary["files_written"] == 0
    assert summary["missing_members"] == ["001"]
    assert "failed members: 001" in str(summary["error"])
    assert not (tmp_path / "ICON-CH1-EPS" / "20260531" / "1800" / "probabilities").exists()


def test_generate_probability_products_writes_step_netcdf(tmp_path: Path) -> None:
    date = "20260531"
    time_value = "1800"
    for member, ptype, rain_probability in [
        ("000", np.array([[0, 1], [5, 8]], dtype=np.int32), 20.0),
        ("001", np.array([[1, 1], [5, 13]], dtype=np.int32), 60.0),
    ]:
        destination = tmp_path / "ICON-CH1-EPS" / date / time_value / member / "lfff00000000.ptype_diag.nc"
        write_member_diagnostic_netcdf(
            destination,
            ptype=ptype,
            hourly_precip_mm=np.full((2, 2), 2.0),
            probabilities=_probabilities(rain_probability),
            attrs={"model": "ICON-CH1-EPS", "date": date, "time": time_value, "member": member, "step": 0},
        )

    summary = generate_probability_products(
        output_root=tmp_path,
        model="ICON-CH1-EPS",
        date=date,
        time_value=time_value,
        members=("000", "001"),
        processed_members=("000", "001"),
        failed_members=(),
        start_step=0,
        max_step=0,
    )

    assert summary["status"] == "ok"
    assert summary["format"] == "netcdf"
    assert summary["scale"] == "percent_0_100"
    assert summary["files_written"] == 1
    output_path = tmp_path / "ICON-CH1-EPS" / date / time_value / "probabilities" / "lfff00000000.ptype_prob.nc"
    variables = read_netcdf(output_path)
    np.testing.assert_allclose(variables["prob_rain_mm_ens"], np.full((2, 2), 40.0))
    np.testing.assert_allclose(variables["ptype_probability_0"], np.array([[50.0, 0.0], [0.0, 0.0]]))
    np.testing.assert_allclose(variables["ptype_probability_1"], np.array([[50.0, 100.0], [0.0, 0.0]]))
    np.testing.assert_allclose(variables["valid_member_count"], np.full((2, 2), 2.0))
