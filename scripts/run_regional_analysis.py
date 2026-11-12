#!/usr/bin/env python3
"""Prepare official year inputs, then run the public scoped simulation API.

Every preparation or snapshot process must run through the host heavy guard.
The original managed-runtime artifacts remain separate and replayable.
"""

import argparse
import gc
import hashlib
import importlib.metadata as metadata
import json
import pickle
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_analysis as original  # noqa: E402

from oregon_credit_analysis.regional import (  # noqa: E402
    index_by_household_id,
    native_vectors_equal_by_id,
)
from oregon_credit_analysis.schema import (  # noqa: E402
    DATA_REVISION,
    DATA_SHA256,
    MANAGED_SCOPE_WRAPPER_COMMIT,
    REGIONAL_CONTEXT_WRAPPER_COMMIT,
    REGIONAL_WRAPPER_COMMIT,
    RuntimeIdentity,
)

SOURCE = ROOT / "data/populace_us_2024.h5"
SOURCE_NAME = "populace_us_2024"


def digest_series(values):
    return hashlib.sha256(
        values.to_json(
            orient="split", double_precision=15, default_handler=str
        ).encode()
    ).hexdigest()


def activate(role, profile="regional-ids"):
    expected_commit = {
        "regional-ids": REGIONAL_WRAPPER_COMMIT,
        "managed-scope": MANAGED_SCOPE_WRAPPER_COMMIT,
    }[profile]
    runtime = json.loads((ROOT / f".runtime/runtime-{profile}-{role}.json").read_text())
    if Path(sys.executable).resolve() != Path(runtime["python"]).resolve():
        raise ValueError("Use the selected, isolated regional interpreter")
    fixture_path = Path(runtime["source"]) / "tests/fixtures/spm_development.py"
    if (
        original.sha256(fixture_path)
        != "f762892a374c3950af54834f0fb234708addc59ad4af90aab99866eb37e27a5e"
    ):
        raise ValueError("Authenticated upstream development helper changed")
    sys.path.insert(0, runtime["source"])
    from tests.fixtures.spm_development import activate_spm_development_manifest

    fixture = activate_spm_development_manifest(runtime["development_manifest"])
    import policyengine as pe

    if fixture["development"]["data_certification"] != "not_certified":
        raise ValueError("Development runtime must deny certification")
    if (
        pe.us.model.data_certification.compatibility_basis
        != "unverified_development_fixture"
    ):
        raise ValueError("Unexpected model/data qualification")
    wrapper = runtime["wrapper"]
    if wrapper["source_commit"] != expected_commit:
        raise ValueError("Unexpected wrapper adapter source")
    wheel = Path(runtime["wrapper_wheel"])
    if original.sha256(wheel) != wrapper["sha256"]:
        raise ValueError("Wrapper wheel fingerprint changed")
    distribution = metadata.distribution("policyengine")
    expected_import = Path(
        distribution.locate_file("policyengine/__init__.py")
    ).resolve()
    if Path(pe.__file__).resolve() != expected_import:
        raise ValueError(
            "Wrapper import does not resolve to the authenticated installation"
        )
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("policyengine/") and not name.endswith("/"):
                if Path(distribution.locate_file(name)).read_bytes() != archive.read(
                    name
                ):
                    raise ValueError(f"Installed wrapper differs from wheel: {name}")
    if original.sha256(SOURCE) != DATA_SHA256:
        raise ValueError("Source dataset fingerprint changed")
    identity = RuntimeIdentity(
        wrapper_version=metadata.version("policyengine"),
        wrapper_source_commit=wrapper["source_commit"],
        wrapper_wheel_sha256=wrapper["sha256"],
        wrapper_build_status=wrapper["build_status"],
        country_version=metadata.version("policyengine-us"),
        country_source_commit=runtime["model_source_commit"],
        country_wheel_sha256=runtime["model_wheel_sha256"],
        core_version=metadata.version("policyengine-core"),
        spm_version=metadata.version("spm-calculator"),
        spm_forecast_content_sha256=runtime["forecast_content_sha256"],
        data_sha256=DATA_SHA256,
        data_revision=DATA_REVISION,
        certification="unverified_development_fixture",
        data_certification="not_certified",
    )
    return pe, identity, runtime


def cache_folder(identity, role, year):
    return (
        ROOT
        / ".runtime/yearfiles"
        / role
        / identity.country_source_commit
        / identity.wrapper_source_commit
        / DATA_SHA256
        / str(year)
    )


