"""History receipts distinguish supplied periods from cached formula reads."""

import runpy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from oregon_credit_analysis.schema import (
    DATA_SHA256,
    MANAGED_SCOPE_WRAPPER_COMMIT,
    RuntimeIdentity,
)


def test_supplied_receipt_excludes_derived_months_and_reads_actual_branch():
    runner = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/run_managed_scope.py")
    )
    calls = []

    class Holder:
        variable = SimpleNamespace(
            entity=SimpleNamespace(key="person"), definition_period="year"
        )

        def get_input_periods(self, branch):
            calls.append(branch)
            return ["2024", "2029"]

        def get_known_periods(self):
            pytest.fail("formula-cache periods must not qualify as supplied history")

        def get_array(self, period, branch):
            assert branch == "reform-arm"
            return np.array([40 if period == "2024" else 45], dtype=np.int32)

    country = SimpleNamespace(
        input_variables=["age"],
        branch_name="reform-arm",
        get_holder=lambda name: Holder(),
    )
    receipt = runner["supplied_input_receipt"](country)
    assert calls == ["reform-arm"]
    assert set(receipt["age"]["periods"]) == {"2024", "2029"}
    assert receipt["age"]["periods"]["2029"]["dtype"] == "int32"


def test_managed_source_version_and_development_status_remain_bound():
    identity = dict(
        wrapper_version="6.18.0",
        wrapper_source_commit=MANAGED_SCOPE_WRAPPER_COMMIT,
        wrapper_wheel_sha256="d" * 64,
        wrapper_build_status="unreleased_source_build",
        country_version="synthetic",
        country_source_commit="a" * 40,
        country_wheel_sha256="b" * 64,
        core_version="3.34.2",
        spm_version="1.1.0",
        spm_forecast_content_sha256="c" * 64,
        data_sha256=DATA_SHA256,
        data_revision="populace-us-2024-spm-20260915",
        certification="unverified_development_fixture",
        data_certification="not_certified",
    )
    assert RuntimeIdentity(**identity).wrapper_version == "6.18.0"
    with pytest.raises(ValidationError, match="pinned source"):
        RuntimeIdentity(**(identity | {"wrapper_version": "6.17.2"}))
    with pytest.raises(ValidationError, match="unreleased source build"):
        RuntimeIdentity(**(identity | {"wrapper_build_status": "official_release"}))
