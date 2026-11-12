"""Guard policy timing, weighted accounting and development-result labels."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from microdf import MicroSeries
from pydantic import ValidationError

from oregon_credit_analysis.accounting import (
    BUDGET_VARIABLES,
    BudgetImpact,
    accounting_residual,
    budget_impact,
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
