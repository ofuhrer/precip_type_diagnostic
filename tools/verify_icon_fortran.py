#!/usr/bin/env python3
"""Compare the ICON-like Python reference against the pinned ICON Fortran source.

The verifier obtains ``mo_diag_precip_type.f90`` with ``git show`` from an ICON
checkout, compiles that exact blob with small dependency stubs, and diagnoses
the same deterministic columns in Fortran and Python.  This intentionally does
not vendor or translate the upstream implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from precip_type_diag.constants import ICON_REFERENCE_COMMIT
from precip_type_diag.icon_profile import (
    KELVIN_OFFSET,
    RDV,
    SATURATION_B1,
    SATURATION_WATER_B2,
    SATURATION_WATER_B4,
    IconColumnProfile,
    SurfacePrecipitationRates,
    diagnose_icon_column,
    icon_precip_amount_threshold,
)

ICON_MODULE_PATH = "src/atm_phy_nwp/mo_diag_precip_type.f90"
DEFAULT_VECTOR_PATH = Path(__file__).resolve().parents[1] / "test" / "data" / "icon_fortran_reference.json"


@dataclass(frozen=True)
class ReferenceCase:
    name: str
    temperature_k: list[float]
    pressure_pa: list[float]
    specific_humidity: list[float]
    full_level_height_m: list[float]
    half_level_height_m: list[float]
    total_precip_mm: float
    previous_total_precip_mm: float
    ground_temperature_k: float
    rain_rate: float = 0.0
    snow_rate: float = 0.0
    graupel_rate: float = 0.0
    hail_rate: float = 0.0


def _water_saturation_specific_humidity(temperature_k: np.ndarray, pressure_pa: np.ndarray) -> np.ndarray:
    saturation_pressure = SATURATION_B1 * np.exp(
        SATURATION_WATER_B2 * (temperature_k - KELVIN_OFFSET) / (temperature_k - SATURATION_WATER_B4)
    )
    return saturation_pressure * RDV / (pressure_pa - saturation_pressure * (1.0 - RDV))


def _case(
    name: str,
    temperature_c: list[float],
    *,
    ground_temperature_c: float | None = None,
    total_precip_mm: float = 1.0,
    previous_total_precip_mm: float = 0.0,
    rain_rate: float = 0.0,
    snow_rate: float = 0.0,
    graupel_rate: float = 0.0,
    hail_rate: float = 0.0,
) -> ReferenceCase:
    nlev = len(temperature_c)
    full_heights = np.linspace(11500.0, 350.0, nlev)
    half_heights = np.empty(nlev + 1)
    half_heights[1:-1] = 0.5 * (full_heights[:-1] + full_heights[1:])
    half_heights[0] = full_heights[0] + 0.5 * (full_heights[0] - full_heights[1])
    half_heights[-1] = full_heights[-1] - 0.5 * (full_heights[-2] - full_heights[-1])
    pressure = np.linspace(25000.0, 97000.0, nlev)
    temperature = np.asarray(temperature_c) + KELVIN_OFFSET
    humidity = _water_saturation_specific_humidity(temperature, pressure)
    return ReferenceCase(
        name=name,
        temperature_k=temperature.tolist(),
        pressure_pa=pressure.tolist(),
        specific_humidity=humidity.tolist(),
        full_level_height_m=full_heights.tolist(),
        half_level_height_m=half_heights.tolist(),
        total_precip_mm=total_precip_mm,
        previous_total_precip_mm=previous_total_precip_mm,
        ground_temperature_k=(temperature_c[-1] if ground_temperature_c is None else ground_temperature_c) + KELVIN_OFFSET,
        rain_rate=rain_rate,
        snow_rate=snow_rate,
        graupel_rate=graupel_rate,
        hail_rate=hail_rate,
    )


def reference_cases() -> list[ReferenceCase]:
    """Return deterministic columns covering core, trace, reset, and refinement branches."""

    snow = [-16.0] * 16
    wet_snow = [-16.0] * 11 + [-8.0, -4.0, -1.0, -0.2, -0.2]
    return [
        _case("trace_mask_equal_threshold", [4.0] * 16, total_precip_mm=0.01),
        _case("negative_accumulation_reset", [4.0] * 16, total_precip_mm=0.5, previous_total_precip_mm=4.0),
        _case("snow", snow),
        _case("freezing_drizzle", [-8.5] * 16),
        _case("rain", [4.0] * 16),
        _case("freezing_rain_on_ground", [4.0] * 16, ground_temperature_c=-5.0),
        _case("freezing_rain", [-16.0] * 12 + [1.0] * 2 + [-1.0] * 2),
        _case("ice_pellets", [-16.0] * 6 + [4.0] * 5 + [-5.0] * 5),
        _case("large_refreezing_suppresses_ice_pellets", [-16.0] * 5 + [5.0] * 4 + [-10.0] * 7),
        _case("wet_snow_refinement", wet_snow, snow_rate=3.0e-5),
        _case("mixed_refinement", snow, rain_rate=2.0e-5, snow_rate=3.0e-5),
        _case("graupel_refinement", snow, snow_rate=1.0e-5, graupel_rate=4.0e-5),
        _case("hail_refinement", snow, snow_rate=1.0e-5, hail_rate=4.0e-5),
    ]


DEPENDENCY_STUBS = """
module mo_kind
  use iso_fortran_env, only: real64
  implicit none
  integer, parameter :: wp = real64
