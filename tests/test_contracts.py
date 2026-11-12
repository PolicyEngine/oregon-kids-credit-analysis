"""Guard policy timing, weighted accounting and development-result labels."""

import pickle
import runpy
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from microdf import MicroSeries
from pydantic import ValidationError

from oregon_credit_analysis.accounting import (
    BUDGET_VARIABLES,
    IDENTITY_INPUTS,
    BudgetImpact,
    accounting_residual,
    budget_impact,
    native_absolute,
    native_accounting_check,
)
from oregon_credit_analysis.scenarios import CTC, SCENARIOS
from oregon_credit_analysis.schema import RuntimeIdentity


def test_renewal_changes_no_amount_or_income_limit():
    reform = SCENARIOS["indexed_renewal_2029"].reform()
    assert reform == {f"{CTC}.in_effect": {"2029-01-01.2100-12-31": True}}


def test_2028_evaluates_the_same_2027_expansion():
    assert SCENARIOS["expansion_2027"].reform() == SCENARIOS["expansion_2028"].reform()
    assert SCENARIOS["expansion_2028"].year == 2028


def test_expansion_alone_does_not_renew_expired_credit():
    assert f"{CTC}.in_effect" not in SCENARIOS["expansion_2027"].reform()
    assert f"{CTC}.in_effect" in SCENARIOS["expanded_renewal_2029"].reform()


finite = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)


@given(st.dictionaries(st.sampled_from(BUDGET_VARIABLES), finite, min_size=7))
def test_budget_partition_and_health_double_counting(changes):
    excluded = budget_impact(changes, health_in_household_benefits=False)
    included = budget_impact(changes, health_in_household_benefits=True)
    health = changes["federal_benefit_cost"] + changes["state_benefit_cost"]
    assert included.total - excluded.total == pytest.approx(health, abs=1e-5)
    assert excluded.total == pytest.approx(
        excluded.federal + excluded.state + excluded.unattributed
    )
    assert excluded.government_cost == -excluded.total


def test_budget_captures_benefits_outside_the_ctc():
    changes = dict.fromkeys(BUDGET_VARIABLES, 0.0)
    changes["state_income_tax"] = changes["household_tax"] = -100
    changes["household_benefits"] = 25
    impact = budget_impact(changes, health_in_household_benefits=False)
    assert impact.government_cost == 125
    assert impact.state == -100
    assert impact.unattributed == -25


def test_budget_captures_shared_health_cost_once():
    changes = dict.fromkeys(BUDGET_VARIABLES, 0.0)
    changes.update(federal_benefit_cost=60, state_benefit_cost=40)
    assert (
        budget_impact(changes, health_in_household_benefits=False).government_cost
        == 100
    )
    changes["household_benefits"] = 100
    assert (
        budget_impact(changes, health_in_household_benefits=True).government_cost == 100
    )


def test_weighted_filtered_gain_uses_household_representation():
    # Synthetic example: two Oregon households and one other-state household.
    gain = MicroSeries([100, 200, 1000], weights=[2, 5, 11])
    oregon = MicroSeries([True, True, False], weights=[2, 5, 11])
    assert gain[oregon].sum() == 1200
    assert (gain[oregon] > 0).sum() == 7


@given(finite, finite, finite, finite, finite)
def test_household_accounting_identity(market, benefits, credits, tax, health):
    totals = {
        "household_market_income": market,
        "household_benefits": benefits,
        "household_refundable_tax_credits": credits,
        "household_tax_before_refundable_credits": tax,
        "household_health_costs": health,
        "household_net_income": market + benefits + credits - tax - health,
    }
    assert accounting_residual(totals) == pytest.approx(0, abs=1e-5)


def test_schema_rejects_nonfinite_and_uncertified_label_omission():
    with pytest.raises(ValidationError):
        BudgetImpact(federal=float("nan"), state=0, unattributed=0)
    with pytest.raises(ValidationError):
        RuntimeIdentity(wrapper_version="6.17.2", certification="certified")


def test_baseline_subtraction_retains_microseries_weights():
    baseline = MicroSeries([1000, 2000], weights=[2, 7])
    reform = MicroSeries([1100, 2020], weights=[2, 7])
    assert (reform - baseline).sum() == 340


def test_result_provenance_omits_local_directories_and_preserves_artifact_identity():
    helpers = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/run_analysis.py")
    )
    source = {
        "wheel": "file:///private/task/model.whl",
        "sha256": "a" * 64,
        "nested": ["/private/task/data.h5", "hf://dataset/source@revision"],
    }
    assert helpers["portable_provenance"](source) == {
        "wheel": "local:model.whl",
        "sha256": "a" * 64,
        "nested": ["local:data.h5", "hf://dataset/source@revision"],
    }
    assert source["wheel"].startswith("file:///private/")


def test_native_snapshot_roundtrip_preserves_weighted_difference_and_index():
    baseline = MicroSeries([1000.0, 2000.0], weights=[2, 7], index=[17, 91])
    reform = MicroSeries([1100.0, 2020.0], weights=[2, 7], index=[17, 91])
    loaded = [pickle.loads(pickle.dumps(values)) for values in (baseline, reform)]
    assert all(isinstance(values, MicroSeries) for values in loaded)
    assert loaded[0].index.equals(baseline.index)
    assert loaded[0].weights.equals(baseline.weights)
    assert (loaded[1] - loaded[0]).sum() == 340
    assert ((loaded[1] - loaded[0]) > 0).sum() == 9


def test_native_absolute_retains_unequal_weights():
    values = MicroSeries([-2, 3], weights=[7, 11])
    assert values.abs().sum() == 5  # pandas abs loses MicroSeries metadata.
    assert native_absolute(values).sum() == 47


def test_exact_record_check_distinguishes_model_rounding_from_bad_record():
    names = (*IDENTITY_INPUTS, "household_net_income")
    vectors = {
        name: MicroSeries(np.array([0, 0], dtype=np.float32), weights=[7, 11])
        for name in names
    }
    vectors["household_market_income"] = MicroSeries(
        np.array([-1e8, 1e8], dtype=np.float32), weights=[7, 11]
    )
    vectors["household_refundable_tax_credits"] = MicroSeries(
        np.array([1, 0], dtype=np.float32), weights=[7, 11]
    )
    vectors["household_net_income"] = vectors["household_market_income"].copy()
    check, masses = native_accounting_check(vectors)
    assert check["passed"]  # float32 legitimately rounds away the first $1.
    assert check["residual"] == -7
    assert masses["household_market_income"] == 1.8e9
    vectors["household_net_income"].iloc[0] += np.float32(16)
    broken, _ = native_accounting_check(vectors)
    assert not broken["passed"]
    assert broken["violating_records"] == 1
    assert broken["weighted_violating_households"] == 7
