"""ID alignment must preserve native precision and unequal survey weights."""

import runpy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from microdf import MicroSeries
from pydantic import ValidationError

from oregon_credit_analysis.regional import (
    index_by_household_id,
    native_vectors_equal_by_id,
)
from oregon_credit_analysis.schema import (
    DATA_SHA256,
    REGIONAL_WRAPPER_COMMIT,
    RuntimeIdentity,
)


def test_native_household_id_alignment_retains_values_precision_and_weights():
    original = MicroSeries(
        np.array([-2, 3], dtype=np.float32), weights=[7.0, 11.0], index=[91, 17]
    )
    labeled = index_by_household_id(original, [800, 200])
    assert isinstance(labeled, MicroSeries)
    assert labeled.dtype == original.dtype
    assert labeled.index.tolist() == [800, 200]
    assert labeled.weights.index.equals(labeled.index)
    assert labeled.weights.tolist() == [7.0, 11.0]
    assert labeled.sum() == original.sum() == 19
    assert (labeled * (2 * (labeled >= 0) - 1)).sum() == 47
    shuffled = MicroSeries(
        np.array([3, -2], dtype=np.float32), weights=[11.0, 7.0], index=[200, 800]
    )
    assert native_vectors_equal_by_id(labeled, shuffled)
    assert original.index.tolist() == [91, 17]
    assert original.weights.index.tolist() == [91, 17]


def test_id_alignment_rejects_weight_or_value_swaps_and_precision_changes():
    vector = MicroSeries(
        np.array([1, 2], dtype=np.float32), weights=[3, 7], index=[9, 8]
    )
    for changed in (
        MicroSeries(np.array([1, 2], dtype=np.float32), weights=[7, 3], index=[9, 8]),
        MicroSeries(np.array([2, 1], dtype=np.float32), weights=[3, 7], index=[9, 8]),
        MicroSeries(np.array([1, 2], dtype=np.float64), weights=[3, 7], index=[9, 8]),
    ):
        assert not native_vectors_equal_by_id(vector, changed)


@pytest.mark.parametrize("ids", [[1], [1, 1], [1, None]])
def test_id_alignment_rejects_missing_or_ambiguous_records(ids):
    with pytest.raises(ValueError, match="complete, unique and aligned"):
        index_by_household_id(MicroSeries([1, 2], weights=[3, 7]), ids)


def test_id_alignment_rejects_detached_weight_labels():
    vector = MicroSeries([1, 2], weights=[3, 7])
    vector.weights.index = pd.Index([8, 9])
    with pytest.raises(ValueError, match="different indexes"):
        index_by_household_id(vector, [100, 200])


def test_unreleased_wrapper_cannot_inherit_a_released_label():
    identity = {
        "wrapper_version": "6.17.2",
        "wrapper_source_commit": REGIONAL_WRAPPER_COMMIT,
        "country_version": "synthetic",
        "country_source_commit": "a" * 40,
        "country_wheel_sha256": "b" * 64,
        "core_version": "3.34.2",
        "spm_version": "1.1.0",
        "spm_forecast_content_sha256": "c" * 64,
        "data_sha256": DATA_SHA256,
        "data_revision": "populace-us-2024-spm-20260915",
        "certification": "unverified_development_fixture",
        "data_certification": "not_certified",
    }
    with pytest.raises(ValidationError, match="unreleased source build"):
        RuntimeIdentity(**identity)
    identity["wrapper_build_status"] = "unreleased_source_build"
    with pytest.raises(ValidationError, match="wheel SHA"):
        RuntimeIdentity(**identity)
    identity["wrapper_wheel_sha256"] = "d" * 64
    assert RuntimeIdentity(**identity).wrapper_build_status == "unreleased_source_build"


@pytest.fixture
def observer_class():
    return runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/run_regional_analysis.py")
    )["NativeObserver"]


def test_preparation_guard_decides_on_scoring_requests_before_execution(observer_class):
    frame = SimpleNamespace(
        f_globals={"__name__": "policyengine_core.simulations.microsimulation"},
        f_code=SimpleNamespace(co_name="calculate"),
        f_locals={"variable_name": "household_tax"},
    )
    with pytest.raises(ValueError, match="before regional scope"):
        observer_class("prepare", 2027).observe(frame, "call", None)


def test_financial_formula_cannot_precede_scoping(observer_class):
    variable = SimpleNamespace(
        name="synthetic_currency_formula",
        unit="currency-USD",
        get_formula=lambda period: object(),
        adds=None,
        subtracts=None,
    )
    frame = SimpleNamespace(
        f_globals={"__name__": "policyengine_core.simulations.simulation"},
        f_code=SimpleNamespace(co_name="_run_formula"),
        f_locals={
            "variable": variable,
            "period": 2027,
            "self": SimpleNamespace(
                populations={"household": SimpleNamespace(ids=[1, 2])}
            ),
        },
    )
    with pytest.raises(ValueError, match="Financial formula before scope"):
        observer_class("prepare", 2027).observe(frame, "call", None)
    with pytest.raises(ValueError, match="preceded population scope"):
        observer_class("snapshot", 2027, 1).observe(frame, "call", None)
