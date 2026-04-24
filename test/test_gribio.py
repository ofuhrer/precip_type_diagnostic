from __future__ import annotations

import os
from pathlib import Path
import time

import numpy as np
import pytest
from earthkit.data import from_source
from earthkit.data.encoders.grib import GribEncoder

from precip_type_diag.constants import INPUT_PARAM_IDS, OUTPUT_PARAM_ID, OUTPUT_SHORT_NAME, REQUIRED_INPUT_FIELDS
from precip_type_diag.gribio import (
    GribTemplateMessage,
    MemberHourJob,
    MissingFieldError,
    MissingFileError,
    _scan_grib_file_fast,
    _prune_grib_index_cache,
    _scan_grib_file_sequential,
    _previous_step,
    bootstrap_eccodes_definitions,
    build_jobs,
    derive_vertical_level_selection,
    load_member_hour,
    load_member_hour_fast,
    load_member_hour_legacy,
    parse_hours,
    parse_members,
    report_for_run,
    validate_precip_mask_threshold_mm,
    write_output_grib,
)

REAL_FIXTURE_DIR = Path("test/fixtures/real_icon_ch2_eps")
REAL_FIXTURE_DIR_CH1 = Path("test/fixtures/real_icon_ch1_eps")


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


def test_missing_previous_file_fails_for_explicit_request(tmp_path: Path) -> None:
    run_dir = tmp_path / "icon"
    member_dir = run_dir / "000"
    member_dir.mkdir(parents=True)
    (member_dir / "lfff00000000c").write_bytes(b"")
    (member_dir / "lfff00010000").write_bytes(b"")

    with pytest.raises(MissingFileError):
        build_jobs(run_dir, members=["000"], hours=["00010000"])


def test_previous_step_rolls_across_day_boundary() -> None:
    assert _previous_step("01000000") == "00230000"
    assert _previous_step("04180000") == "04170000"
    assert _previous_step("00000000") is None


def test_parse_members_and_hours_validate_explicit_values() -> None:
    assert parse_members("000,010") == ["000", "010"]
    assert parse_hours("00010000,04180000") == ["00010000", "04180000"]

    with pytest.raises(ValueError, match="Invalid member identifier"):
        parse_members("0,010")
    with pytest.raises(ValueError, match="Invalid ICON step string"):
        parse_hours("1")


def test_precip_mask_threshold_validation_rejects_negative_or_non_finite() -> None:
    assert validate_precip_mask_threshold_mm(0.25) == 0.25

    with pytest.raises(ValueError, match="non-negative"):
        validate_precip_mask_threshold_mm(-0.1)
    with pytest.raises(ValueError, match="finite"):
        validate_precip_mask_threshold_mm(float("nan"))


def test_bootstrap_eccodes_definitions_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("precip_type_diag.gribio._package_definitions_dir", lambda: Path("/defs/local"))
    monkeypatch.setattr("precip_type_diag.gribio._candidate_meteoswiss_definition_dirs", lambda: [Path("/defs/ms")])
    monkeypatch.setattr("precip_type_diag.gribio.eccodes.codes_definition_path", lambda: "/defs/local:/defs/ms:/eccodes/base")
    monkeypatch.setattr("precip_type_diag.gribio.eccodes.codes_set_definitions_path", calls.append)

    combined = bootstrap_eccodes_definitions()

    assert combined == "/defs/local:/defs/ms:/eccodes/base"
    assert calls == ["/defs/local:/defs/ms:/eccodes/base"]


def test_grib_index_cache_prunes_files_older_than_max_age(tmp_path: Path) -> None:
    old_index = tmp_path / "old.idx"
    fresh_index = tmp_path / "fresh.idx"
    unrelated = tmp_path / "old.tmp"
    old_index.write_bytes(b"old")
    fresh_index.write_bytes(b"fresh")
    unrelated.write_bytes(b"tmp")

    now = time.time()
    old_time = now - 11 * 24 * 60 * 60
    os.utime(old_index, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))

    _prune_grib_index_cache(tmp_path, max_age_days=10)

    assert not old_index.exists()
    assert fresh_index.exists()
    assert unrelated.exists()