end module mo_kind

module mo_exception
  implicit none
  character(len=1024) :: message_text
contains
  subroutine finish(routine, message)
    character(len=*), intent(in) :: routine, message
    write(*, '(a,2a)') trim(routine), ': ', trim(message)
    error stop 2
  end subroutine finish
end module mo_exception

module mo_lookup_tables_constants
  use mo_kind, only: wp
  implicit none
  real(wp), parameter :: c1es=610.78_wp, c3les=17.502_wp, c4les=32.19_wp
end module mo_lookup_tables_constants

module mo_physical_constants
  use mo_kind, only: wp
  implicit none
  real(wp), parameter :: grav=9.80665_wp, tmelt=273.15_wp
end module mo_physical_constants

module mo_thdyn_functions
  use mo_kind, only: wp
  use mo_physical_constants, only: tmelt
  implicit none
contains
  pure real(wp) function sat_pres_ice(temperature_k) result(value)
    real(wp), intent(in) :: temperature_k
    value = 610.78_wp*exp(22.587_wp*(temperature_k-tmelt)/(temperature_k-(-0.7_wp)))
  end function sat_pres_ice
end module mo_thdyn_functions

module mo_util_phys
  use mo_kind, only: wp
  implicit none
contains
  pure real(wp) function vap_pres(qv, pressure_pa) result(value)
    real(wp), intent(in) :: qv, pressure_pa
    real(wp), parameter :: rdv=287.04_wp/461.51_wp
    value = qv*pressure_pa/(rdv+(1._wp-rdv)*qv)
  end function vap_pres
end module mo_util_phys
"""


def _fortran_real(value: float) -> str:
    return f"{value:.17e}_wp"


def _fortran_array(values: list[float]) -> str:
    return ", &\n      ".join(_fortran_real(value) for value in values)


def _driver_source(cases: list[ReferenceCase], interval_seconds: float) -> str:
    ncases = len(cases)
    nlev = len(cases[0].temperature_k)
    for case in cases:
        if len(case.temperature_k) != nlev:
            raise ValueError("All Fortran reference cases must use the same number of levels")

    # Fortran's first array index varies fastest: values are level-major across cases.
    temperature = [cases[index].temperature_k[level] for level in range(nlev) for index in range(ncases)]
    pressure = [cases[index].pressure_pa[level] for level in range(nlev) for index in range(ncases)]
    humidity = [cases[index].specific_humidity[level] for level in range(nlev) for index in range(ncases)]
    full_height = [cases[index].full_level_height_m[level] for level in range(nlev) for index in range(ncases)]
    half_height = [cases[index].half_level_height_m[level] for level in range(nlev + 1) for index in range(ncases)]

    def surface(field: str) -> list[float]:
        return [float(getattr(case, field)) for case in cases]

    return f"""
