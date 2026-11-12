"""Verify actual-snapshot comparisons fail closed across process boundaries."""

import hashlib
import json
import pickle
import runpy
from pathlib import Path

import pytest
from microdf import MicroSeries

from oregon_credit_analysis.accounting import BUDGET_VARIABLES
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
        [1100.0, 2020.0] if reform else [1000.0, 2000.0],
        weights=weights,
        index=[17, 91],
    )
    data = pickle.dumps(vector)
    vector_path = directory / f"{role}.households.pkl"
    vector_path.write_bytes(data)
    totals = dict.fromkeys(BUDGET_VARIABLES, 0.0)
    totals.update(
        household_tax=160.0 if reform else 500.0,
        state_income_tax=160.0 if reform else 500.0,
        household_net_income=float(vector.sum()),
        household_market_income=16_500.0,
        household_refundable_tax_credits=0.0,
        household_tax_before_refundable_credits=160.0 if reform else 500.0,
        household_health_costs=0.0,
        or_ctc=340.0 if reform else 0.0,
        households=9.0,
    )
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