def table_receipt(dataset):
    import pandas as pd

    return {
        entity: {
            "rows": len(frame),
            "columns": list(frame.columns),
            "dtypes": {name: str(dtype) for name, dtype in frame.dtypes.items()},
            "columns_sha256": {
                name: digest_series(pd.Series(frame[name])) for name in frame.columns
            },
        }
        for entity, frame in dataset.data.entity_data.items()
    }


def native_input_receipt(country):
    import pandas as pd

    result = {}
    for name in sorted(country.input_variables):
        holder = country.get_holder(name)
        periods = {}
        for period in holder.get_known_periods():
            values = holder.get_array(period, country.branch_name)
            if values is not None:
                periods[str(period)] = {
                    "rows": len(values),
                    "dtype": str(values.dtype),
                    "sha256": digest_series(pd.Series(values)),
                }
        result[name] = {
            "definition_period": str(holder.variable.definition_period),
            "periods": periods,
        }
    return result


def live_identifier_receipt(country, dataset, year):
    """Verify all live IDs against source rows using the native variable cast."""
    import pandas as pd

    checked = {}
    for entity, frame in dataset.data.entity_data.items():
        frame = pd.DataFrame(frame).set_index(f"{entity}_id", drop=False)
        frame = frame.loc[country.populations[entity].ids]
        columns = {f"{entity}_id": f"{entity}_id"}
        if entity == "person":
            for group in (
                "household",
                "tax_unit",
                "spm_unit",
                "family",
                "marital_unit",
            ):
                live = f"person_{group}_id"
                columns[live] = live if live in frame else f"{group}_id"
        for live, column in columns.items():
            variable = country.tax_benefit_system.variables[live]
            if (
                str(variable.definition_period) != "year"
                or live not in country.input_variables
            ):
                raise ValueError(
                    f"Live identifier lacks its native yearly input: {live}"
                )
            actual = pd.Series(country.calculate(live, year), copy=False).reset_index(
                drop=True
            )
            expected = pd.Series(frame[column], dtype=variable.dtype).reset_index(
                drop=True
            )
            if not actual.equals(expected):
                raise ValueError(f"Live identifier differs from source rows: {live}")
            checked[live] = {
                "source_column": column,
                "entity": entity,
                "period": str(year),
                "dtype": str(actual.dtype),
                "sha256": digest_series(actual),
            }
    if len(checked) != 11:
        raise ValueError("All eleven live identifiers must be verified")
    return {"passed": True, "variables": checked}


class NativeObserver:
    """Observe native execution; never replace methods, inputs or formulas."""

    def __init__(self, stage, year, expected_households=None):
        self.stage = stage
        self.year = year
        self.expected_households = expected_households
        self.calculations = Counter()
        self.formulas = Counter()
        self.country = None

    def __enter__(self):
        if sys.getprofile() is not None:
            raise ValueError(
                "An existing profiler would make execution tracing ambiguous"
            )
        sys.setprofile(self.observe)
        return self

    def __exit__(self, *args):
        sys.setprofile(None)

    def observe(self, frame, event, arg):
        module = frame.f_globals.get("__name__", "")
        name = frame.f_code.co_name
        if event == "call" and module.startswith("policyengine_core.simulations."):
            if name == "calculate":
                variable = frame.f_locals.get("variable_name")
                if variable:
                    self.calculations[variable] += 1
                    if self.stage == "prepare" and variable in original.VARIABLES:
                        raise ValueError(
                            f"Scoring output before regional scope: {variable}"
                        )
            elif name == "_run_formula":
                variable = frame.f_locals.get("variable")
                if variable is not None:
                    self.formulas[variable.name] += 1
                    country = frame.f_locals["self"]
                    financial = variable.unit and "USD" in str(variable.unit)
                    has_formula = (
                        variable.get_formula(frame.f_locals["period"]) is not None
                        or variable.adds
                        or variable.subtracts
                    )
                    if self.stage == "prepare" and (
                        variable.name in original.VARIABLES
                        or (financial and has_formula)
                    ):
                        raise ValueError(
                            f"Financial formula before scope: {variable.name}"
                        )
                    if self.stage == "snapshot" and (
                        variable.name in original.VARIABLES or financial
                    ):
                        if (
                            len(country.populations["household"].ids)
                            != self.expected_households
                        ):
                            raise ValueError(
                                "Financial calculation preceded population scope"
                            )
        if (
            event == "return"
            and module == "policyengine.tax_benefit_models.us.model"
            and name == "run"
        ):
            country = frame.f_locals.get("microsim")
            if country is not None:
                arms = {"reform": country}
                if country.baseline is not None:
                    arms["baseline"] = country.baseline
                self.country = {
                    "parameters": original.parameter_values(country.tax_benefit_system),
                    "health_in_household_benefits": bool(
                        country.tax_benefit_system.parameters(
                            f"{self.year}-01-01"
                        ).gov.simulation.include_health_benefits_in_net_income
                    ),
                    "arms": {
                        arm: {
                            "population_mode": native.is_over_dataset,
                            "dataset_is_none": native.dataset is None,
                            "household_records": len(
                                native.populations["household"].ids
                            ),
                            "live_identifier_check": live_identifier_receipt(
                                native, frame.f_locals["dataset"], self.year
                            ),
                            "native_inputs": native_input_receipt(native),
                        }
                        for arm, native in arms.items()
                    },
                }

    def receipt(self):
        return {
            "method": "sys.setprofile_observation_only",
            "stage": self.stage,
            "calculate_calls": dict(sorted(self.calculations.items())),
            "formula_calls": dict(sorted(self.formulas.items())),
            "country": self.country,
        }


