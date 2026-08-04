from __future__ import annotations

from pathlib import Path

import eccodes
import numpy as np
import pytest
from earthkit.data import from_source

from precip_type_diag.constants import OUTPUT_PARAM_ID, OUTPUT_SHORT_NAME
from precip_type_diag.gribio import (
    bootstrap_eccodes_definitions,
    check_precip_mask_threshold_mm,
    derive_vertical_level_selection,
    write_output_grib,
)


def _write_template_grib(path: Path) -> object:
    handle_id = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, value in {
            "Ni": 2,
            "Nj": 2,
            "latitudeOfFirstGridPointInDegrees": 1.0,
            "longitudeOfFirstGridPointInDegrees": 0.0,
            "latitudeOfLastGridPointInDegrees": 0.0,
            "longitudeOfLastGridPointInDegrees": 1.0,
            "iDirectionIncrementInDegrees": 1.0,
            "jDirectionIncrementInDegrees": 1.0,
            "date": 20260423,
            "time": 0,
            "step": 1,
            "discipline": 0,
            "parameterCategory": 0,
            "parameterNumber": 0,
            "typeOfFirstFixedSurface": 1,
            "scaledValueOfFirstFixedSurface": 0,
            "scaleFactorOfFirstFixedSurface": 0,
            "packingType": "grid_simple",
        }.items():
            eccodes.codes_set(handle_id, key, value)
        eccodes.codes_set_values(handle_id, np.array([[0.0, 1.0], [2.0, 3.0]]).reshape(-1))
        with path.open("wb") as handle:
            eccodes.codes_write(handle_id, handle)
    finally:
        eccodes.codes_release(handle_id)
    return from_source("file", str(path))[0]


def test_precip_mask_threshold_check_rejects_negative_or_non_finite() -> None:
    assert check_precip_mask_threshold_mm(0.25) == 0.25

    with pytest.raises(ValueError, match="non-negative"):
        check_precip_mask_threshold_mm(-0.1)
    with pytest.raises(ValueError, match="finite"):
        check_precip_mask_threshold_mm(float("nan"))


def test_bootstrap_eccodes_definitions_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("precip_type_diag.gribio._package_definitions_dir", lambda: Path("/defs/local"))
    monkeypatch.setattr("precip_type_diag.gribio._candidate_meteoswiss_definition_dirs", lambda: [Path("/defs/ms")])
    monkeypatch.setattr("precip_type_diag.gribio.eccodes.codes_definition_path", lambda: "/defs/local:/defs/ms:/eccodes/base")
    monkeypatch.setattr("precip_type_diag.gribio.eccodes.codes_set_definitions_path", calls.append)

    combined = bootstrap_eccodes_definitions()

    assert combined == "/defs/local:/defs/ms:/eccodes/base"
    assert calls == ["/defs/local:/defs/ms:/eccodes/base"]


def test_output_grib_metadata_is_stable(tmp_path: Path) -> None:
    bootstrap_eccodes_definitions()

    template_path = tmp_path / "template.grib2"
    template_field = _write_template_grib(template_path)

    output_path = tmp_path / "ptype.grib2"
    write_output_grib(template_field, np.array([[13, 1], [5, 8]], dtype=np.int32), output_path)

    output_field = from_source("file", str(output_path))[0]
    assert output_field.metadata("paramId") in {OUTPUT_PARAM_ID, 260015}
    assert output_field.metadata("shortName") in {OUTPUT_SHORT_NAME, "ptype"}
    assert output_field.metadata("discipline") == 0
    assert output_field.metadata("parameterCategory") == 1
    assert output_field.metadata("parameterNumber") == 19
    assert output_field.metadata("date") == template_field.metadata("date")
    assert output_field.metadata("time") == template_field.metadata("time")
    assert output_field.metadata("step") == template_field.metadata("step")
    assert output_field.metadata("Nx") == template_field.metadata("Nx")
    assert output_field.metadata("Ny") == template_field.metadata("Ny")
    np.testing.assert_allclose(output_field.to_numpy(), np.array([[13.0, 1.0], [5.0, 8.0]]))


def test_output_grib_write_is_atomic_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenEncoded:
        def to_file(self, handle):
            raise RuntimeError("boom")

    class BrokenEncoder:
        def encode(self, **kwargs):
            return BrokenEncoded()

    monkeypatch.setattr("precip_type_diag.gribio.GribEncoder", BrokenEncoder)
    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")

    destination = tmp_path / "ptype.grib2"
    with pytest.raises(RuntimeError, match="boom"):
        write_output_grib(object(), np.array([[1, 0], [0, 1]], dtype=np.int32), destination)

    assert not destination.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_output_grib_rejects_invalid_category_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")

    destination = tmp_path / "ptype.grib2"
    with pytest.raises(ValueError, match="invalid code"):
        write_output_grib(object(), np.array([[2]], dtype=np.int32), destination)

    assert not destination.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_output_grib_rejects_shape_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")

    destination = tmp_path / "ptype.grib2"
    with pytest.raises(ValueError, match="does not match template shape"):
        write_output_grib(object(), np.array([[1]], dtype=np.int32), destination, expected_shape=(2, 2))

    assert not destination.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_derive_vertical_level_selection_from_synthetic_hhl() -> None:
    half_level_height_m = np.array(
        [
            [4000.0, 4100.0],
            [3000.0, 3100.0],
            [2000.0, 2100.0],
            [1000.0, 1100.0],
            [0.0, 100.0],
        ]
    )

    selection = derive_vertical_level_selection(half_level_height_m, 2500.0)

    assert selection.full_level_start == 1
    assert selection.half_level_start == 1
    assert selection.retained_full_levels == 3


def test_derive_vertical_level_selection_rejects_non_finite_cutoff() -> None:
    half_level_height_m = np.array([[2000.0], [1000.0], [0.0]])

    with pytest.raises(ValueError, match="must be finite"):
        derive_vertical_level_selection(half_level_height_m, float("nan"))