def test_fast_scan_discards_bad_index_cache_before_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "input.grib2"
    path.write_bytes(b"fake")
    discarded: list[Path] = []

    def broken_indexed(*args, **kwargs):
        raise MissingFieldError("bad index")

    def fallback_scan(*args, **kwargs):
        return {"TOT_PREC": np.array([1.0])}, None

    monkeypatch.setattr("precip_type_diag.gribio._scan_grib_file_indexed", broken_indexed)
    monkeypatch.setattr("precip_type_diag.gribio._scan_grib_file_sequential", fallback_scan)
    monkeypatch.setattr("precip_type_diag.gribio._discard_grib_index_cache", discarded.append)

    fields, template = _scan_grib_file_fast(path, ("TOT_PREC",))

    np.testing.assert_array_equal(fields["TOT_PREC"], np.array([1.0]))
    assert template is None
    assert discarded == [path]


def test_fast_scan_skips_discarded_3d_levels_before_decoding_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "input.grib2"
    path.write_bytes(b"fake")
    messages = [
        {"paramId": INPUT_PARAM_IDS["T"], "values": np.array([270.0]), "name": "T0"},
        {"paramId": INPUT_PARAM_IDS["T"], "values": np.array([271.0]), "name": "T1"},
        {"paramId": INPUT_PARAM_IDS["T"], "values": np.array([272.0]), "name": "T2"},
        {"paramId": INPUT_PARAM_IDS["TOT_PREC"], "values": np.array([1.0]), "name": "TOT_PREC"},
    ]
    pending = iter(messages)
    decoded: list[str] = []
    released: list[str] = []

    def fake_codes_grib_new_from_file(handle):
        return next(pending, None)

    def fake_codes_get_long(message, key: str) -> int:
        assert key == "paramId"
        return int(message["paramId"])

    def fake_codes_get_values(message):
        decoded.append(str(message["name"]))
        return message["values"]

    def fake_codes_release(message) -> None:
        released.append(str(message["name"]))

    monkeypatch.setattr("precip_type_diag.gribio.eccodes.codes_grib_new_from_file", fake_codes_grib_new_from_file)
    monkeypatch.setattr("precip_type_diag.gribio.eccodes.codes_get_long", fake_codes_get_long)
    monkeypatch.setattr("precip_type_diag.gribio.eccodes.codes_get_values", fake_codes_get_values)
    monkeypatch.setattr("precip_type_diag.gribio.eccodes.codes_release", fake_codes_release)
    monkeypatch.setattr("precip_type_diag.gribio._reshape_message_values", lambda message, values: values)

    fields, template = _scan_grib_file_sequential(
        path,
        ("T", "TOT_PREC"),
        level_start_by_name={"T": 2},
    )

    assert template is None
    np.testing.assert_array_equal(fields["T"], np.array([[272.0]]))
    np.testing.assert_array_equal(fields["TOT_PREC"], np.array([1.0]))
    assert decoded == ["T2", "TOT_PREC"]
    assert released == ["T0", "T1", "T2", "TOT_PREC"]


def test_missing_required_input_field_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    current_path = Path("/tmp/current-missing")
    constants_path = Path("/tmp/constants-missing")
    fake_sources = {
        str(current_path): FakeFieldSet(
            {
                500014: [np.ones((2, 2)) * level for level in (270.0, 268.0)],
                500001: [np.ones((2, 2)) * level for level in (80000.0, 90000.0)],
                500035: [np.ones((2, 2)) * level for level in (0.002, 0.003)],
                500041: [np.ones((2, 2)) * 4.0],
            }
        ),
        str(constants_path): FakeFieldSet({500008: [np.ones((2, 2)) * 3000.0, np.ones((2, 2)) * 1500.0, np.zeros((2, 2))]}),
    }

    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")
    monkeypatch.setattr("precip_type_diag.gribio.from_source", lambda source, path: fake_sources[path])

    with pytest.raises(MissingFieldError):
        load_member_hour_legacy(
            MemberHourJob(
                member="000",
                step="00000000",
                current_file=current_path,
                previous_file=None,
                constants_file=constants_path,
            ),
        )


