from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from precip_type_diag.operational import (
    OperationalConfig,
    config_for_model,
    process_member_run,
    resolve_run_id,
    run_operational,
)


class FakeField:
    def __init__(self, values: np.ndarray):
        self._values = np.asarray(values)

    def to_numpy(self, flatten: bool = False):
        if flatten:
            return self._values.reshape(-1)
        return self._values


class FakeFieldSet:
    def __init__(self, mapping: dict[int, list[np.ndarray]]):
        self._mapping = {key: [FakeField(values) for values in value] for key, value in mapping.items()}

    def sel(self, *, paramId: int):
        return self._mapping.get(paramId, [])


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def _fake_sources(member_dir: Path) -> dict[str, FakeFieldSet]:
    return {
        str(member_dir / "lfff00000000c"): FakeFieldSet(
            {500008: [np.ones((2, 2)) * 3000.0, np.ones((2, 2)) * 1500.0, np.zeros((2, 2))]}
        ),
        str(member_dir / "lfff00000000"): FakeFieldSet(
            {
                500014: [np.ones((2, 2)) * level for level in (270.0, 268.0)],
                500001: [np.ones((2, 2)) * level for level in (80000.0, 90000.0)],
                500035: [np.ones((2, 2)) * level for level in (0.002, 0.003)],
                500041: [np.ones((2, 2)) * 0.5],
                500010: [np.ones((2, 2)) * 269.15],
            }
        ),
        str(member_dir / "lfff00010000"): FakeFieldSet(
            {
                500014: [np.ones((2, 2)) * level for level in (270.0, 268.0)],
                500001: [np.ones((2, 2)) * level for level in (80000.0, 90000.0)],
                500035: [np.ones((2, 2)) * level for level in (0.002, 0.003)],
                500041: [np.ones((2, 2)) * 1.0],
                500010: [np.ones((2, 2)) * 269.15],
            }
        ),
        str(member_dir / "lfff00020000"): FakeFieldSet(
            {
                500014: [np.ones((2, 2)) * level for level in (270.0, 268.0)],
                500001: [np.ones((2, 2)) * level for level in (80000.0, 90000.0)],
                500035: [np.ones((2, 2)) * level for level in (0.002, 0.003)],
                500041: [np.ones((2, 2)) * 2.5],
                500010: [np.ones((2, 2)) * 269.15],
            }
        ),
    }


def _fake_scan_factory(member_dir: Path):
    fake_sources = _fake_sources(member_dir)

    def fake_scan(path: Path, required_fields, *, level_start_by_name=None, capture_template_for=None):
        fieldset = fake_sources[str(path)]
        outputs: dict[str, np.ndarray] = {}
        level_start_by_name = level_start_by_name or {}
        for short_name in required_fields:
            param_id = {
                "T": 500014,
                "P": 500001,
                "QV": 500035,
                "TOT_PREC": 500041,
                "T_G": 500010,
                "HHL": 500008,
            }[short_name]
            selected = fieldset.sel(paramId=param_id)
            if short_name in {"T", "P", "QV", "HHL"}:
                values = np.stack([field.to_numpy(flatten=False) for field in selected], axis=0)
                start = level_start_by_name.get(short_name, 0)
                outputs[short_name] = values[start:]
            else:
                outputs[short_name] = selected[0].to_numpy(flatten=False)
        template = object() if capture_template_for is not None else None
        return outputs, template

    return fake_scan


