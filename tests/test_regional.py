"""ID alignment must preserve native precision and unequal survey weights."""

import hashlib
import json
import runpy
import zipfile
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
    REGIONAL_CONTEXT_WRAPPER_COMMIT,
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


@pytest.fixture
def identifier_fixture():
    runner = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/run_regional_analysis.py")
    )
    entities = ("person", "household", "tax_unit", "spm_unit", "family", "marital_unit")
    frames = {
        entity: pd.DataFrame({f"{entity}_id": [100 * index + 7, 100 * index + 5]})
        for index, entity in enumerate(entities, 1)
    }
    populations = {
        entity: SimpleNamespace(
            ids=frame[f"{entity}_id"].to_numpy()[::-1]
            if entity != "person"
            else frame[f"{entity}_id"].to_numpy()
        )
        for entity, frame in frames.items()
    }
    values = {
        f"{entity}_id": population.ids for entity, population in populations.items()
    }
    for entity in entities[1:]:
        frames["person"][f"person_{entity}_id"] = frames[entity][
            f"{entity}_id"
        ].to_numpy()
        frames["person"][f"{entity}_id"] = [-1, -1]  # conflicting bare alias
        values[f"person_{entity}_id"] = frames["person"][
            f"person_{entity}_id"
        ].to_numpy()
    variables = {
        name: SimpleNamespace(
            definition_period="year",
            dtype=np.float32 if name == "family_id" else np.int32,
        )
        for name in values
    }
    country = SimpleNamespace(
        populations=populations,
        tax_benefit_system=SimpleNamespace(variables=variables),
        input_variables=list(values),
        calculate=lambda name, year: MicroSeries(
            np.asarray(values[name], dtype=variables[name].dtype), weights=[2, 7]
        ),
    )
    dataset = SimpleNamespace(data=SimpleNamespace(entity_data=frames))
    return runner["live_identifier_receipt"], country, dataset, values


def test_live_id_guard_queries_all_ids_and_preserves_native_family_cast(
    identifier_fixture,
):
    check, country, dataset, _ = identifier_fixture
    receipt = check(country, dataset, 2029)
    assert receipt["passed"]
    assert len(receipt["variables"]) == 11
    assert receipt["variables"]["family_id"]["dtype"] == "float32"
    assert (
        receipt["variables"]["person_household_id"]["source_column"]
        == "person_household_id"
    )


@pytest.mark.parametrize("corruption", ["values", "input", "period"])
def test_live_id_guard_rejects_missing_or_recomputed_ids(
    identifier_fixture, corruption
):
    check, country, dataset, values = identifier_fixture
    if corruption == "values":
        values["person_id"] = [0, 1]
    elif corruption == "input":
        country.input_variables.remove("person_id")
    else:
        country.tax_benefit_system.variables["person_id"].definition_period = "eternity"
    with pytest.raises(ValueError, match="Live identifier"):
        check(country, dataset, 2029)


@pytest.mark.parametrize("changed_pipeline", [False, True])
def test_export_reuse_requires_same_nonadapter_bytes(tmp_path, changed_pipeline):
    runner = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/run_regional_analysis.py")
    )
    runner["load_prepared"].__globals__["ROOT"] = tmp_path
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    identities = []
    for prefix, commit in (
        ("regional", REGIONAL_CONTEXT_WRAPPER_COMMIT),
        ("regional-ids", REGIONAL_WRAPPER_COMMIT),
    ):
        wheel = tmp_path / f"{prefix}.whl"
        with zipfile.ZipFile(wheel, "w") as z:
            z.writestr("policyengine/tax_benefit_models/us/model.py", prefix)
            z.writestr(
                "policyengine/tax_benefit_models/us/datasets.py",
                prefix if changed_pipeline else "unchanged-pipeline",
            )
        sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
        identity = RuntimeIdentity(
            wrapper_version="6.17.2",
            wrapper_source_commit=commit,
            wrapper_build_status="unreleased_source_build",
            wrapper_wheel_sha256=sha,
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
        identities.append(identity)
        (runtime / f"runtime-{prefix}-candidate.json").write_text(
            json.dumps({"wrapper_wheel": str(wheel)})
        )
    old, new = identities
    folder = runner["cache_folder"](old, "candidate", 2027)
    folder.mkdir(parents=True)
    yearfile = folder / "populace_us_2024_year_2027.h5"
    yearfile.write_bytes(b"synthetic-loader-fixture")
    dataset = SimpleNamespace(
        data=SimpleNamespace(
            entity_data={
                "household": pd.DataFrame(
                    {"household_id": [7], "household_weight": [4.0]}
                )
            }
        ),
        metadata={},
    )
    receipt = {
        "runtime": old.model_dump(),
        "role": "candidate",
        "year": 2027,
        "source_sha256": DATA_SHA256,
        "year_file": yearfile.name,
        "year_file_sha256": hashlib.sha256(yearfile.read_bytes()).hexdigest(),
        "tables": runner["table_receipt"](dataset),
        "metadata": {},
    }
    manifest = tmp_path / "prepared.json"
    manifest.write_text(json.dumps(receipt))
    pe = SimpleNamespace(
        us=SimpleNamespace(
            load_datasets=lambda **kwargs: {"populace_us_2024_2027": dataset}
        )
    )
    if changed_pipeline:
        with pytest.raises(ValueError, match="pipeline changed"):
            runner["load_prepared"](pe, new, "candidate", 2027, manifest)
    else:
        loaded, reused, _ = runner["load_prepared"](
            pe, new, "candidate", 2027, manifest
        )
        assert loaded is dataset
        assert reused["reuse"]["status"] == "explicit_unchanged_export"