def test_report_for_run_lists_required_fields(tmp_path: Path) -> None:
    run_dir = tmp_path / "icon"
    (run_dir / "000").mkdir(parents=True)
    report = report_for_run(run_dir)
    assert set(report["required_fields"]) == set(REQUIRED_INPUT_FIELDS)


def test_load_member_hour_with_mocked_fieldsets(monkeypatch: pytest.MonkeyPatch) -> None:
    current_path = Path("/tmp/current")
    previous_path = Path("/tmp/previous")
    constants_path = Path("/tmp/constants")

    fake_sources = {
        str(current_path): FakeFieldSet(
            {
                500014: [np.ones((2, 2)) * level for level in (270.0, 268.0)],
                500001: [np.ones((2, 2)) * level for level in (80000.0, 90000.0)],
                500035: [np.ones((2, 2)) * level for level in (0.002, 0.003)],
                500041: [np.ones((2, 2)) * 4.0],
                500010: [np.ones((2, 2)) * 269.15],
            }
        ),
        str(previous_path): FakeFieldSet({500041: [np.ones((2, 2)) * 1.5]}),
        str(constants_path): FakeFieldSet({500008: [np.ones((2, 2)) * 3000.0, np.ones((2, 2)) * 1500.0, np.zeros((2, 2))]}),
    }

    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")
    monkeypatch.setattr("precip_type_diag.gribio.from_source", lambda source, path: fake_sources[path])

    grid_inputs, template, selection = load_member_hour_legacy(
        MemberHourJob(
            member="000",
            step="00010000",
            current_file=current_path,
            previous_file=previous_path,
            constants_file=constants_path,
        ),
    )

    assert grid_inputs.temperature_k.shape == (2, 2, 2)
    assert grid_inputs.half_level_height_m.shape == (3, 2, 2)
    np.testing.assert_allclose(grid_inputs.total_precip_mm, np.full((2, 2), 2.5))
    assert isinstance(template.field, FakeField)
    assert selection.retained_full_levels == 2


def test_load_member_hour_reuses_fieldsets(monkeypatch: pytest.MonkeyPatch) -> None:
    current_path = Path("/tmp/current-cache")
    previous_path = Path("/tmp/previous-cache")
    constants_path = Path("/tmp/constants-cache")

    fake_sources = {
        str(current_path): FakeFieldSet(
            {
                500014: [np.ones((2, 2)) * level for level in (270.0, 268.0)],
                500001: [np.ones((2, 2)) * level for level in (80000.0, 90000.0)],
                500035: [np.ones((2, 2)) * level for level in (0.002, 0.003)],
                500041: [np.ones((2, 2)) * 4.0],
                500010: [np.ones((2, 2)) * 269.15],
            }
        ),
        str(previous_path): FakeFieldSet({500041: [np.ones((2, 2)) * 1.5]}),
        str(constants_path): FakeFieldSet({500008: [np.ones((2, 2)) * 3000.0, np.ones((2, 2)) * 1500.0, np.zeros((2, 2))]}),
    }
    open_counts: dict[str, int] = {}

    def fake_from_source(source, path):
        assert source == "file"
        open_counts[path] = open_counts.get(path, 0) + 1
        return fake_sources[path]

    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")
    monkeypatch.setattr("precip_type_diag.gribio.from_source", fake_from_source)

    load_member_hour_legacy(
        MemberHourJob(
            member="000",
            step="00010000",
            current_file=current_path,
            previous_file=previous_path,
            constants_file=constants_path,
        ),
    )

    assert open_counts == {
        str(current_path): 1,
        str(previous_path): 1,
        str(constants_path): 1,
    }


