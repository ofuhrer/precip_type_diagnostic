from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from earthkit.data import from_source
from earthkit.data.encoders.grib import GribEncoder

from precip_type_diag.constants import PrecipitationTypeCode
from precip_type_diag.gribio import bootstrap_eccodes_definitions, write_output_grib
from precip_type_diag.verification import (
    ObservationRecord,
    load_column_validation_manifest,
    load_observation_records_csv,
    run_column_validation_manifest,
    run_prototype_regression_manifest,
    score_observation_records,
)


def _write_template_grib(path: Path) -> object:
    encoder = GribEncoder()
    with path.open("wb") as handle:
        encoder.encode(
            values=np.array([[0.0, 1.0], [2.0, 3.0]]),
            metadata={
                "gridType": "regular_ll",
                "Nx": 2,
                "Ny": 2,
                "latitudeOfFirstGridPointInDegrees": 1.0,
                "longitudeOfFirstGridPointInDegrees": 0.0,
                "latitudeOfLastGridPointInDegrees": 0.0,
                "longitudeOfLastGridPointInDegrees": 1.0,
                "iDirectionIncrementInDegrees": 1.0,
                "jDirectionIncrementInDegrees": 1.0,
                "date": 20260423,
                "time": 0,
                "step": 1,
                "shortName": "T_G",
                "typeOfFirstFixedSurface": 1,
                "scaledValueOfFirstFixedSurface": 0,
                "scaleFactorOfFirstFixedSurface": 0,
                "packingType": "grid_simple",
            },
        ).to_file(handle)
    return from_source("file", str(path))[0]


def test_run_prototype_regression_manifest(tmp_path: Path) -> None:
    bootstrap_eccodes_definitions()

    template = _write_template_grib(tmp_path / "template.grib2")
    reference = tmp_path / "reference.grib2"
    candidate = tmp_path / "candidate.grib2"
    write_output_grib(template, np.array([[5, 1], [12, 0]], dtype=np.int32), reference)
    write_output_grib(template, np.array([[5, 1], [12, 0]], dtype=np.int32), candidate)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "synthetic",
                        "candidate_grib": str(candidate),
                        "reference_grib": str(reference),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_prototype_regression_manifest(manifest)
    assert result["all_equal"] is True
    assert result["cases"][0]["diff_count"] == 0


def test_load_observation_records_csv_and_score(tmp_path: Path) -> None:
    path = tmp_path / "obs.csv"
    path.write_text(
        "predicted,observed\nrain,rain\nsnow,freezing_rain\n12,12\n",
        encoding="utf-8",
    )

    records = load_observation_records_csv(path)
    assert records == [
        ObservationRecord(predicted=PrecipitationTypeCode.RAIN, observed=PrecipitationTypeCode.RAIN),
        ObservationRecord(predicted=PrecipitationTypeCode.SNOW, observed=PrecipitationTypeCode.FREEZING_RAIN),
        ObservationRecord(predicted=PrecipitationTypeCode.FREEZING_DRIZZLE, observed=PrecipitationTypeCode.FREEZING_DRIZZLE),
    ]

    score = score_observation_records(records)
    assert score["n_records"] == 3
    assert score["by_category"]["rain"]["tp"] == 1
    assert score["by_category"]["snow"]["fp"] == 1
    assert score["any_freezing_precip"]["tp"] == 1
    assert score["any_freezing_precip"]["fn"] == 1


def test_run_column_validation_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "column-validation.json"
    manifest.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "cold_saturated_column",
                        "temperature_k": [253.15, 255.15, 257.15, 259.15, 261.15],
                        "pressure_pa": [70000.0, 76000.0, 82000.0, 90000.0, 98000.0],
                        "specific_humidity": [0.0015, 0.0015, 0.0015, 0.0015, 0.0015],
                        "full_level_height_m": [4000.0, 3000.0, 2000.0, 1000.0, 0.0],
                        "total_precip_mm": 1.0,
                        "ground_temperature_c": -5.0,
                        "expected": "snow",
                        "metadata": {
                            "source": "synthetic test profile",
                            "event_type": "all_subfreezing",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_column_validation_manifest(manifest)

    assert result["all_passed"] is True
    assert result["n_cases"] == 1
    assert result["cases"][0]["actual"] == int(PrecipitationTypeCode.SNOW)
    assert result["cases"][0]["actual_name"] == "snow"


def test_load_column_validation_manifest_rejects_inconsistent_lengths(tmp_path: Path) -> None:
    manifest = tmp_path / "bad-column-validation.json"
    manifest.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "bad",
                        "temperature_k": [253.15, 255.15],
                        "pressure_pa": [70000.0],
                        "specific_humidity": [0.0015, 0.0015],
                        "full_level_height_m": [4000.0, 3000.0],
                        "total_precip_mm": 1.0,
                        "ground_temperature_c": -5.0,
                        "expected": "snow",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    try:
        load_column_validation_manifest(manifest)
    except ValueError as exc:
        assert "inconsistent full-level array lengths" in str(exc)
    else:
        raise AssertionError("expected ValueError")
