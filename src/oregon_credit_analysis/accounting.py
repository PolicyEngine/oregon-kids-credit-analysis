"""Budget accounting after MicroSeries has produced weighted changes.

The equations follow PolicyEngine 6.17.2's calculate_budgetary_impact:
https://github.com/PolicyEngine/policyengine.py/blob/5b6d7901cf93ffbc903874aa305689c03fcb9247/src/policyengine/tax_benefit_models/us/analysis.py
Positive budgetary impact denotes a government gain. Cost reverses that sign.
"""

from pydantic import BaseModel, ConfigDict, computed_field

BUDGET_VARIABLES = (
    "household_tax",
    "household_benefits",
    "federal_benefit_cost",
    "state_benefit_cost",
    "income_tax",
    "employee_payroll_tax",
    "state_income_tax",
)


class BudgetImpact(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    federal: float
    state: float
    unattributed: float

    @computed_field
    @property
    def total(self) -> float:
        return self.federal + self.state + self.unattributed

    @computed_field
    @property
    def government_cost(self) -> float:
        return -self.total


def budget_impact(
    changes: dict[str, float], *, health_in_household_benefits: bool
) -> BudgetImpact:
    """Apply the upstream budget identity to reform-minus-baseline totals."""
    missing = set(BUDGET_VARIABLES) - changes.keys()
    if missing:
        raise ValueError(f"Missing budget components: {sorted(missing)}")
    health = changes["federal_benefit_cost"] + changes["state_benefit_cost"]
    total = changes["household_tax"] - changes["household_benefits"]
    if not health_in_household_benefits:
        total -= health
    federal = (
        changes["income_tax"]
        + changes["employee_payroll_tax"]
        - changes["federal_benefit_cost"]
    )
    state = changes["state_income_tax"] - changes["state_benefit_cost"]
    return BudgetImpact(
        federal=federal, state=state, unattributed=total - federal - state
    )


def accounting_residual(totals: dict[str, float]) -> float:
    """Check the model's household net-income identity, including private costs."""
    return totals["household_net_income"] - (
        totals["household_market_income"]
        + totals["household_benefits"]
        + totals["household_refundable_tax_credits"]
        - totals["household_tax_before_refundable_credits"]
        - totals["household_health_costs"]
    )