def test_load_member_hour_skips_previous_fieldset_when_not_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    current_path = Path("/tmp/current-no-prev")
    constants_path = Path("/tmp/constants-no-prev")

    fake_sources = {
        str(current_path): FakeFieldSet(
            {
                500014: [np.ones((2, 2)) * level for level in (270.0, 268.0)],
                500001: [np.ones((2, 2)) * level for level in (80000.0, 90000.0)],
                500035: [np.ones((2, 2)) * level for level in (0.002, 0.003)],
                500041: [np.ones((2, 2)) * 4.0],
                500010: [np.ones((2, 2)) * 269.15],
            }
        ),
        str(constants_path): FakeFieldSet({500008: [np.ones((2, 2)) * 3000.0, np.ones((2, 2)) * 1500.0, np.zeros((2, 2))]}),
    }
    open_counts: dict[str, int] = {}

    def fake_from_source(source, path):
        assert source == "file"
        open_counts[path] = open_counts.get(path, 0) + 1
        return fake_sources[path]

    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")
    monkeypatch.setattr("precip_type_diag.gribio.from_source", fake_from_source)

    load_member_hour_legacy(
        MemberHourJob(
            member="000",
            step="00000000",
            current_file=current_path,
            previous_file=None,
            constants_file=constants_path,
        ),
    )

    assert open_counts == {
        str(current_path): 1,
        str(constants_path): 1,
    }


def test_load_member_hour_accepts_unstructured_horizontal_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    current_path = Path("/tmp/current-unstructured")
    previous_path = Path("/tmp/previous-unstructured")
    constants_path = Path("/tmp/constants-unstructured")

    fake_sources = {
        str(current_path): FakeFieldSet(
            {
                500014: [np.full(3, level) for level in (270.0, 268.0)],
                500001: [np.full(3, level) for level in (80000.0, 90000.0)],
                500035: [np.full(3, level) for level in (0.002, 0.003)],
                500041: [np.full(3, 4.0)],
                500010: [np.full(3, 269.15)],
            }
        ),
        str(previous_path): FakeFieldSet({500041: [np.full(3, 1.5)]}),
        str(constants_path): FakeFieldSet({500008: [np.full(3, 3000.0), np.full(3, 1500.0), np.zeros(3)]}),
    }

    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")
    monkeypatch.setattr("precip_type_diag.gribio.from_source", lambda source, path: fake_sources[path])

    grid_inputs, _, _ = load_member_hour_legacy(
        MemberHourJob(
            member="000",
            step="04180000",
            current_file=current_path,
            previous_file=previous_path,
            constants_file=constants_path,
        ),
    )

    from precip_type_diag.grid import diagnose_grid

    categorical, diagnostics = diagnose_grid(grid_inputs)
    assert categorical.shape == (3,)
    assert len(diagnostics) == 3


def test_categorical_fast_path_matches_full_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    current_path = Path("/tmp/current-fast")
    previous_path = Path("/tmp/previous-fast")
    constants_path = Path("/tmp/constants-fast")

    fake_sources = {
        str(current_path): FakeFieldSet(
            {
                500014: [np.full(3, level) for level in (270.0, 268.0)],
                500001: [np.full(3, level) for level in (80000.0, 90000.0)],
                500035: [np.full(3, level) for level in (0.002, 0.003)],
                500041: [np.array([4.0, 0.0, 2.0])],
                500010: [np.full(3, 269.15)],
            }
        ),
        str(previous_path): FakeFieldSet({500041: [np.zeros(3)]}),
        str(constants_path): FakeFieldSet({500008: [np.full(3, 3000.0), np.full(3, 1500.0), np.zeros(3)]}),
    }

    monkeypatch.setattr("precip_type_diag.gribio.bootstrap_eccodes_definitions", lambda: "")
    monkeypatch.setattr("precip_type_diag.gribio.from_source", lambda source, path: fake_sources[path])

    grid_inputs, _, _ = load_member_hour_legacy(
        MemberHourJob(
            member="000",
            step="04180000",
            current_file=current_path,
            previous_file=previous_path,
            constants_file=constants_path,
        ),
    )

    from precip_type_diag.grid import diagnose_grid, diagnose_grid_categorical

    categorical_full, _ = diagnose_grid(grid_inputs)
    categorical_fast = diagnose_grid_categorical(grid_inputs, chunk_size=2)
    np.testing.assert_array_equal(categorical_fast, categorical_full)


