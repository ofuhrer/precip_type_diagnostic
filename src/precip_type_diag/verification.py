"""Verification helpers for prototype regression and observations."""

from __future__ import annotations

from dataclasses import dataclass
import csv
import json
from pathlib import Path

import numpy as np
from earthkit.data import from_source

from .constants import FREEZING_PRECIP_TYPES, PRECIPITATION_TYPE_NAMES, PrecipitationTypeCode
from .gribio import bootstrap_eccodes_definitions


@dataclass(frozen=True)
class PrototypeRegressionCase:
    name: str
    candidate_grib: Path
    reference_grib: Path


@dataclass(frozen=True)
class ObservationRecord:
    predicted: PrecipitationTypeCode
    observed: PrecipitationTypeCode


def load_categorical_grib(path: Path) -> np.ndarray:
    bootstrap_eccodes_definitions()
    fieldset = from_source("file", str(path))
    if len(fieldset) != 1:
        raise ValueError(f"Expected one field in {path}, found {len(fieldset)}")
    return np.asarray(fieldset[0].to_numpy(flatten=False), dtype=np.int32)


def compare_categorical_gribs(candidate: Path, reference: Path) -> dict[str, object]:
    candidate_values = load_categorical_grib(candidate)
    reference_values = load_categorical_grib(reference)
    if candidate_values.shape != reference_values.shape:
        raise ValueError(
            f"Shape mismatch between candidate {candidate_values.shape} and reference {reference_values.shape}"
        )

    diff_mask = candidate_values != reference_values
    return {
        "candidate": str(candidate),
        "reference": str(reference),
        "shape": list(candidate_values.shape),
        "equal": bool(np.array_equal(candidate_values, reference_values)),
        "diff_count": int(np.count_nonzero(diff_mask)),
    }


def run_prototype_regression_manifest(manifest_path: Path) -> dict[str, object]:
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    cases = [
        PrototypeRegressionCase(
            name=entry["name"],
            candidate_grib=Path(entry["candidate_grib"]),
            reference_grib=Path(entry["reference_grib"]),
        )
        for entry in payload["cases"]
    ]
    results = []
    for case in cases:
        result = compare_categorical_gribs(case.candidate_grib, case.reference_grib)
        result["name"] = case.name
        results.append(result)
    return {
        "manifest": str(manifest_path),
        "cases": results,
        "all_equal": all(item["equal"] for item in results),
    }


def _parse_code(value: str | int) -> PrecipitationTypeCode:
    if isinstance(value, int):
        return PrecipitationTypeCode(value)
    text = str(value).strip()
    if text.isdigit():
        return PrecipitationTypeCode(int(text))

    normalized = text.lower()
    for code, name in PRECIPITATION_TYPE_NAMES.items():
        if normalized == name:
            return code
    raise ValueError(f"Unknown precipitation type code: {value}")


def load_observation_records_csv(path: Path) -> list[ObservationRecord]:
    records: list[ObservationRecord] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            records.append(
                ObservationRecord(
                    predicted=_parse_code(row["predicted"]),
                    observed=_parse_code(row["observed"]),
                )
            )
    return records


def score_observation_records(records: list[ObservationRecord]) -> dict[str, object]:
    if not records:
        raise ValueError("At least one observation record is required")

    by_category: dict[str, dict[str, int]] = {}
    for code, name in PRECIPITATION_TYPE_NAMES.items():
        tp = fp = fn = tn = 0
        for record in records:
            predicted = record.predicted == code
            observed = record.observed == code
            if predicted and observed:
                tp += 1
            elif predicted and not observed:
                fp += 1
            elif not predicted and observed:
                fn += 1
            else:
                tn += 1
        by_category[name] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}

    freezing_tp = freezing_fp = freezing_fn = freezing_tn = 0
    for record in records:
        predicted = record.predicted in FREEZING_PRECIP_TYPES
        observed = record.observed in FREEZING_PRECIP_TYPES
        if predicted and observed:
            freezing_tp += 1
        elif predicted and not observed:
            freezing_fp += 1
        elif not predicted and observed:
            freezing_fn += 1
        else:
            freezing_tn += 1

    accuracy = sum(int(record.predicted == record.observed) for record in records) / len(records)
    return {
        "n_records": len(records),
        "accuracy": accuracy,
        "by_category": by_category,
        "any_freezing_precip": {
            "tp": freezing_tp,
            "fp": freezing_fp,
            "fn": freezing_fn,
            "tn": freezing_tn,
        },
    }
