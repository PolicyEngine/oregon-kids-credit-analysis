#!/usr/bin/env python3
"""Run one actual comparison in its authenticated PolicyEngine environment.

Launch with cwd at the authenticated wrapper source root, using that role's
interpreter. The host heavy-run wrapper must own every population command.
"""

import argparse
import gc
import hashlib
import importlib.metadata as metadata
import json
import os
import pickle
import resource
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oregon_credit_analysis.accounting import (  # noqa: E402
    BUDGET_VARIABLES,
    IDENTITY_INPUTS,
    accounting_residual,
    budget_impact,
    native_accounting_check,
)
from oregon_credit_analysis.scenarios import CTC, SCENARIOS  # noqa: E402
from oregon_credit_analysis.schema import (  # noqa: E402
    DATA_REVISION,
    DATA_SHA256,
    WRAPPER_COMMIT,
    ComparisonResult,
    RuntimeIdentity,
)

ACCOUNTING_VARIABLES = (
    "household_net_income",
    "household_market_income",
    "household_refundable_tax_credits",
    "household_tax_before_refundable_credits",
    "household_health_costs",
)
VARIABLES = tuple(dict.fromkeys((*BUDGET_VARIABLES, *ACCOUNTING_VARIABLES, "or_ctc")))
COVARIATES = (
    ("household_id", "household"),
    ("tax_unit_id", "tax_unit"),
    ("spm_unit_id", "spm_unit"),
    ("person_id", "person"),
    ("state_fips", "household"),
    ("age", "person"),
    ("employment_income", "person"),
)
ASSUMPTIONS = [
    "The Oregon credit is calculated for every eligible modeled tax unit; "
    "or_ctc has no separate filing or take-up adjustment.",
    "The source data carry other programs’ participation inputs unchanged.",
]
LIMITATIONS = [
    "Unverified development model/data pair; not a production-certified estimate.",
    "Oregon estimate uses the roughly 57,000-household national dataset; "
    "state sampling uncertainty may affect the result.",
    "This run does not reproduce an ACS-local website report.",
    "No behavioral response parameters were changed by the analysis.",
]
GAIN_THRESHOLD = 0.01


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def portable_provenance(value):
    """Keep artifact identities while omitting machine-local directory names."""
    if isinstance(value, dict):
        return {key: portable_provenance(item) for key, item in value.items()}
    if isinstance(value, list):
        return [portable_provenance(item) for item in value]
    if isinstance(value, str) and (
        value.startswith("/") or value.startswith("file://")
    ):
        return "local:" + Path(value.removeprefix("file://")).name
    return value


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(portable_provenance(payload), indent=2, allow_nan=False, default=str)
        + "\n"
    )
    temporary.replace(path)


def activate(role):
    runtime = json.loads((ROOT / ".runtime" / f"runtime-{role}.json").read_text())
    if Path(sys.executable).resolve() != Path(runtime["python"]).resolve():
        raise ValueError("Use the selected role's isolated interpreter")
    from tests.fixtures.spm_development import activate_spm_development_manifest

    fixture = activate_spm_development_manifest(runtime["development_manifest"])
    import policyengine as pe

    if fixture["development"]["data_certification"] != "not_certified":
        raise ValueError("The development fixture must deny data certification")
    if (
        pe.us.model.data_certification.compatibility_basis
        != "unverified_development_fixture"
    ):
        raise ValueError("Unexpected model/data certification")
    os.chdir(ROOT)
    dataset = ROOT / "data/populace_us_2024.h5"
    if sha256(dataset) != DATA_SHA256:
        raise ValueError(
            "Input dataset differs from the recorded certified source bytes"
        )
    identity = RuntimeIdentity(
        wrapper_version=metadata.version("policyengine"),
        wrapper_source_commit=WRAPPER_COMMIT,
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
    return pe, identity


def parameter_values(system):
    paths = (f"{CTC}.amount", f"{CTC}.reduction.start", f"{CTC}.in_effect")
    return {
        str(year): {
            path: system.parameters.get_child(path)(f"{year}-01-01") for path in paths
        }
        for year in (2026, 2027, 2028, 2029)
    }


def progress(event, **details):
    print(
        json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "event": event,
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
                **details,
            }
        ),
        flush=True,
    )