def test_categorical_fast_path_skips_dry_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    from precip_type_diag.grid import GridInputs, diagnose_grid_categorical

    call_count = {"value": 0}

    def fake_calculate_thermodynamics(temperature_k, specific_humidity, pressure_pa):
        zeros = np.zeros_like(temperature_k, dtype=float)
        from precip_type_diag.profile import ThermodynamicColumn

        return ThermodynamicColumn(temperature_c=zeros, wet_bulb_c=zeros, relative_humidity_ice_pct=zeros)

    def fake_kernel(
        temperature_c_2d,
        wet_bulb_c_2d,
        relative_humidity_ice_pct_2d,
        full_level_height_m_2d,
        total_precip_mm,
        ground_temperature_c,
        precip_mask_threshold_mm,
    ):
        call_count["value"] += 1
        return np.ones(total_precip_mm.shape, dtype=np.int32)

    monkeypatch.setattr("precip_type_diag.grid.calculate_thermodynamics", fake_calculate_thermodynamics)
    monkeypatch.setattr("precip_type_diag.grid.diagnose_grid_categorical_numba_kernel", fake_kernel)

    categorical = diagnose_grid_categorical(
        GridInputs(
            temperature_k=np.ones((2, 4)) * 270.0,
            pressure_pa=np.ones((2, 4)) * 80000.0,
            specific_humidity=np.ones((2, 4)) * 0.002,
            half_level_height_m=np.array(
                [
                    [3000.0, 3000.0, 3000.0, 3000.0],
                    [1500.0, 1500.0, 1500.0, 1500.0],
                    [0.0, 0.0, 0.0, 0.0],
                ]
            ),
            total_precip_mm=np.array([0.0, 1.0, 0.0, 2.0]),
            ground_temperature_c=np.array([-1.0, -1.0, -1.0, -1.0]),
        ),
        chunk_size=1,
    )

    np.testing.assert_array_equal(categorical, np.array([0, 1, 0, 1], dtype=np.int32))
    assert call_count["value"] == 2


def test_categorical_fast_path_treats_negative_hourly_precipitation_as_dry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precip_type_diag.grid import GridInputs, diagnose_grid_categorical

    def fail_if_called(*args, **kwargs):
        raise AssertionError("negative precipitation columns should not reach the numba kernel")

    monkeypatch.setattr("precip_type_diag.grid.diagnose_grid_categorical_numba_kernel", fail_if_called)

    categorical = diagnose_grid_categorical(
        GridInputs(
            temperature_k=np.ones((2, 3)) * 270.0,
            pressure_pa=np.ones((2, 3)) * 80000.0,
            specific_humidity=np.ones((2, 3)) * 0.002,
            half_level_height_m=np.array(
                [
                    [3000.0, 3000.0, 3000.0],
                    [1500.0, 1500.0, 1500.0],
                    [0.0, 0.0, 0.0],
                ]
            ),
            total_precip_mm=np.array([-0.2, -1.0, 0.0]),
            ground_temperature_c=np.array([-1.0, -1.0, -1.0]),
        )
    )

    np.testing.assert_array_equal(categorical, np.array([0, 0, 0], dtype=np.int32))


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


def test_real_icon_ch2_eps_fixture_identity() -> None:
    expected = {
        "lfff00000000c": 53391272,
        "lfff04170000": 775954375,
        "lfff04180000": 775386121,
    }

    missing = [name for name in expected if not (REAL_FIXTURE_DIR / name).exists()]
    if missing:
        pytest.skip(f"Real CH2-EPS fixture files are not present: {missing}")

    for name, size in expected.items():
        path = REAL_FIXTURE_DIR / name
        assert path.stat().st_size == size
        with path.open("rb") as handle:
            assert handle.read(4) == b"GRIB"