def test_resolve_run_id_picks_latest(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    for run in ["26042312_741", "26042318_741"]:
        (root / "ICON-CH2-EPS" / "FCST_RING" / run / "icon").mkdir(parents=True)

    assert resolve_run_id("ICON-CH2-EPS", root, "latest") == "26042318_741"


def test_process_member_run_reuses_previous_tot_prec_from_prior_step(monkeypatch, tmp_path: Path) -> None:
    member_dir = tmp_path / "icon" / "000"
    _touch(member_dir / "lfff00000000c")
    _touch(member_dir / "lfff00010000")
    _touch(member_dir / "lfff00020000")
    fake_sources = _fake_sources(member_dir)
    writes: list[tuple[Path, np.ndarray]] = []

    monkeypatch.setattr("precip_type_diag.operational.bootstrap_eccodes_definitions", lambda: "")
    monkeypatch.setattr("precip_type_diag.operational._is_valid_output", lambda path: False)
    monkeypatch.setattr("precip_type_diag.operational._scan_grib_file_fast", _fake_scan_factory(member_dir))
    selection_calls: list[float] = []

    def fake_selection(full_half_level_height_m, vertical_cutoff_m):
        selection_calls.append(vertical_cutoff_m)

        class Selection:
            full_level_start = 0
            half_level_start = 0
            retained_full_levels = 2

        return Selection()

    def fake_diag(grid_inputs, *, chunk_size=4096, precip_mask_threshold_mm=0.0):
        return np.where(grid_inputs.total_precip_mm > 1.0, 5, 1).astype(np.int32)

    def fake_write(template_field, categorical_codes, destination):
        writes.append((destination, np.asarray(categorical_codes)))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"GRIB")
        return destination

    monkeypatch.setattr("precip_type_diag.operational.diagnose_grid_categorical", fake_diag)
    monkeypatch.setattr("precip_type_diag.operational.write_output_grib", fake_write)
    monkeypatch.setattr("precip_type_diag.operational.derive_vertical_level_selection", fake_selection)

    summary = process_member_run(
        member_dir=member_dir,
        member="000",
        output_dir=tmp_path / "out",
        precip_mask_threshold_mm=0.0,
        vertical_cutoff_m=9000.0,
        overwrite=False,
    )

    assert [item["step"] for item in summary["skipped"]] == ["00010000"]
    assert [item["step"] for item in summary["written"]] == ["00020000"]
    assert writes[0][1].shape == (2, 2)
    np.testing.assert_array_equal(writes[0][1], np.full((2, 2), 5, dtype=np.int32))
    assert selection_calls == [9000.0]


def test_process_member_run_skips_existing_valid_outputs(monkeypatch, tmp_path: Path) -> None:
    member_dir = tmp_path / "icon" / "000"
    _touch(member_dir / "lfff00000000c")
    _touch(member_dir / "lfff00000000")
    _touch(member_dir / "lfff00010000")
    fake_sources = _fake_sources(member_dir)
    output_dir = tmp_path / "out"
    existing = output_dir / "000" / "lfff00010000.ptype.grib2"
    _touch(existing)

    monkeypatch.setattr("precip_type_diag.operational.bootstrap_eccodes_definitions", lambda: "")
    monkeypatch.setattr("precip_type_diag.operational._is_valid_output", lambda path: path == existing)
    monkeypatch.setattr("precip_type_diag.operational._scan_grib_file_fast", _fake_scan_factory(member_dir))
    writes: list[Path] = []

    def fake_diag(grid_inputs, *, chunk_size=4096, precip_mask_threshold_mm=0.0):
        return np.ones((2, 2), dtype=np.int32)

    def fake_write(template_field, categorical_codes, destination):
        writes.append(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"GRIB")
        return destination

    monkeypatch.setattr("precip_type_diag.operational.diagnose_grid_categorical", fake_diag)
    monkeypatch.setattr("precip_type_diag.operational.write_output_grib", fake_write)

    summary = process_member_run(
        member_dir=member_dir,
        member="000",
        output_dir=output_dir,
        precip_mask_threshold_mm=0.0,
        overwrite=False,
    )

    assert summary["written"][0]["step"] == "00000000"
    assert summary["skipped"][0]["reason"] == "existing valid output"
    assert writes == [output_dir / "000" / "lfff00000000.ptype.grib2"]


def test_run_operational_writes_summary_for_latest_run(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "cache"
    run_dir = root / "ICON-CH1-EPS" / "FCST_RING" / "26042315_639" / "icon" / "000"
    run_dir.mkdir(parents=True)

    test_config = OperationalConfig(
        model="ICON-CH1-EPS",
        members=("000",),
        input_root=root,
        output_root=tmp_path / "products",
        precip_mask_threshold_mm=0.1,
        max_workers=1,
    )

    monkeypatch.setattr("precip_type_diag.operational.config_for_model", lambda *args, **kwargs: test_config)
    monkeypatch.setattr(
        "precip_type_diag.operational.process_member_run",
        lambda **kwargs: {
            "member": "000",
            "written": [{"step": "00010000", "path": "dummy"}],
            "skipped": [],
            "failed": [],
            "category_counts": {"no_precip": 0, "rain": 4, "freezing_rain": 0, "snow": 0, "ice_pellets": 0, "freezing_drizzle": 0, "freezing_rain_on_ground": 0},
            "runtime_s": 1.0,
        },
    )

    summary = run_operational(model="ICON-CH1-EPS", run="latest")

    summary_path = tmp_path / "products" / "ICON-CH1-EPS" / "26042315_639" / "summary.json"
    assert summary["run"] == "26042315_639"
    assert summary["written_count"] == 1
    assert summary_path.exists()


def test_config_for_model_rejects_invalid_precip_threshold() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        config_for_model("ICON-CH1-EPS", precip_mask_threshold_mm=-0.1)
