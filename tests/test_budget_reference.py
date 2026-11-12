"""Differentially check the budget equations against the authenticated upstream body.

The reference function receives synthetic aggregate changes at its aggregate-read
boundary. It never loads a country model or substitutes a population estimate.
"""

import ast
import hashlib
import zipfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, Field, computed_field

from oregon_credit_analysis.accounting import BUDGET_VARIABLES, budget_impact

WHEEL = (
    Path(__file__).resolve().parents[1]
    / ".runtime/artifacts/policyengine-6.17.2-py3-none-any.whl"
)
WHEEL_SHA = "d86ba67f7093453fbb3249275c1802ed9dbadb06582fddff447f181958468088"


def upstream_budget(changes, include_health):
    assert hashlib.sha256(WHEEL.read_bytes()).hexdigest() == WHEEL_SHA
    with zipfile.ZipFile(WHEEL) as archive:
        source = archive.read("policyengine/tax_benefit_models/us/analysis.py").decode()
    tree = ast.parse(source)
    reference = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *[
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in {"BudgetaryImpact", "calculate_budgetary_impact"}
            ],
        ],
        type_ignores=[],
    )
    namespace = {
        "BaseModel": BaseModel,
        "Field": Field,
        "computed_field": computed_field,
        "_sum_change": lambda _base, _ref, variable: changes[variable],
        "_include_health_benefits_in_net_income": lambda _base: include_health,
    }
    exec(
        compile(ast.fix_missing_locations(reference), "upstream_budget.py", "exec"),
        namespace,
    )
    return namespace["calculate_budgetary_impact"](None, None)


@given(
    st.fixed_dictionaries(
        {
            key: st.integers(min_value=-(10**12), max_value=10**12)
            for key in BUDGET_VARIABLES
        }
    ),
    st.booleans(),
)
def test_budget_matches_upstream(changes, health):
    reference = upstream_budget(changes, health)
    actual = budget_impact(changes, health_in_household_benefits=health)
    for name in ("federal", "state", "unattributed", "total"):
        assert getattr(actual, name) == pytest.approx(getattr(reference, name))
