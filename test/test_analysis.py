from __future__ import annotations

import hashlib
import json
from pathlib import Path

import eccodes
import netCDF4
import numpy as np
import pyarrow.parquet as pq
import pytest

from precip_type_diag import analysis
from precip_type_diag.constants import PTYPE_BITS_PER_VALUE
from precip_type_diag.gribio import bootstrap_eccodes_definitions
from precip_type_diag.operational import _atomic_write_json


def test_analysis_plan_can_select_one_month(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_manifest_path = tmp_path / "source-campaign" / "manifest.json"
    _atomic_write_json({"source": "synthetic"}, source_manifest_path)
    source_root = tmp_path / "source-products"
    source_manifest = {
        "output_root": str(source_root),
        "diagnostic_algorithm": "icon",
        "periods": [
            {
                "period": "201001",
                "dates": ["20100101", "20100131"],
                "message_count": 48,
                "archive": "ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201001.grib2",
            },
            {
                "period": "201002",
                "dates": ["20100201", "20100228"],
                "message_count": 48,
                "archive": "ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201002.grib2",
            },
        ],
    }
    monkeypatch.setattr(analysis, "load_source_manifest", lambda _: source_manifest)
    monkeypatch.setattr(analysis, "source_campaign_status", lambda **_: {"complete": True})

    manifest = analysis.build_analysis_manifest(
        source_manifest_path=source_manifest_path,
        manifest_path=tmp_path / "analysis-campaign" / "manifest.json",
        output_root=tmp_path / "analysis-products",
        staging_root=tmp_path / "analysis-staging",
        start_period="201002",
        end_period="201002",
    )

    assert manifest["selected_period_range"] == {"start": "201002", "end": "201002"}
    assert manifest["cycle_date_range"] == {"start": "20100201", "end": "20100228", "inclusive": True}
    assert manifest["valid_time_coverage"] == {
        "first_interval_end": "2010-02-01T01:00:00+00:00",
        "last_interval_end": "2010-03-01T00:00:00+00:00",
    }
    assert manifest["periods"] == [
        {
            "index": 0,
            "source_index": 1,
            "period": "201002",
            "dates": ["20100201", "20100228"],
            "message_count": 48,
            "source_archive": "ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201002.grib2",
            "compact_archive": "compact/ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201002.grib2",
            "monthly_statistics": "monthly/ptype_counts_201002.nc",
            "hourly_counts": "monthly/ptype_hourly_counts_201002.parquet",
        }
    ]

    with pytest.raises(ValueError, match="no source periods"):
        analysis.build_analysis_manifest(
            source_manifest_path=source_manifest_path,
            manifest_path=tmp_path / "other" / "manifest.json",
            output_root=tmp_path / "other-products",
            staging_root=tmp_path / "other-staging",
            start_period="201003",
        )


def _write_source_archive(path: Path, *, date: str = "20100131") -> None:
    bootstrap_eccodes_definitions()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        for step in range(1, 25):
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
                    "date": int(date),
                    "time": 0,
                    "step": step,
                    "discipline": 0,
                    "parameterCategory": 1,
                    "parameterNumber": 19,
                    "typeOfFirstFixedSurface": 1,
                    "scaledValueOfFirstFixedSurface": 0,
                    "scaleFactorOfFirstFixedSurface": 0,
                    "packingType": "grid_simple",
                    "bitsPerValue": 16,
                }.items():
                    eccodes.codes_set(handle_id, key, value)
                if step == 1:
                    values = np.array([3, 13, 0, 1], dtype=float)
                elif step == 6:
                    values = np.zeros(4, dtype=float)
                else:
                    values = np.array([0, 0, 5, 1], dtype=float)
                eccodes.codes_set_values(handle_id, values)
                assert eccodes.codes_get(handle_id, "bitsPerValue") == (0 if step == 6 else 16)
                eccodes.codes_write(handle_id, output)
            finally:
                eccodes.codes_release(handle_id)


