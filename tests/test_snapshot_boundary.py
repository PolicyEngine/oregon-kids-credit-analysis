"""Verify actual-snapshot comparisons fail closed across process boundaries."""

import hashlib
import json
import pickle
import runpy
from pathlib import Path

import numpy as np
import pytest
from microdf import MicroSeries

from oregon_credit_analysis.accounting import (
    BUDGET_VARIABLES,
    IDENTITY_INPUTS,
    native_accounting_check,
)
from oregon_credit_analysis.scenarios import SCENARIOS
from oregon_credit_analysis.schema import DATA_REVISION, DATA_SHA256, WRAPPER_COMMIT


@pytest.fixture
def runner():
    return runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/run_analysis.py")
    )


def save_synthetic_snapshot(directory, role, *, weights=(2, 7)):
    reform = role == "reform"
    vector = MicroSeries(
        np.array([1002.0, 2048.0] if reform else [1000.0, 2000.0], dtype=np.float32),
        weights=weights,
        index=[17, 91],
    )
    data = pickle.dumps(vector)
    vector_path = directory / f"{role}.households.pkl"
    vector_path.write_bytes(data)
    vectors = {
        name: MicroSeries(
            np.array([0, 0], dtype=np.float32), weights=weights, index=vector.index
        )
        for name in (*IDENTITY_INPUTS, "household_net_income")
    }
    vectors["household_net_income"] = vector
    vectors["household_market_income"] = MicroSeries(
        np.array([1005, 2070], dtype=np.float32), weights=weights, index=vector.index
    )
    vectors["household_tax_before_refundable_credits"] = MicroSeries(
        np.array([3, 22] if reform else [5, 70], dtype=np.float32),
        weights=weights,
        index=vector.index,
    )
    totals = dict.fromkeys(BUDGET_VARIABLES, 0.0)
    totals.update({name: float(values.sum()) for name, values in vectors.items()})
    totals.update(
        household_tax=totals["household_tax_before_refundable_credits"],
        state_income_tax=totals["household_tax_before_refundable_credits"],
        or_ctc=340.0 if reform else 0.0,
        households=9.0,
    )
    components_data = pickle.dumps(vectors)
    components_path = directory / f"{role}.components.pkl"
    components_path.write_bytes(components_data)
    payload = {
        "year": 2027,
        "region": "OR",
        "scenario": "expansion_2027" if reform else None,
        "reform": SCENARIOS["expansion_2027"].reform() if reform else {},
        "runtime": {
            "wrapper_version": "6.17.2",
            "wrapper_source_commit": WRAPPER_COMMIT,
            "country_version": "synthetic",
            "country_source_commit": "a" * 40,
            "country_wheel_sha256": "b" * 64,
            "core_version": "3.34.2",
            "spm_version": "1.1.0",
            "spm_forecast_content_sha256": "c" * 64,
            "data_sha256": DATA_SHA256,
            "data_revision": DATA_REVISION,
            "certification": "unverified_development_fixture",
            "data_certification": "not_certified",
        },
        "totals": totals,
        "household_records": len(vector),
        "covariate_fingerprints": {"source": "synthetic", "weight": "same"},
        "health_in_household_benefits": False,
        "accounting_status": "passed",
        "household_vector_file": vector_path.name,
        "household_vector_sha256": hashlib.sha256(data).hexdigest(),
        "parameters": {},
        "bundle": {},
    }
    check, masses = native_accounting_check(vectors)
    payload["accounting_absolute_masses"] = masses
    payload["accounting_check"] = check
    payload["accounting_components_file"] = components_path.name
    payload["accounting_components_sha256"] = hashlib.sha256(
        components_data
    ).hexdigest()
    path = directory / f"{role}.json"
    path.write_text(json.dumps(payload))
    return path


def test_split_comparison_uses_native_weighted_vectors(tmp_path, runner):
    base, ref = (
        save_synthetic_snapshot(tmp_path, role) for role in ("baseline", "reform")
    )
    output = tmp_path / "result.json"
    runner["comparison"](base, ref, "expansion_2027", output)
    result = json.loads(output.read_text())
    assert result["households_gaining"] == 9
    assert result["households_losing"] == 0
    assert result["budget"]["government_cost"] == 340
    assert result["changes"]["household_net_income"] == 340


def test_split_comparison_rejects_changed_weight_metadata(tmp_path, runner):
    base = save_synthetic_snapshot(tmp_path, "baseline")
    ref = save_synthetic_snapshot(tmp_path, "reform", weights=(2, 8))
    with pytest.raises(ValueError, match="indexes or weights differ"):
        runner["comparison"](base, ref, "expansion_2027", tmp_path / "result.json")


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("accounting_status", "failed", "accounting checks"),
        ("covariate_fingerprints", {"source": "different"}, "covariates"),
        ("reform", {}, "this reform"),
    ],
)
def test_split_comparison_rejects_unqualified_pair(
    tmp_path, runner, field, replacement, message
):
    base, ref = (
        save_synthetic_snapshot(tmp_path, role) for role in ("baseline", "reform")
    )
    payload = json.loads(ref.read_text())
    payload[field] = replacement
    ref.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        runner["comparison"](base, ref, "expansion_2027", tmp_path / "result.json")