def covariate_fingerprints(simulation, year):
    # Serialization serves only identity checks; every estimate retains MicroSeries.
    fingerprints = {
        f"{entity}.{variable}": hashlib.sha256(
            simulation.calc(variable, period=2024, map_to=entity)
            .to_json(orient="split", double_precision=15)
            .encode()
        ).hexdigest()
        for variable, entity in COVARIATES
    }
    for period in (2024, year):
        values = simulation.calc("household_weight", period=period, map_to="household")
        fingerprints[f"household.household_weight.{period}"] = hashlib.sha256(
            values.to_json(orient="split", double_precision=15).encode()
        ).hexdigest()
    return fingerprints


def totals(simulation, year, region):
    geography = simulation.calc("state_fips", period=year, map_to="household")
    mask = geography == 41 if region == "OR" else geography == geography
    output = {}
    metric_vectors = {}
    for variable in VARIABLES:
        progress("calculate_start", variable=variable, year=year)
        values = simulation.calc(variable, period=year, map_to="household")
        if values.isna().any():
            raise ValueError(f"Missing {variable} values before geographic aggregation")
        output[variable] = float(values[mask].sum())
        metric_vectors[variable] = values[mask]
        if variable in (*IDENTITY_INPUTS, "household_net_income"):
            if str(values.dtype) != "float32" or str(values.weights.dtype) != "float64":
                raise ValueError("Accounting precision assumptions differ from runtime")
            if values.weights.isna().any() or (values.weights < 0).any():
                raise ValueError("Accounting bound requires finite nonnegative weights")
        progress("calculate_complete", variable=variable, year=year)
    output["households"] = float((geography[mask] == geography[mask]).sum())
    return output, mask, metric_vectors


def input_audit(simulation):
    tables = simulation.dataset.datasets[2024]
    unknown = {}
    for entity in (
        "person",
        "household",
        "tax_unit",
        "spm_unit",
        "family",
        "marital_unit",
    ):
        columns = set(getattr(tables, entity).columns)
        unknown[entity] = sorted(
            columns - simulation.tax_benefit_system.variables.keys()
        )
    required = {
        "age",
        "state_fips",
        "household_weight",
        "household_id",
        "person_id",
        "person_household_id",
        "person_tax_unit_id",
        "person_spm_unit_id",
        "tax_unit_role_input",
        "employment_income_before_lsr",
        "self_employment_income_before_lsr",
    }
    missing = required - set(simulation.input_variables)
    if missing:
        raise ValueError(
            f"Critical native source inputs were not loaded: {sorted(missing)}"
        )
    return {
        "unknown_entity_columns": unknown,
        "unknown_unique_count": len(set().union(*map(set, unknown.values()))),
        "required_native_inputs_loaded": sorted(required),
        "legacy_input_renames": simulation.policyengine_bundle["legacy_input_renames"],
    }