def _analysis_manifest(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_manifest_path = tmp_path / "source-campaign" / "manifest.json"
    _atomic_write_json({"source": "synthetic"}, source_manifest_path)
    _atomic_write_json(
        {"daily_results": [{"data_quality": {"clamped_negative_total_precip_deltas": 7}}]},
        source_manifest_path.parent / "receipts" / "00000-201001.json",
    )
    source_root = tmp_path / "source-products"
    source_archive = source_root / "ICON-REA-L-CH1" / "2010" / "ptype_ICON-REA-L-CH1_201001.grib2"
    _write_source_archive(source_archive)
    output_root = tmp_path / "analysis-products"
    manifest_path = tmp_path / "analysis-campaign" / "manifest.json"
    manifest = {
        "schema_version": analysis.ANALYSIS_MANIFEST_SCHEMA_VERSION,
        "mode": analysis.ANALYSIS_MANIFEST_MODE,
        "model": analysis.REA_MODEL,
        "source_manifest_path": str(source_manifest_path.resolve()),
        "source_manifest_sha256": hashlib.sha256(source_manifest_path.read_bytes()).hexdigest(),
        "source_archive_root": str(source_root.resolve()),
        "output_root": str(output_root.resolve()),
        "staging_root": str((tmp_path / "staging").resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "created_at": "2026-08-08T00:00:00+00:00",
        "cycle_date_range": {"start": "20100131", "end": "20100131", "inclusive": True},
        "valid_time_coverage": {
            "first_interval_end": "2010-01-31T01:00:00+00:00",
            "last_interval_end": "2010-02-01T00:00:00+00:00",
        },
        "source_diagnostic_algorithm": "icon",
        "category_codes": list(analysis.CATEGORY_CODES),
        "category_names": list(analysis.CATEGORY_NAMES),
        "high_impact_codes": list(analysis.HIGH_IMPACT_CODES),
        "compact_bits_per_value": PTYPE_BITS_PER_VALUE,
        "periods": [
            {
                "index": 0,
                "period": "201001",
                "dates": ["20100131"],
                "message_count": 24,
                "source_archive": "ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201001.grib2",
                "compact_archive": "compact/ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201001.grib2",
                "monthly_statistics": "monthly/ptype_counts_201001.nc",
                "hourly_counts": "monthly/ptype_hourly_counts_201001.parquet",
            }
        ],
        "final_products": {
            "hourly_counts": "ptype_hourly_counts.parquet",
            "frequency": "ptype_frequency.nc",
            "high_impact_events": "high_impact_events.parquet",
            "freezing_rain_map": "maps/freezing_rain_frequency.nc",
            "quality_report": "DATA_QUALITY_REPORT.json",
            "quality_report_markdown": "DATA_QUALITY_REPORT.md",
            "grid": "grid.nc",
            "reduction_receipt": "REDUCTION.json",
        },
    }
    _atomic_write_json(manifest, manifest_path)
    return manifest_path, source_archive, output_root


def test_analysis_task_repackages_exactly_and_groups_by_valid_month(tmp_path: Path) -> None:
    manifest_path, source_archive, output_root = _analysis_manifest(tmp_path)
    source_bytes = source_archive.read_bytes()

    receipt = analysis.run_analysis_task(manifest_path=manifest_path, index=0)

    assert receipt["ok"] is True
    assert receipt["message_count"] == 24
    assert receipt["source_archive"]["bits_per_value_counts"] == {"0": 1, "16": 23}
    assert receipt["compact_archive"]["bits_per_value_counts"] == {"0": 1, "4": 23}
    assert receipt["decoded_sha256"]
    assert receipt["compression_ratio"] > 1.0
    assert source_archive.read_bytes() == source_bytes
    manifest = analysis.load_analysis_manifest(manifest_path)
    period = manifest["periods"][0]
    assert analysis._receipt_complete(
        manifest_path=manifest_path,
        manifest=manifest,
        period=period,
        verify_outputs=True,
    )

    compact = output_root / "compact/ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201001.grib2"
    check = analysis._validate_compact_archive(compact, period)
    assert check["decoded_sha256"] == receipt["decoded_sha256"]
    assert check["message_count"] == 24

    with netCDF4.Dataset(output_root / "monthly/ptype_counts_201001.nc") as dataset:
        assert list(dataset.variables["valid_period"][:]) == ["201001", "201002"]
        assert list(dataset.variables["valid_hours"][:]) == [23, 1]
        assert dataset.variables["ptype_count"].shape == (2, len(analysis.CATEGORY_CODES), 4)

    table = pq.read_table(output_root / "monthly/ptype_hourly_counts_201001.parquet")
    assert table.num_rows == 24
    assert table.column("high_impact_grid_point_count")[0].as_py() == 2
    assert table.column("qc_ok").to_pylist() == [True] * 24

    changed_source = bytearray(source_bytes)
    changed_source[-1] ^= 1
    source_archive.write_bytes(changed_source)
    assert not analysis._receipt_complete(
        manifest_path=manifest_path,
        manifest=manifest,
        period=period,
        verify_outputs=True,
    )


def test_analysis_reducer_writes_frequency_events_and_quality_report(tmp_path: Path) -> None:
    manifest_path, _, output_root = _analysis_manifest(tmp_path)
    analysis.run_analysis_task(manifest_path=manifest_path, index=0)

    reduction = analysis.reduce_analysis(manifest_path=manifest_path)

    assert reduction["ok"] is True
    assert analysis.analysis_status(manifest_path=manifest_path)["complete"] is True
    report = json.loads((output_root / "DATA_QUALITY_REPORT.json").read_text())
    assert report["status"] == "pass"
    assert report["hourly_records"] == 24
    assert report["temporal_gap_or_duplicate_count"] == 0
    assert report["four_bit_exact_decoded_validation"] is True
    assert report["high_impact_event_count"] == 1
    assert report["source_generation_data_quality"] == {"clamped_negative_total_precip_deltas": 7}

    with netCDF4.Dataset(output_root / "ptype_frequency.nc") as dataset:
        assert dataset.variables["annual_valid_hours"][:].tolist() == [24]
        assert dataset.variables["monthly_climatology_valid_hours"][:2].tolist() == [23, 1]
        assert int(dataset.variables["full_period_valid_hours"].getValue()) == 24
        full_count = np.asarray(dataset.variables["full_period_count"][:])
        assert int(full_count.sum()) == 24 * 4

    events = pq.read_table(output_root / "high_impact_events.parquet").to_pylist()
    assert events[0]["duration_hours"] == 1
    assert events[0]["max_affected_grid_cells"] == 2
    with netCDF4.Dataset(output_root / "maps/freezing_rain_frequency.nc") as dataset:
        assert dataset.variables["combined_freezing_rain_count"][:].tolist() == [1, 1, 0, 0]


def test_output_writer_uses_exact_four_bit_packing(tmp_path: Path) -> None:
    manifest_path, _, output_root = _analysis_manifest(tmp_path)
    analysis.run_analysis_task(manifest_path=manifest_path, index=0)
    compact = output_root / "compact/ICON-REA-L-CH1/2010/ptype_ICON-REA-L-CH1_201001.grib2"
    with compact.open("rb") as handle:
        message_id = eccodes.codes_grib_new_from_file(handle)
    try:
        assert eccodes.codes_get(message_id, "bitsPerValue") == PTYPE_BITS_PER_VALUE
        assert eccodes.codes_get(message_id, "binaryScaleFactor") == 0
        np.testing.assert_array_equal(eccodes.codes_get_values(message_id), np.array([3, 13, 0, 1]))
    finally:
        eccodes.codes_release(message_id)
