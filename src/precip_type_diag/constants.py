"""Constants used by the thesis implementation.

All meteorological thresholds are taken from the thesis unless noted otherwise.
The thesis references are recorded alongside the constant declarations.
"""

from __future__ import annotations

from enum import IntEnum

KELVIN_OFFSET = 273.15
GRAVITY = 9.81

# Thesis section 3.1.1, equations (1) to (3).
PRECIP_GENERATION_RHI_THRESHOLD_PCT = 75.0
PRECIP_GENERATION_MIN_DEPTH_M = 1000.0
SUBLIMATION_MIN_DEPTH_M = 1500.0
PROB_ICE_FULL_THRESHOLD_C = -15.0
PROB_ICE_ZERO_THRESHOLD_C = -7.0

# Thesis section 3.1.2, equation (4) and surrounding text.
SMALL_AREA_THRESHOLD_JKG = 2.0
SHALLOW_SURFACE_REFREEZING_JKG = 1.0
GROUND_FREEZING_THRESHOLD_C = -3.0
DEFAULT_VERTICAL_CUTOFF_M = 12000.0

# The thesis prototype uses the model's lowest full level, which is the 80th full level
# for ICON-1E. This implementation uses the last available full level to keep the same
# method valid for both ICON-CH1-EPS and ICON-CH2-EPS.
SURFACE_LEVEL_INDEX = -1

# The thesis text states that wet-bulb temperature and RH_i are calculated from model
# output, but does not publish the closed-form wet-bulb approximation itself. The
# accompanying prototype cites Fieldextra and uses the constants below.
FIELDEXTRA_ZG = 0.5
FIELDEXTRA_ZH = 0.6
FIELDEXTRA_ZI = 700.0
FIELDEXTRA_ZL = 0.1
FIELDEXTRA_ZM = 6400.0
FIELDEXTRA_ZN = 11.564
FIELDEXTRA_ZO = 1742.0

DEWPOINT_BW = 5420.0
DEWPOINT_AW = 2.53e11
SATURATION_B1 = 611.21
SATURATION_B2I = 22.587
SATURATION_B3 = 273.16
SATURATION_B4I = -0.7

# Thesis section 3.2 / Figure 5, mirrored from the original plotting order:
# RA -> SN -> FZDZ -> PL -> FZRA_gr -> FZRA.
CATEGORICAL_PRIORITY = (
    "freezing_rain",
    "freezing_rain_on_ground",
    "ice_pellets",
    "freezing_drizzle",
    "snow",
    "rain",
)

INPUT_PARAM_IDS = {
    "PS": 500000,
    "P": 500001,
    "HSURF": 500007,
    "HHL": 500008,
    "T_G": 500010,
    "T_2M": 500011,
    "T": 500014,
    "QV": 500035,
    "TOT_PREC": 500041,
    "T_S": 500061,
    "QC": 500100,
    "QI": 500101,
    "QR": 500102,
    "QS": 500103,
    "QG": 500106,
    "T_SO": 500166,
}

REQUIRED_INPUT_FIELDS = ("T", "P", "QV", "HHL", "TOT_PREC", "T_G")

OUTPUT_PARAM_ID = 502712
OUTPUT_SHORT_NAME = "PTYPE"
OUTPUT_NAME = "Precipitation type"
OUTPUT_UNITS = "code table 4.201"

DEFAULT_PROBABILITY_THRESHOLD_PERCENT = 30.0
DEFAULT_INTENSITY_PRECIP_THRESHOLD_MM = 0.01


class PrecipitationTypeCode(IntEnum):
    """Categorical output codes."""

    NO_PRECIP = 0
    RAIN = 1
    FREEZING_RAIN = 3
    SNOW = 5
    ICE_PELLETS = 8
    FREEZING_DRIZZLE = 12
    FREEZING_RAIN_ON_GROUND = 13


PRECIPITATION_TYPE_NAMES = {
    PrecipitationTypeCode.NO_PRECIP: "no_precip",
    PrecipitationTypeCode.RAIN: "rain",
    PrecipitationTypeCode.FREEZING_RAIN: "freezing_rain",
    PrecipitationTypeCode.SNOW: "snow",
    PrecipitationTypeCode.ICE_PELLETS: "ice_pellets",
    PrecipitationTypeCode.FREEZING_DRIZZLE: "freezing_drizzle",
    PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND: "freezing_rain_on_ground",
}

PROBABILITY_TYPE_FIELDS = (
    ("rain", PrecipitationTypeCode.RAIN),
    ("snow", PrecipitationTypeCode.SNOW),
    ("ice_pellets", PrecipitationTypeCode.ICE_PELLETS),
    ("freezing_drizzle", PrecipitationTypeCode.FREEZING_DRIZZLE),
    ("freezing_rain_on_ground", PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND),
    ("freezing_rain", PrecipitationTypeCode.FREEZING_RAIN),
)

CATEGORICAL_PROBABILITY_CODES = (
    PrecipitationTypeCode.NO_PRECIP,
    PrecipitationTypeCode.RAIN,
    PrecipitationTypeCode.FREEZING_RAIN,
    PrecipitationTypeCode.SNOW,
    PrecipitationTypeCode.ICE_PELLETS,
    PrecipitationTypeCode.FREEZING_DRIZZLE,
    PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND,
)

MEMBER_DIAGNOSTIC_VARIABLES = (
    "ptype",
    "hourly_precip_mm",
    *(f"prob_{name}_mm" for name, _ in PROBABILITY_TYPE_FIELDS),
    *(f"precip_{name}_th_mm" for name, _ in PROBABILITY_TYPE_FIELDS),
)

FINAL_PROBABILITY_VARIABLES = (
    *(f"prob_{name}_mm_ens" for name, _ in PROBABILITY_TYPE_FIELDS),
    *(f"precip_{name}_th_ens" for name, _ in PROBABILITY_TYPE_FIELDS),
    *(f"ptype_probability_{int(code)}" for code in CATEGORICAL_PROBABILITY_CODES),
    "valid_member_count",
    "hourly_precip_mean_mm",
)

FREEZING_PRECIP_TYPES = frozenset(
    {
        PrecipitationTypeCode.FREEZING_RAIN,
        PrecipitationTypeCode.FREEZING_DRIZZLE,
        PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND,
        PrecipitationTypeCode.ICE_PELLETS,
    }
)
