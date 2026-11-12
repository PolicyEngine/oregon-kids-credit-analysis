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
IDENTITY_INPUTS = (
    "household_market_income",
    "household_benefits",
    "household_refundable_tax_credits",
    "household_tax_before_refundable_credits",
    "household_health_costs",
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


def native_absolute(values):
    """Preserve MicroSeries weights; pandas abs currently drops that metadata."""
    from microdf import MicroSeries

    if not isinstance(values, MicroSeries):
        raise TypeError("Accounting vectors must retain their native MicroSeries type")
    result = values * (2 * (values >= 0) - 1)
    if not isinstance(result, MicroSeries):
        raise TypeError("Absolute values lost their native MicroSeries type")
    return result


def native_accounting_check(vectors):
    """Require each native float32 record to match Core's exact operation order."""
    from microdf import MicroSeries

    names = (*IDENTITY_INPUTS, "household_net_income")
    reference = vectors["household_net_income"]
    for name in names:
        value = vectors[name]
        if not isinstance(value, MicroSeries) or str(value.dtype) != "float32":
            raise ValueError("Accounting requires native float32 outcome vectors")
        if not value.index.equals(reference.index) or not value.weights.equals(
            reference.weights
        ):
            raise ValueError("Accounting vector indexes or weights differ")
        if value.isna().any() or value.isin([float("inf"), -float("inf")]).any():
            raise ValueError("Accounting values must be finite")
        weights = value.weights
        if (
            str(weights.dtype) != "float64"
            or weights.isna().any()
            or ((weights < 0) | weights.isin([float("inf"), -float("inf")])).any()
        ):
            raise ValueError("Accounting requires finite nonnegative float64 weights")
    # Core's _run_formula starts at zero, then adds these three arrays and
    # subtracts these two. Zero + the first array is exact. Keep all four
    # remaining operations in float32, exactly as the actual model does.
    rebuilt = (
        vectors["household_market_income"]
        + vectors["household_benefits"]
        + vectors["household_refundable_tax_credits"]
        - vectors["household_tax_before_refundable_credits"]
        - vectors["household_health_costs"]
    )
    if str(rebuilt.dtype) != "float32":
        raise ValueError("Reconstructed identity changed Core's arithmetic precision")
    violations = rebuilt != reference
    masses = {
        name: float(native_absolute(vectors[name].astype("float64")).sum())
        for name in names
    }
    totals = {name: float(vectors[name].sum()) for name in names}
    check = {
        "method": "exact_core_float32_identity_per_household",
        "operation_order": (
            "market + benefits + refundable_credits - tax_before - health"
        ),
        "residual": accounting_residual(totals),
        "records": len(reference),
        "violating_records": len(violations[violations]),
        "weighted_violating_households": float(violations.sum()),
        "passed": bool(rebuilt.equals(reference)),
    }
    return check, masses