program verify_icon_ptype
  use mo_kind, only: wp
  use mo_diag_precip_type, only: compute_precip_type_field, ptype_precip_amount_threshold
  implicit none
  integer, parameter :: nproma={ncases}, nlev={nlev}
  real(wp) :: temp(nproma,nlev), pres(nproma,nlev), qv(nproma,nlev)
  real(wp) :: z_mc(nproma,nlev), z_ifc(nproma,nlev+1)
  real(wp) :: tot_prec(nproma), tot_prec0(nproma), ground_temp(nproma)
  real(wp) :: rain(nproma), snow(nproma), graupel(nproma), hail(nproma), zeros(nproma)
  real(wp) :: threshold
  integer :: ptype(nproma), jc
  temp = reshape([ &
      {_fortran_array(temperature)} ], shape(temp))
  pres = reshape([ &
      {_fortran_array(pressure)} ], shape(pres))
  qv = reshape([ &
      {_fortran_array(humidity)} ], shape(qv))
  z_mc = reshape([ &
      {_fortran_array(full_height)} ], shape(z_mc))
  z_ifc = reshape([ &
      {_fortran_array(half_height)} ], shape(z_ifc))
  tot_prec = [ {_fortran_array(surface('total_precip_mm'))} ]
  tot_prec0 = [ {_fortran_array(surface('previous_total_precip_mm'))} ]
  ground_temp = [ {_fortran_array(surface('ground_temperature_k'))} ]
  rain = [ {_fortran_array(surface('rain_rate'))} ]
  snow = [ {_fortran_array(surface('snow_rate'))} ]
  graupel = [ {_fortran_array(surface('graupel_rate'))} ]
  hail = [ {_fortran_array(surface('hail_rate'))} ]
  zeros = 0._wp
  threshold = ptype_precip_amount_threshold({_fortran_real(interval_seconds)})
  ptype = -999
  call compute_precip_type_field(nproma, nlev, 1, nproma, temp, pres, qv, z_mc, z_ifc, &
    tot_prec, tot_prec0, ground_temp, rain, zeros, snow, zeros, graupel, hail, threshold, ptype, .false.)
  write(*, '(a,es24.16)') 'threshold=', threshold
  do jc=1,nproma
    write(*, '(i0)') ptype(jc)
  end do