def test_real_icon_ch2_fast_reader_matches_legacy_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    required = ["lfff00000000c", "lfff04170000", "lfff04180000"]
    if any(not (REAL_FIXTURE_DIR / name).exists() for name in required):
        pytest.skip("Real CH2-EPS fixture triplet is not fully present")

    job = MemberHourJob(
        member="000",
        step="04180000",
        current_file=REAL_FIXTURE_DIR / "lfff04180000",
        previous_file=REAL_FIXTURE_DIR / "lfff04170000",
        constants_file=REAL_FIXTURE_DIR / "lfff00000000c",
    )
    try:
        bootstrap_eccodes_definitions()
        legacy_inputs, _, legacy_selection = load_member_hour_legacy(job)
        fast_inputs, fast_template, fast_selection = load_member_hour(job)
    except (MissingFieldError, RuntimeError):
        pytest.skip("Real CH2-EPS decoding is unavailable in this interpreter")

    assert isinstance(fast_template, GribTemplateMessage)
    assert fast_selection == legacy_selection
    np.testing.assert_allclose(fast_inputs.temperature_k, legacy_inputs.temperature_k)
    np.testing.assert_allclose(fast_inputs.pressure_pa, legacy_inputs.pressure_pa)
    np.testing.assert_allclose(fast_inputs.specific_humidity, legacy_inputs.specific_humidity)
    np.testing.assert_allclose(fast_inputs.half_level_height_m, legacy_inputs.half_level_height_m)
    np.testing.assert_allclose(fast_inputs.total_precip_mm, legacy_inputs.total_precip_mm)
    np.testing.assert_allclose(fast_inputs.ground_temperature_c, legacy_inputs.ground_temperature_c)


def test_real_icon_ch2_vertical_cutoff_reduces_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    required = ["lfff00000000c", "lfff04170000", "lfff04180000"]
    if any(not (REAL_FIXTURE_DIR / name).exists() for name in required):
        pytest.skip("Real CH2-EPS fixture triplet is not fully present")

    try:
        bootstrap_eccodes_definitions()
        _, _, selection = load_member_hour_fast(
            MemberHourJob(
                member="000",
                step="04180000",
                current_file=REAL_FIXTURE_DIR / "lfff04180000",
                previous_file=REAL_FIXTURE_DIR / "lfff04170000",
                constants_file=REAL_FIXTURE_DIR / "lfff00000000c",
            ),
            vertical_cutoff_m=12000.0,
        )
    except (MissingFieldError, RuntimeError):
        pytest.skip("Real CH2-EPS decoding is unavailable in this interpreter")

    assert selection.retained_full_levels == 67
    assert selection.full_level_start == 13


def test_output_grib_metadata_is_stable_with_fast_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    required = ["lfff00000000c", "lfff04170000", "lfff04180000"]
    if any(not (REAL_FIXTURE_DIR / name).exists() for name in required):
        pytest.skip("Real CH2-EPS fixture triplet is not fully present")

    try:
        bootstrap_eccodes_definitions()
        grid_inputs, template, _ = load_member_hour(
            MemberHourJob(
                member="000",
                step="04180000",
                current_file=REAL_FIXTURE_DIR / "lfff04180000",
                previous_file=REAL_FIXTURE_DIR / "lfff04170000",
                constants_file=REAL_FIXTURE_DIR / "lfff00000000c",
            ),
        )
    except (MissingFieldError, RuntimeError):
        pytest.skip("Real CH2-EPS decoding is unavailable in this interpreter")

    output_path = tmp_path / "ptype-fast-template.grib2"
    write_output_grib(template, np.where(grid_inputs.total_precip_mm > 0.0, 1, 0).astype(np.int32), output_path)

    output_field = from_source("file", str(output_path))[0]
    assert output_field.metadata("paramId") in {OUTPUT_PARAM_ID, 260015}
    assert output_field.metadata("shortName") in {OUTPUT_SHORT_NAME, "ptype"}
    assert output_field.metadata("step") == 114


def test_real_icon_ch1_eps_fixture_smoke() -> None:
    required = ["lfff00010000", "lfff00000000c"]
    if any(not (REAL_FIXTURE_DIR_CH1 / name).exists() for name in required):
        pytest.skip("Real CH1-EPS fixture pair is not fully present")

    bootstrap_eccodes_definitions()

    selected = from_source("file", str(REAL_FIXTURE_DIR_CH1 / "lfff00010000")).sel(paramId=500041)
    if len(selected) == 0:
        pytest.skip("Real CH1-EPS fixture does not expose TOT_PREC with local definitions")
    field = selected[0]
    assert field.metadata("shortName") == "TOT_PREC"
    assert field.metadata("step") == 1