def preflight(pe, identity, output):
    from policyengine_core.reforms import Reform
    from policyengine_us import CountryTaxBenefitSystem

    system = CountryTaxBenefitSystem()
    baseline = parameter_values(system)
    reform = Reform.from_dict(
        SCENARIOS["expanded_renewal_2029"].reform(), country_id="us"
    )
    reformed = CountryTaxBenefitSystem(reform=(reform,))
    changed = parameter_values(reformed)
    for year in ("2026", "2027", "2028"):
        if baseline[year] != changed[year]:
            raise ValueError(f"A future renewal changed pre-start year {year}")
    if baseline["2029"][f"{CTC}.in_effect"]:
        raise ValueError("Current law must expire in 2029")
    if not changed["2029"][f"{CTC}.in_effect"]:
        raise ValueError("Renewal did not activate in 2029")
    households = {}
    for name, scenario in SCENARIOS.items():
        # The wrapper household surface accepts flat current-year values.
        flat = {
            path: next(iter(value.values()))
            for path, value in scenario.reform().items()
        }
        kwargs = dict(
            people=[{"age": 40, "employment_income": 20_000}, {"age": 3}],
            tax_unit={"filing_status": "HEAD_OF_HOUSEHOLD"},
            household={"state_code": "OR", "county_fips": "41051"},
            year=scenario.year,
            extra_variables=["or_ctc"],
        )
        base = pe.us.calculate_household(**kwargs)
        ref = pe.us.calculate_household(**kwargs, reform=flat)
        expected = (
            baseline[str(scenario.year)][f"{CTC}.amount"]
            if name == "indexed_renewal_2029"
            else 1_200
        )
        if abs(ref.tax_unit.or_ctc - expected) > 0.01:
            raise ValueError(f"Synthetic credit check failed for {name}")
        if scenario.year == 2029 and base.tax_unit.or_ctc != 0:
            raise ValueError("Synthetic current-law 2029 credit did not expire")
        households[name] = {
            "baseline_ctc": base.tax_unit.or_ctc,
            "reform_ctc": ref.tax_unit.or_ctc,
        }
    write_json(
        output,
        {
            "runtime": identity.model_dump(),
            "parameters": baseline,
            "future_reform_parameters": changed,
            "synthetic_households": households,
            "status": "passed",
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )


def snapshot(pe, identity, year, region, output, scenario_name=None):
    scenario = SCENARIOS[scenario_name] if scenario_name else None
    if scenario:
        if year != scenario.year or region != "OR":
            raise ValueError("Policy snapshot must use its scenario's Oregon/year")
    progress("construct_start", scenario=scenario_name, year=year, region=region)
    simulation = pe.us.managed_microsimulation(
        dataset="populace_us_2024", dataset_end_year=2029
    )
    if scenario:
        apply_scenario(simulation, scenario)
    if simulation.baseline is not None:
        raise ValueError("A snapshot must retain only its own population branch")
    progress("construct_complete", scenario=scenario_name, year=year, region=region)
    source_inputs = input_audit(simulation)
    fingerprints = covariate_fingerprints(simulation, year)
    values, mask, accounting_vectors = totals(simulation, year, region)
    parameters = parameter_values(simulation.tax_benefit_system)
    bundle = simulation.policyengine_bundle
    health = bool(
        simulation.tax_benefit_system.parameters(
            f"{year}-01-01"
        ).gov.simulation.include_health_benefits_in_net_income
    )
    records = len(mask[mask])
    vector = accounting_vectors["household_net_income"]
    # All model calculations and metadata extraction are finished. The saved
    # native vectors own the values/indexes/weights needed by the receipt.
    del simulation, mask
    gc.collect()
    progress("simulation_released", scenario=scenario_name, year=year, region=region)
    check, absolute_masses = native_accounting_check(accounting_vectors)
    vector_path = Path(output).with_suffix(".households.pkl")
    vector_path.parent.mkdir(parents=True, exist_ok=True)
    # Native MicroSeries serialization preserves its weights and row index.
    # This ignored local file contains microdata and must never be published.
    vector_path.write_bytes(pickle.dumps(vector, protocol=pickle.HIGHEST_PROTOCOL))
    components_path = Path(output).with_suffix(".components.pkl")
    components_path.write_bytes(
        pickle.dumps(accounting_vectors, protocol=pickle.HIGHEST_PROTOCOL)
    )
    write_json(
        output,
        {
            "schema_version": 1,
            "runtime": identity.model_dump(),
            "year": year,
            "region": region,
            "scenario": scenario_name,
            "reform": scenario.reform() if scenario else {},
            "totals": values,
            "household_records": records,
            "covariate_fingerprints": fingerprints,
            "source_input_audit": source_inputs,
            "household_vector_file": vector_path.name,
            "household_vector_sha256": sha256(vector_path),
            "accounting_components_file": components_path.name,
            "accounting_components_sha256": sha256(components_path),
            "health_in_household_benefits": health,
            "parameters": parameters,
            "bundle": bundle,
            "simulation_configuration": {
                "initialization": "managed_current_law_then_apply_reform"
                if scenario
                else "managed_current_law",
                "post_calculation_lifecycle": "release_before_receipt_validation",
                "dataset_end_year": 2029,
            },
            "accounting_residual": check["residual"],
            "accounting_check": check,
            "accounting_absolute_masses": absolute_masses,
            "accounting_status": "passed" if check["passed"] else "failed",
            "analysis_runner_sha256": sha256(__file__),
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    if not check["passed"]:
        raise ValueError(
            f"Household accounting fails: {check}; actual checkpoint saved for audit"
        )
    progress("snapshot_complete", scenario=scenario_name, year=year, region=region)


def apply_scenario(simulation, scenario):
    """Use the engine's isolated parameter-write and cache invalidation path."""
    from policyengine_core.reforms import Reform

    simulation.apply_reform(Reform.from_dict(scenario.reform(), country_id="us"))


def split_preflight(identity, output):
    from policyengine.tax_benefit_models.us.household import _build_situation
    from policyengine_core.reforms import Reform
    from policyengine_us import CountryTaxBenefitSystem, Simulation

    scenario = SCENARIOS["expanded_renewal_2029"]
    groups = {
        entity: [{"members": [0, 1]}]
        for entity in ("marital_unit", "family", "spm_unit", "tax_unit", "household")
    }
    groups["household"][0].update(state_code="OR", county_fips="41051")
    groups["tax_unit"][0].update(filing_status="HEAD_OF_HOUSEHOLD")
    situation = _build_situation(
        people=[{"age": 40, "employment_income": 20_000}, {"age": 3}],
        groups=groups,
        year=scenario.year,
    )
    standard = Simulation(situation=situation, reform=scenario.reform())
    system = CountryTaxBenefitSystem(
        reform=(Reform.from_dict(scenario.reform(), country_id="us"),)
    )
    single = Simulation(situation=situation, tax_benefit_system=system)
    if single.baseline is not None:
        raise ValueError("Explicit-system constructor retained a baseline population")
    if parameter_values(standard.tax_benefit_system) != parameter_values(
        single.tax_benefit_system
    ):
        raise ValueError("Explicit-system policy parameters differ")
    checked = {}
    for variable in ("or_ctc", "or_agi", "state_income_tax", "household_net_income"):
        left, right = (
            simulation.calculate(variable, scenario.year)
            for simulation in (standard, single)
        )
        if not (left == right).all():
            raise ValueError(f"Explicit-system household values differ for {variable}")
        checked[variable] = float(right[0])
    write_json(
        output,
        {"status": "passed", "runtime": identity.model_dump(), "synthetic": checked},
    )


def load_snapshot(path):
    payload = json.loads(Path(path).read_text())
    required = {"accounting_components_file", "accounting_components_sha256"}
    if (
        not required <= payload.keys()
        or payload.get("accounting_check", {}).get("method")
        != "exact_core_float32_identity_per_household"
    ):
        raise ValueError("Snapshot requires an exact per-household accounting receipt")
    vectors = load_metric_vectors(path, payload)
    check, masses = native_accounting_check(vectors)
    if (
        not check["passed"]
        or check != payload["accounting_check"]
        or masses != payload["accounting_absolute_masses"]
        or payload.get("accounting_status") != "passed"
    ):
        raise ValueError("Per-household accounting receipt does not reproduce/pass")
    reference = vectors["household_net_income"]
    if len(reference) != payload["household_records"]:
        raise ValueError("Snapshot raw household count differs from actual vectors")
    for name in VARIABLES:
        value = vectors[name]
        if not value.index.equals(reference.index) or not value.weights.equals(
            reference.weights
        ):
            raise ValueError("Saved metric vectors have different indexes or weights")
        if value.isna().any() or value.isin([float("inf"), -float("inf")]).any():
            raise ValueError("Saved metric vectors must be finite")
        if float(value.sum()) != payload["totals"][name]:
            raise ValueError(
                "Snapshot metric/component totals differ from actual vectors"
            )
    if float((reference == reference).sum()) != payload["totals"]["households"]:
        raise ValueError("Snapshot household count differs from actual vectors")
    if payload["accounting_residual"] != check["residual"]:
        raise ValueError("Snapshot aggregate residual differs from actual vectors")
    return payload


def load_metric_vectors(path, payload):
    from microdf import MicroSeries

    components = Path(path).parent / payload["accounting_components_file"]
    if components.parent.resolve() != Path(path).parent.resolve():
        raise ValueError("Accounting vectors must be beside their snapshot")
    if sha256(components) != payload["accounting_components_sha256"]:
        raise ValueError("Accounting component fingerprint mismatch")
    vectors = pickle.loads(components.read_bytes())
    if not set(VARIABLES) <= vectors.keys() or not all(
        isinstance(vectors[name], MicroSeries) for name in VARIABLES
    ):
        raise ValueError("Every saved metric must retain its native MicroSeries")
    return vectors


def validate_snapshot_pair(base, ref):
    if any(row.get("accounting_status") != "passed" for row in (base, ref)):
        raise ValueError("Snapshots have not passed their accounting checks")
    if (base["year"], base["region"]) != (ref["year"], ref["region"]):
        raise ValueError("Snapshot year/region mismatch")
    if base["runtime"]["data_sha256"] != ref["runtime"]["data_sha256"]:
        raise ValueError("Snapshot data differ")
    if base["covariate_fingerprints"] != ref["covariate_fingerprints"]:
        raise ValueError("Source covariates, memberships or weights differ")
    if base["health_in_household_benefits"] != ref["health_in_household_benefits"]:
        raise ValueError("Health accounting settings differ")


def load_household_vector(snapshot_path, snapshot):
    from microdf import MicroSeries

    path = Path(snapshot_path).parent / snapshot["household_vector_file"]
    if path.parent.resolve() != Path(snapshot_path).parent.resolve():
        raise ValueError("Household vector must be beside its snapshot")
    if sha256(path) != snapshot["household_vector_sha256"]:
        raise ValueError("Native household vector fingerprint mismatch")
    # Only load files produced locally by this script, never untrusted pickles.
    values = pickle.loads(path.read_bytes())
    if not isinstance(values, MicroSeries):
        raise ValueError("Household values lost their native MicroSeries type")
    if len(values) != snapshot["household_records"]:
        raise ValueError("Native vector row count differs from its actual snapshot")
    if abs(float(values.sum()) - snapshot["totals"]["household_net_income"]) > 0.005:
        raise ValueError("Native vector aggregate differs from its actual snapshot")
    component = load_metric_vectors(snapshot_path, snapshot)["household_net_income"]
    if not values.equals(component) or not values.weights.equals(component.weights):
        raise ValueError("Gain vector differs from its actual metric vector")
    return values


def comparison(left, right, name, output):
    scenario = SCENARIOS[name]
    base_snapshot, ref_snapshot = (load_snapshot(path) for path in (left, right))
    validate_snapshot_pair(base_snapshot, ref_snapshot)
    if base_snapshot["runtime"] != ref_snapshot["runtime"]:
        raise ValueError("Policy snapshots must use the identical model runtime")
    if base_snapshot["reform"] or ref_snapshot["reform"] != scenario.reform():
        raise ValueError("Policy snapshots do not match current law and this reform")
    if ref_snapshot["scenario"] != name or ref_snapshot["year"] != scenario.year:
        raise ValueError("Policy snapshot scenario/year mismatch")
    base_vector = load_household_vector(left, base_snapshot)
    ref_vector = load_household_vector(right, ref_snapshot)
    if not base_vector.index.equals(ref_vector.index) or not base_vector.weights.equals(
        ref_vector.weights
    ):
        raise ValueError("Native household row indexes or weights differ")
    base, ref = base_snapshot["totals"], ref_snapshot["totals"]
    changes = {variable: ref[variable] - base[variable] for variable in base}
    residuals = {
        "baseline": accounting_residual(base),
        "reform": accounting_residual(ref),
    }
    gain = ref_vector - base_vector
    health = base_snapshot["health_in_household_benefits"]
    result = ComparisonResult(
        scenario=name,
        year=scenario.year,
        limitations=LIMITATIONS,
        assumptions=ASSUMPTIONS,
        runtime=RuntimeIdentity.model_validate(ref_snapshot["runtime"]),
        reform=scenario.reform(),
        baseline_totals=base,
        reform_totals=ref,
        changes=changes,
        budget=budget_impact(changes, health_in_household_benefits=health),
        households_gaining=float((gain > GAIN_THRESHOLD).sum()),
        households_losing=float((gain < -GAIN_THRESHOLD).sum()),
        oregon_household_records=len(gain),
        covariates_preserved=True,
        accounting_residuals=residuals,
        completed_at=datetime.now(UTC).isoformat(),
    )
    write_json(
        output,
        {
            **result.model_dump(),
            "bundle": ref_snapshot["bundle"],
            "baseline_parameters": base_snapshot["parameters"],
            "reform_parameters": ref_snapshot["parameters"],
            "snapshot_sha256": {"baseline": sha256(left), "reform": sha256(right)},
            "household_classification": {
                "gaining": "annual net-income change > $0.01",
                "losing": "annual net-income change < -$0.01",
            },
            "accounting_checks": {
                "baseline": base_snapshot["accounting_check"],
                "reform": ref_snapshot["accounting_check"],
            },
        },
    )


def compare_snapshots(left, right, output):
    base, ref = (load_snapshot(path) for path in (left, right))
    validate_snapshot_pair(base, ref)
    changes = {
        variable: ref["totals"][variable] - value
        for variable, value in base["totals"].items()
    }
    budget = budget_impact(
        changes, health_in_household_benefits=base["health_in_household_benefits"]
    )
    write_json(
        output,
        {
            "scenario": "cpi_correction_2026",
            "year": base["year"],
            "region": base["region"],
            "baseline_runtime": base["runtime"],
            "candidate_runtime": ref["runtime"],
            "baseline_totals": base["totals"],
            "candidate_totals": ref["totals"],
            "changes": changes,
            "budget": budget.model_dump(),
            "covariates_preserved": True,
            "limitations": LIMITATIONS,
            "assumptions": ASSUMPTIONS,
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("preflight", "split-preflight", "scenario", "snapshot", "compare"),
    )
    parser.add_argument(
        "--role", choices=("baseline", "candidate"), default="candidate"
    )
    parser.add_argument("--scenario", choices=tuple(SCENARIOS))
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--region", choices=("OR", "us"), default="OR")
    parser.add_argument("--baseline-snapshot", type=Path)
    parser.add_argument("--candidate-snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode in {"compare", "scenario"}:
        if not args.baseline_snapshot or not args.candidate_snapshot:
            parser.error("Comparison needs both saved actual snapshots")
        if args.mode == "scenario":
            if args.scenario is None:
                parser.error("scenario mode needs --scenario")
            comparison(
                args.baseline_snapshot,
                args.candidate_snapshot,
                args.scenario,
                args.output,
            )
            return
        compare_snapshots(args.baseline_snapshot, args.candidate_snapshot, args.output)
        return
    pe, identity = activate(args.role)
    if args.mode == "preflight":
        preflight(pe, identity, args.output)
    elif args.mode == "split-preflight":
        split_preflight(identity, args.output)
    else:
        snapshot(pe, identity, args.year, args.region, args.output, args.scenario)


if __name__ == "__main__":
    main()
