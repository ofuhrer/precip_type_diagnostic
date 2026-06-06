"""GRIB definition setup, vertical selection, and output writing."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import eccodes
import numpy as np
from earthkit.data import from_source
from earthkit.data.encoders.grib import GribEncoder

from .constants import PrecipitationTypeCode

ALLOWED_OUTPUT_CODES = frozenset(int(code) for code in PrecipitationTypeCode)


@dataclass(frozen=True)
class GribTemplateMessage:
    message_bytes: bytes
    values_shape: tuple[int, ...]


@dataclass(frozen=True)
class GribFieldMessage:
    values: np.ndarray
    template: GribTemplateMessage


@dataclass(frozen=True)
class VerticalLevelSelection:
    full_level_start: int
    half_level_start: int
    retained_full_levels: int


def _package_definitions_dir() -> Path:
    return Path(__file__).resolve().parent / "definitions"


def _candidate_meteoswiss_definition_dirs() -> list[Path]:
    candidates: list[Path] = []
    env_dir = os.environ.get("PRECIP_TYPE_DIAG_COSMO_DEFS")
    if env_dir:
        candidates.append(Path(env_dir))

    try:
        import eccodes_cosmo_resources

        get_path = getattr(eccodes_cosmo_resources, "get_definitions_path", None)
        if callable(get_path):
            candidates.append(Path(get_path()))
        module_dir = Path(eccodes_cosmo_resources.__file__).resolve().parent
        candidates.extend(sorted(module_dir.glob("definitions.*"), reverse=True))
    except Exception:
        pass

    candidates.extend(
        [
            Path("/tmp/eccodes-cosmo-resources-python/definitions.edzw-2.38.3-1"),
            Path("/tmp/eccodes-cosmo-defs/definitions.edzw-2.38.3-1"),
        ]
    )
    return [candidate for candidate in candidates if candidate.exists()]


def bootstrap_eccodes_definitions() -> str:
    """Prepend MeteoSwiss definitions and the project-local PTYPE overlay."""

    current = eccodes.codes_definition_path()
    local_defs = _package_definitions_dir()
    meteoswiss_defs = _candidate_meteoswiss_definition_dirs()

    if not meteoswiss_defs:
        raise RuntimeError(
            "Could not locate MeteoSwiss ecCodes definitions. "
            "Install eccodes-cosmo-resources-python or set PRECIP_TYPE_DIAG_COSMO_DEFS."
        )

    preferred = [str(local_defs), str(meteoswiss_defs[0])]
    current_parts = [part for part in current.split(":") if part]
    combined_parts: list[str] = []
    for part in [*preferred, *current_parts]:
        if part not in combined_parts:
            combined_parts.append(part)
    combined = ":".join(combined_parts)
    eccodes.codes_set_definitions_path(combined)
    os.environ["ECCODES_DEFINITION_PATH"] = combined
    return combined


def check_precip_mask_threshold_mm(value: float) -> float:
    threshold = float(value)
    if not np.isfinite(threshold):
        raise ValueError(f"precip_mask_threshold_mm must be finite, got {value!r}")
    if threshold < 0.0:
        raise ValueError(f"precip_mask_threshold_mm must be non-negative, got {value!r}")
    return threshold


def _template_shape(template_field) -> tuple[int, ...] | None:
    if isinstance(template_field, GribTemplateMessage):
        return template_field.values_shape
    return None


def _check_categorical_codes(categorical_codes: np.ndarray, expected_shape: tuple[int, ...] | None) -> np.ndarray:
    values = np.asarray(categorical_codes)
    if expected_shape is not None and tuple(values.shape) != tuple(expected_shape):
        raise ValueError(f"categorical_codes shape {values.shape} does not match template shape {expected_shape}")
    if not np.all(np.isfinite(values)):
        raise ValueError("categorical_codes must contain only finite values")
    if not np.all(values == np.rint(values)):
        raise ValueError("categorical_codes must contain integer category codes")

    categorical = values.astype(np.int32)
    invalid_codes = sorted(set(int(value) for value in np.unique(categorical)) - ALLOWED_OUTPUT_CODES)
    if invalid_codes:
        allowed = ", ".join(str(code) for code in sorted(ALLOWED_OUTPUT_CODES))
        invalid = ", ".join(str(code) for code in invalid_codes)
        raise ValueError(f"categorical_codes contain invalid code(s): {invalid}; allowed codes are: {allowed}")
    return categorical


def derive_vertical_level_selection(
    half_level_height_m: np.ndarray,
    vertical_cutoff_m: float | None,
) -> VerticalLevelSelection:
    if vertical_cutoff_m is None:
        return VerticalLevelSelection(
            full_level_start=0,
            half_level_start=0,
            retained_full_levels=int(half_level_height_m.shape[0] - 1),
        )
    cutoff = float(vertical_cutoff_m)
    if not np.isfinite(cutoff):
        raise ValueError(f"vertical_cutoff_m must be finite, got {vertical_cutoff_m!r}")

    half_level_height_m = np.asarray(half_level_height_m, dtype=float)
    if half_level_height_m.ndim < 2:
        raise ValueError(
            "half_level_height_m must have shape (half_level, npoint) or (half_level, y, x), "
            f"got {half_level_height_m.shape}"
        )
    flat = half_level_height_m.reshape(half_level_height_m.shape[0], -1)
    full_level_height_m = 0.5 * (flat[:-1] + flat[1:])
    domain_min_full_level_height_m = np.nanmin(full_level_height_m, axis=1)
    keep_mask = domain_min_full_level_height_m <= cutoff
    full_level_start = int(np.argmax(keep_mask)) if np.any(keep_mask) else 0

    retained_full_levels = int(full_level_height_m.shape[0] - full_level_start)
    if retained_full_levels <= 0:
        raise ValueError(
            f"vertical_cutoff_m={vertical_cutoff_m} removed all full levels; "
            "choose a higher cutoff or disable truncation"
        )

    return VerticalLevelSelection(
        full_level_start=full_level_start,
        half_level_start=full_level_start,
        retained_full_levels=retained_full_levels,
    )


def write_output_grib(
    template_field,
    categorical_codes: np.ndarray,
    destination: Path,
    *,
    expected_shape: tuple[int, ...] | None = None,
) -> Path:
    bootstrap_eccodes_definitions()
    shape = expected_shape if expected_shape is not None else _template_shape(template_field)
    categorical = _check_categorical_codes(categorical_codes, shape)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        if isinstance(template_field, GribTemplateMessage):
            handle_id = eccodes.codes_new_from_message(template_field.message_bytes)
            try:
                eccodes.codes_set(handle_id, "discipline", 0)
                eccodes.codes_set(handle_id, "parameterCategory", 1)
                eccodes.codes_set(handle_id, "parameterNumber", 19)
                eccodes.codes_set(handle_id, "packingType", "grid_simple")
                eccodes.codes_set_values(handle_id, categorical.astype(float).reshape(-1))
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=destination.parent,
                    prefix=f"{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    eccodes.codes_write(handle_id, handle)
            finally:
                eccodes.codes_release(handle_id)
        else:
            encoder = GribEncoder()
            encoded = encoder.encode(
                values=categorical,
                template=template_field,
                metadata={
                    "discipline": 0,
                    "parameterCategory": 1,
                    "parameterNumber": 19,
                    "packingType": "grid_simple",
                },
            )
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f"{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                encoded.to_file(handle)
        os.replace(temp_path, destination)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
    return destination


def read_single_grib_message(path: Path) -> GribFieldMessage:
    """Read one GRIB message and retain its raw bytes as an output template."""

    bootstrap_eccodes_definitions()
    message_bytes: bytes | None = None
    with path.open("rb") as handle:
        message_id = eccodes.codes_grib_new_from_file(handle)
        if message_id is None:
            raise ValueError(f"{path} does not contain a GRIB message")
        try:
            message_bytes = eccodes.codes_get_message(message_id)
        finally:
            eccodes.codes_release(message_id)

        extra_message_id = eccodes.codes_grib_new_from_file(handle)
        if extra_message_id is not None:
            try:
                raise ValueError(f"{path} contains more than one GRIB message")
            finally:
                eccodes.codes_release(extra_message_id)

    fields = list(from_source("file", str(path)))
    if len(fields) != 1:
        raise ValueError(f"{path} contains {len(fields)} GRIB fields; expected one")
    values = np.asarray(fields[0].to_numpy(flatten=False))
    return GribFieldMessage(
        values=values,
        template=GribTemplateMessage(
            message_bytes=message_bytes,
            values_shape=tuple(values.shape),
        ),
    )


def read_categorical_grib(path: Path) -> GribFieldMessage:
    field = read_single_grib_message(path)
    categorical = _check_categorical_codes(field.values, tuple(field.values.shape))
    return GribFieldMessage(values=categorical, template=field.template)

