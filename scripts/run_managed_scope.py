#!/usr/bin/env python3
"""Score a native managed household cohort while retaining supplied history.

Run snapshots through the host heavy guard. Native record vectors stay in
ignored local files; public code contains no numerical findings or records.
"""

import argparse
import gc
import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_analysis as original  # noqa: E402
import run_regional_analysis as regional  # noqa: E402

from oregon_credit_analysis.regional import index_by_household_id  # noqa: E402

ENTITIES = ("person", "household", "tax_unit", "spm_unit", "family", "marital_unit")


def supplied_input_receipt(country):
    """Inspect actual supplied storage, excluding formula-cached periods."""
    import pandas as pd

    result = {}
    for name in sorted(country.input_variables):
        holder = country.get_holder(name)
        periods = {}
        for period in holder.get_input_periods(country.branch_name):
            values = holder.get_array(period, country.branch_name)
            if values is None:
                raise ValueError(f"Missing supplied storage for {name}/{period}")
            periods[str(period)] = {
                "rows": len(values),
                "dtype": str(values.dtype),
                "sha256": regional.digest_series(pd.Series(values)),
            }
        result[name] = {
            "entity": holder.variable.entity.key,
            "definition_period": str(holder.variable.definition_period),
            "periods": periods,
        }
    return result


def live_identifiers(country, year):
    """Bind the eleven live ID variables to native source-table values."""
    from types import SimpleNamespace

    single = country.dataset.datasets[year]
    view = SimpleNamespace(
        data=SimpleNamespace(
            entity_data={entity: getattr(single, entity) for entity in ENTITIES}
        )
    )
    return regional.live_identifier_receipt(country, view, year)


def snapshot(pe, identity, year, output, scenario_name=None):
    from policyengine.core import RowFilterStrategy

    scenario = original.SCENARIOS[scenario_name] if scenario_name else None
    if scenario and scenario.year != year:
        raise ValueError("Policy snapshot must use the scenario's scoring year")
    scope = RowFilterStrategy(variable_name="state_fips", variable_value=41)
    original.progress(
        "construct_start",
        year=year,
        scenario=scenario_name,
        method="managed_history_scope",
    )
    with regional.NativeObserver("prepare", year) as preparation:
        country = pe.us.managed_microsimulation(
            dataset="populace_us_2024",
            dataset_end_year=2029,
            scoping_strategy=scope,
        )
    if scenario:
        original.apply_scenario(country, scenario)
    if country.baseline is not None or not country.is_over_dataset:
        raise ValueError("Snapshot must retain a single native population arm")
    if country.dataset.years != list(range(2024, 2030)):
        raise ValueError("Managed scope did not retain the supplied 2024–2029 history")
    records = len(country.populations["household"].ids)
    ids = country.populations["household"].ids.copy()
    source_inputs = original.input_audit(country)
    source_inputs["actual_supplied_inputs"] = supplied_input_receipt(country)
    source_inputs["actual_live_identifiers"] = {
        str(period): live_identifiers(country, period)
        for period in country.dataset.years
    }
    fingerprints = original.covariate_fingerprints(country, year)
    context = {
        "initialization": "managed_native_history_scope_then_apply_reform"
        if scenario
        else "managed_native_history_scope",
        "dataset_end_year": 2029,
        "supplied_years": country.dataset.years,
        "actual_default_calculation_period": str(country.default_calculation_period),
        "actual_default_input_period": str(country.default_input_period),
        "population_mode": country.is_over_dataset,
        "retained_baseline_arm": False,
        "scope": country.policyengine_bundle["scoping"],
        "post_calculation_lifecycle": "release_before_receipt_validation",
    }
    original.progress(
        "construct_complete",
        year=year,
        scenario=scenario_name,
        household_records=records,
    )
    with regional.NativeObserver("snapshot", year, records) as execution:
        values, mask, native = original.totals(country, year, "OR")
    if len(mask[mask]) != records:
        raise ValueError("The native scoped population contains a non-Oregon household")
    native = {
        name: index_by_household_id(vector, ids) for name, vector in native.items()
    }
    parameters = original.parameter_values(country.tax_benefit_system)
    health = bool(
        country.tax_benefit_system.parameters(
            f"{year}-01-01"
        ).gov.simulation.include_health_benefits_in_net_income
    )
    bundle = country.policyengine_bundle
    del country, mask
    gc.collect()
    original.progress("simulation_released", year=year, scenario=scenario_name)
    check, masses = original.native_accounting_check(native)
    vector = native["household_net_income"]
    vector_path = Path(output).with_suffix(".households.pkl")
    components_path = Path(output).with_suffix(".components.pkl")
    vector_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (vector_path, components_path):
        if path.exists():
            raise ValueError("Refuse to overwrite an existing native-vector artifact")
    vector_path.write_bytes(pickle.dumps(vector, protocol=pickle.HIGHEST_PROTOCOL))
    components_path.write_bytes(pickle.dumps(native, protocol=pickle.HIGHEST_PROTOCOL))
    original.write_json(
        output,
        {
            "schema_version": 1,
            "runtime": identity.model_dump(),
            "year": year,
            "region": "OR",
            "scenario": scenario_name,
            "reform": scenario.reform() if scenario else {},
            "totals": values,
            "household_records": records,
            "covariate_fingerprints": fingerprints,
            "source_input_audit": source_inputs,
            "household_vector_file": vector_path.name,
            "household_vector_sha256": original.sha256(vector_path),
            "accounting_components_file": components_path.name,
            "accounting_components_sha256": original.sha256(components_path),
            "health_in_household_benefits": health,
            "parameters": parameters,
            "bundle": bundle,
            "simulation_configuration": context,
            "accounting_residual": check["residual"],
            "accounting_check": check,
            "accounting_absolute_masses": masses,
            "accounting_status": "passed" if check["passed"] else "failed",
            "execution_trace": {
                "preparation": preparation.receipt(),
                "financial": execution.receipt(),
            },
            "analysis_runner_sha256": original.sha256(__file__),
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    original.load_snapshot(output)
    original.progress("snapshot_complete", year=year, scenario=scenario_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("snapshot", "parity", "comparison", "preflight")
    )
    parser.add_argument("--year", type=int, choices=(2027, 2028, 2029), default=2027)
    parser.add_argument("--scenario", choices=tuple(original.SCENARIOS))
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--current", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refuse to overwrite an existing artifact")
    if args.mode in ("parity", "comparison"):
        if not args.previous or not args.current:
            parser.error("Comparison needs both actual snapshots")
        if args.mode == "parity":
            regional.parity(args.previous, args.current, args.output)
        else:
            if not args.scenario:
                parser.error("Policy comparison needs a scenario")
            original.comparison(args.previous, args.current, args.scenario, args.output)
        return
    pe, identity, runtime = regional.activate("candidate", "managed-scope")
    if args.mode == "preflight":
        original.write_json(
            args.output,
            {
                "status": "authenticated_imports_passed",
                "runtime": identity.model_dump(),
                "source": runtime["source"],
                "source_dataset_sha256": original.sha256(regional.SOURCE),
            },
        )
    else:
        snapshot(pe, identity, args.year, args.output, args.scenario)


if __name__ == "__main__":
    main()