def prepare(pe, identity, role, year, output):
    folder = cache_folder(identity, role, year)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "preparation.json"
    if manifest.exists():
        raise ValueError(
            "Preparation already exists; load its verified official year file"
        )
    if (folder / f"{SOURCE_NAME}_year_{year}.h5").exists():
        raise ValueError(
            "An unreceipted year file cannot qualify as an observed preparation"
        )
    link = folder / f"{SOURCE_NAME}.h5"
    if not link.exists():
        link.symlink_to(SOURCE.resolve())
    if original.sha256(link) != DATA_SHA256:
        raise ValueError("Official materializer source alias changed")
    original.progress("year_preparation_start", year=year, role=role)
    with NativeObserver("prepare", year) as observer:
        datasets = pe.us.ensure_datasets(
            datasets=[SOURCE_NAME], years=[year], data_folder=str(folder)
        )
    dataset = datasets[f"{SOURCE_NAME}_{year}"]
    receipt = {
        "status": "prepared",
        "runtime": identity.model_dump(),
        "year": year,
        "role": role,
        "source_sha256": DATA_SHA256,
        "year_file": Path(dataset.filepath).name,
        "year_file_sha256": original.sha256(dataset.filepath),
        "tables": table_receipt(dataset),
        "metadata": dataset.metadata,
        "execution_trace": observer.receipt(),
        "analysis_runner_sha256": original.sha256(__file__),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    original.write_json(manifest, receipt)
    original.write_json(output, receipt)
    original.progress("year_preparation_complete", year=year, role=role)


def load_prepared(pe, identity, role, year, prepared_inputs=None):
    manifest = (
        prepared_inputs or cache_folder(identity, role, year) / "preparation.json"
    )
    receipt = json.loads(manifest.read_text())
    prepared_identity = RuntimeIdentity.model_validate(receipt["runtime"])
    if receipt["source_sha256"] != DATA_SHA256 or (
        receipt["role"],
        receipt["year"],
    ) != (role, year):
        raise ValueError("Prepared inputs use another source or runtime")
    if prepared_identity != identity:
        old = prepared_identity.model_dump(
            exclude={"wrapper_source_commit", "wrapper_wheel_sha256"}
        )
        new = identity.model_dump(
            exclude={"wrapper_source_commit", "wrapper_wheel_sha256"}
        )
        if (
            not prepared_inputs
            or old != new
            or prepared_identity.wrapper_source_commit
            != REGIONAL_CONTEXT_WRAPPER_COMMIT
        ):
            raise ValueError("Prepared inputs use another source or runtime")
        # Explicit reuse of the verified official export. All modules outside
        # the reviewed input adapter must remain byte-identical.
        manifests = [
            ROOT / f".runtime/runtime-{prefix}-{role}.json"
            for prefix in ("regional", "regional-ids")
        ]
        infos = [json.loads(path.read_text()) for path in manifests]
        wheels = [Path(info["wrapper_wheel"]) for info in infos]
        if any(
            original.sha256(path) != expected.wrapper_wheel_sha256
            for path, expected in zip(
                wheels, (prepared_identity, identity), strict=True
            )
        ):
            raise ValueError("Preparation producer or consumer wrapper wheel changed")
        with zipfile.ZipFile(wheels[0]) as left, zipfile.ZipFile(wheels[1]) as right:
            members = {
                name
                for name in left.namelist()
                if name.startswith("policyengine/") and not name.endswith("/")
            }
            right_members = {
                name
                for name in right.namelist()
                if name.startswith("policyengine/") and not name.endswith("/")
            }
            if members != right_members or any(
                left.read(name) != right.read(name)
                for name in members
                if name != "policyengine/tax_benefit_models/us/model.py"
            ):
                raise ValueError("Official input preparation pipeline changed")
        receipt = dict(
            receipt,
            reuse={
                "status": "explicit_unchanged_export",
                "producer_runtime": prepared_identity.model_dump(),
                "consumer_runtime": identity.model_dump(),
                "unchanged_package_members": len(members) - 1,
                "reviewed_adapter_only_change": "PolicyEngine/policyengine.py#754",
            },
        )
    folder = cache_folder(prepared_identity, role, year)
    path = folder / receipt["year_file"]
    if path.parent != folder or original.sha256(path) != receipt["year_file_sha256"]:
        raise ValueError("Official year file differs from its preparation receipt")
    # The official cached loader validates the year-file format/renames/SPM
    # stamps. It loads the file and never reconstructs a native population.
    datasets = pe.us.load_datasets(
        datasets=[SOURCE_NAME], years=[year], data_folder=str(folder)
    )
    dataset = datasets[f"{SOURCE_NAME}_{year}"]
    if (
        table_receipt(dataset) != receipt["tables"]
        or dataset.metadata != receipt["metadata"]
    ):
        raise ValueError("Reloaded official year inputs differ from their receipt")
    return dataset, receipt, manifest


def snapshot(
    pe, identity, role, year, region, output, scenario_name, prepared_inputs=None
):
    from policyengine.core import Simulation
    from policyengine.core.scoping_strategy import RowFilterStrategy

    scenario = original.SCENARIOS[scenario_name] if scenario_name else None
    if scenario and (year != scenario.year or region != "OR"):
        raise ValueError("Policy snapshot must use the scenario's Oregon/year")
    dataset, prepared, manifest = load_prepared(
        pe, identity, role, year, prepared_inputs
    )
    scope = (
        RowFilterStrategy(variable_name="state_fips", variable_value=41)
        if region == "OR"
        else None
    )
    expected = len(dataset.data.household)
    if scope:
        # This raw row count is an execution invariant, not a policy statistic.
        expected = len(
            dataset.data.household[dataset.data.household["state_fips"] == 41]
        )
    simulation = Simulation(
        dataset=dataset,
        tax_benefit_model_version=pe.us.model,
        scoping_strategy=scope,
        policy=scenario.reform() if scenario else None,
        extra_variables={"household": list(original.VARIABLES)},
    )
    original.progress(
        "scoped_calculation_start", year=year, region=region, scenario=scenario_name
    )
    with NativeObserver("snapshot", year, expected) as observer:
        simulation.run()
    native = observer.country
    if native is None or any(
        not arm["population_mode"]
        or arm["household_records"] != expected
        or not arm["live_identifier_check"]["passed"]
        for arm in native["arms"].values()
    ):
        raise ValueError("Country run did not retain scoped population semantics")
    household = simulation.output_dataset.data.household
    vectors = {
        name: index_by_household_id(household[name], household["household_id"])
        for name in original.VARIABLES
    }
    totals = {name: float(values.sum()) for name, values in vectors.items()}
    reference = vectors["household_net_income"]
    totals["households"] = float((reference == reference).sum())
    parameters = native["parameters"]
    health = native["health_in_household_benefits"]
    spm_receipt = simulation.spm_provenance()
    spm_config = simulation.spm_config
    output_metadata = simulation.output_dataset.metadata
    fingerprints = {"source_sha256": DATA_SHA256}
    for entity, frame in dataset.data.entity_data.items():
        for name in (f"{entity}_id", f"{entity}_weight"):
            fingerprints[f"{entity}.{name}"] = digest_series(frame[name])
    fingerprints["scoped_household_ids"] = digest_series(household["household_id"])
    fingerprints["scoped_household_weights"] = digest_series(reference.weights)
    del simulation, household, dataset
    gc.collect()
    check, masses = original.native_accounting_check(vectors)
    output = Path(output)
    components = output.with_suffix(".components.pkl")
    vector = output.with_suffix(".households.pkl")
    components.parent.mkdir(parents=True, exist_ok=True)
    components.write_bytes(pickle.dumps(vectors, protocol=pickle.HIGHEST_PROTOCOL))
    vector.write_bytes(pickle.dumps(reference, protocol=pickle.HIGHEST_PROTOCOL))
    original.write_json(
        output,
        {
            "schema_version": 1,
            "runtime": identity.model_dump(),
            "year": year,
            "region": region,
            "scenario": scenario_name,
            "reform": scenario.reform() if scenario else {},
            "totals": totals,
            "household_records": len(reference),
            "covariate_fingerprints": fingerprints,
            "household_vector_file": vector.name,
            "household_vector_sha256": original.sha256(vector),
            "accounting_components_file": components.name,
            "accounting_components_sha256": original.sha256(components),
            "accounting_check": check,
            "accounting_status": "passed" if check["passed"] else "failed",
            "accounting_absolute_masses": masses,
            "accounting_residual": check["residual"],
            "health_in_household_benefits": health,
            "parameters": parameters,
            "bundle": {"data_certification": "not_certified", "spm": spm_receipt},
            "source_input_audit": {
                "preparation_receipt_sha256": original.sha256(manifest),
                "year_file_sha256": prepared["year_file_sha256"],
                "output_metadata": output_metadata,
                "preparation_reuse": prepared.get("reuse"),
            },
            "simulation_configuration": {
                "initialization": "official_year_inputs_then_modern_simulation",
                "scoping_strategy": scope.model_dump() if scope else None,
                "extra_variables": {"household": list(original.VARIABLES)},
                "spm": spm_config,
                "retained_baseline_arm": scenario is not None,
            },
            "execution_trace": observer.receipt(),
            "analysis_runner_sha256": original.sha256(__file__),
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    original.load_snapshot(output)
    original.progress(
        "snapshot_complete", year=year, region=region, scenario=scenario_name
    )


def parity(previous, current, output):
    import pandas as pd

    old, new = (original.load_snapshot(path) for path in (previous, current))
    if (old["year"], old["region"], old["reform"]) != (
        new["year"],
        new["region"],
        new["reform"],
    ):
        raise ValueError("Qualification scenario/year mismatch")
    for key in (
        "country_source_commit",
        "country_wheel_sha256",
        "core_version",
        "spm_version",
        "spm_forecast_content_sha256",
        "data_sha256",
    ):
        if old["runtime"][key] != new["runtime"][key]:
            raise ValueError(f"Qualification runtime mismatch: {key}")
    if old["parameters"] != new["parameters"]:
        raise ValueError("Dated policy values differ between methods")
    if original.sha256(SOURCE) != DATA_SHA256:
        raise ValueError("Source dataset fingerprint changed")
    ids = (
        pd.read_hdf(SOURCE, key="household")["household_id"]
        .reset_index(drop=True)
        .rename(None)
    )
    if digest_series(ids) != old["covariate_fingerprints"]["household.household_id"]:
        raise ValueError("Old record indexes are not bound to authenticated native IDs")
    left = original.load_metric_vectors(previous, old)
    right = original.load_metric_vectors(current, new)
    checks = {}
    for name in original.VARIABLES:
        indexed = index_by_household_id(left[name], ids.loc[left[name].index])
        checks[name] = native_vectors_equal_by_id(indexed, right[name])
    if not all(checks.values()):
        raise ValueError(
            "Actual native vectors differ: "
            f"{[name for name, ok in checks.items() if not ok]}"
        )
    original.write_json(
        output,
        {
            "status": "passed",
            "method": "all_13_exact_native_vectors_and_weights_by_household_id",
            "checks": checks,
            "previous_snapshot_sha256": original.sha256(previous),
            "current_snapshot_sha256": original.sha256(current),
            "household_records": new["household_records"],
            "runtime": new["runtime"],
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "snapshot", "parity"))
    parser.add_argument(
        "--role", choices=("baseline", "candidate"), default="candidate"
    )
    parser.add_argument(
        "--year", type=int, choices=(2026, 2027, 2028, 2029), default=2027
    )
    parser.add_argument("--region", choices=("OR", "us"), default="OR")
    parser.add_argument("--scenario", choices=tuple(original.SCENARIOS))
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--current", type=Path)
    parser.add_argument("--prepared-inputs", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refuse to overwrite an existing artifact")
    if args.prepared_inputs and args.mode != "snapshot":
        parser.error("Explicit prepared inputs are only valid for a snapshot")
    if args.mode == "parity":
        if not args.previous or not args.current:
            parser.error("Parity needs both actual snapshots")
        parity(args.previous, args.current, args.output)
        return
    pe, identity, _ = activate(args.role)
    if args.mode == "prepare":
        prepare(pe, identity, args.role, args.year, args.output)
    else:
        snapshot(
            pe,
            identity,
            args.role,
            args.year,
            args.region,
            args.output,
            args.scenario,
            args.prepared_inputs,
        )


if __name__ == "__main__":
    main()