end program verify_icon_ptype
"""


def _extract_icon_source(icon_repo: Path) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(icon_repo), "show", f"{ICON_REFERENCE_COMMIT}:{ICON_MODULE_PATH}"],
        check=True,
        capture_output=True,
    )
    return result.stdout


def _run_fortran(icon_repo: Path, cases: list[ReferenceCase], interval_seconds: float) -> tuple[float, list[int], str]:
    compiler = shutil.which("gfortran")
    if compiler is None:
        raise RuntimeError("gfortran is required to execute the ICON Fortran comparison harness")
    source = _extract_icon_source(icon_repo)
    source_sha256 = hashlib.sha256(source).hexdigest()
    with tempfile.TemporaryDirectory(prefix="icon-ptype-verify-") as temporary_directory:
        work = Path(temporary_directory)
        (work / "dependencies.f90").write_text(DEPENDENCY_STUBS, encoding="utf-8")
        (work / "mo_diag_precip_type.f90").write_bytes(source)
        (work / "driver.f90").write_text(_driver_source(cases, interval_seconds), encoding="utf-8")
        executable = work / "verify_icon_ptype"
        subprocess.run(
            [
                compiler,
                "-std=f2008",
                "-O0",
                "-ffree-line-length-none",
                "dependencies.f90",
                "mo_diag_precip_type.f90",
                "driver.f90",
                "-o",
                str(executable),
            ],
            cwd=work,
            check=True,
        )
        result = subprocess.run([str(executable)], cwd=work, check=True, text=True, capture_output=True)
    lines = result.stdout.strip().splitlines()
    threshold = float(lines[0].split("=", maxsplit=1)[1])
    return threshold, [int(line) for line in lines[1:]], source_sha256


def _run_python(cases: list[ReferenceCase], interval_seconds: float) -> tuple[float, list[int]]:
    threshold = icon_precip_amount_threshold(interval_seconds)
    codes: list[int] = []
    for case in cases:
        precip_amount = case.total_precip_mm - case.previous_total_precip_mm
        if precip_amount < 0.0:
            precip_amount = case.total_precip_mm
        diagnostics = diagnose_icon_column(
            IconColumnProfile(
                temperature_k=np.asarray(case.temperature_k),
                pressure_pa=np.asarray(case.pressure_pa),
                specific_humidity=np.asarray(case.specific_humidity),
                full_level_height_m=np.asarray(case.full_level_height_m),
                half_level_height_m=np.asarray(case.half_level_height_m),
                total_precip_mm=precip_amount,
                ground_temperature_c=case.ground_temperature_k - KELVIN_OFFSET,
                surface_rates=SurfacePrecipitationRates(
                    rain_kg_m2_s=case.rain_rate,
                    snow_kg_m2_s=case.snow_rate,
                    graupel_kg_m2_s=case.graupel_rate,
                    hail_kg_m2_s=case.hail_rate,
                ),
            ),
            precip_mask_threshold_mm=threshold,
        )
        codes.append(int(diagnostics.categorical_code))
    return threshold, codes


def _vector_document(
    cases: list[ReferenceCase],
    codes: list[int],
    interval_seconds: float,
    threshold: float,
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "source_commit": ICON_REFERENCE_COMMIT,
        "source_path": ICON_MODULE_PATH,
        "source_sha256": source_sha256,
        "interval_seconds": interval_seconds,
        "precip_amount_threshold_mm": threshold,
        "cases": [{**asdict(case), "expected_code": code} for case, code in zip(cases, codes, strict=True)],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icon-repo", type=Path, required=True, help="ICON Git checkout containing the pinned commit")
    parser.add_argument("--write-vectors", type=Path, default=None, help="Write frozen Fortran reference vectors as JSON")
    parser.add_argument("--interval-seconds", type=float, default=3600.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = reference_cases()
    fortran_threshold, fortran_codes, source_sha256 = _run_fortran(args.icon_repo, cases, args.interval_seconds)
    python_threshold, python_codes = _run_python(cases, args.interval_seconds)
    if not np.isclose(fortran_threshold, python_threshold, rtol=0.0, atol=1.0e-14):
        raise AssertionError(f"threshold mismatch: Fortran={fortran_threshold}, Python={python_threshold}")
    failures = [
        f"{case.name}: Fortran={fortran_code}, Python={python_code}"
        for case, fortran_code, python_code in zip(cases, fortran_codes, python_codes, strict=True)
        if fortran_code != python_code
    ]
    for case, code in zip(cases, fortran_codes, strict=True):
        print(f"{case.name}: {code}")
    if failures:
        raise AssertionError("ICON parity mismatches:\n" + "\n".join(failures))
    vector_path = args.write_vectors
    if vector_path is not None:
        document = _vector_document(cases, fortran_codes, args.interval_seconds, fortran_threshold, source_sha256)
        vector_path.parent.mkdir(parents=True, exist_ok=True)
        vector_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {vector_path}")
    print(f"verified {len(cases)} cases against {ICON_REFERENCE_COMMIT} ({source_sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
