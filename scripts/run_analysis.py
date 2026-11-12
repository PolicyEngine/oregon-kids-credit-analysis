#!/usr/bin/env python3
"""Run one actual comparison in its authenticated PolicyEngine environment.

Launch with cwd at the authenticated wrapper source root, using that role's
interpreter. The host heavy-run wrapper must own every population command.
"""

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oregon_credit_analysis.accounting import (  # noqa: E402
    BUDGET_VARIABLES,
    accounting_residual,
    budget_impact,
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
    "Every filer who satisfies Oregon credit eligibility claims the calculated credit; "
    "or_ctc has no separate take-up adjustment.",
    "The source data carry other programs’ participation inputs unchanged.",
]
LIMITATIONS = [
    "Unverified development model/data pair; not a production-certified estimate.",
    "Oregon estimate uses the roughly 57,000-household national dataset; "
    "state sampling uncertainty may affect the result.",
    "This run does not reproduce an ACS-local website report.",
    "No behavioral response parameters were changed by the analysis.",
]


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


def covariate_fingerprints(simulation):
    # Serialization serves only identity checks; every estimate retains MicroSeries.
    return {
        f"{entity}.{variable}": hashlib.sha256(
            simulation.calc(variable, period=2024, map_to=entity)
            .to_json(orient="split", double_precision=15)
            .encode()
        ).hexdigest()
        for variable, entity in COVARIATES
    }


def totals(simulation, year, region):
    geography = simulation.calc("state_fips", period=year, map_to="household")
    mask = geography == 41 if region == "OR" else geography == geography
    output = {}
    for variable in VARIABLES:
        values = simulation.calc(variable, period=year, map_to="household")
        if values.isna().any():
            raise ValueError(f"Missing {variable} values before geographic aggregation")
        output[variable] = float(values[mask].sum())
    output["households"] = float((geography[mask] == geography[mask]).sum())
    return output, mask


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


def snapshot(pe, identity, year, region, output):
    simulation = pe.us.managed_microsimulation(
        dataset="populace_us_2024", dataset_end_year=2029
    )
    values, mask = totals(simulation, year, region)
    residual = accounting_residual(values)
    if abs(residual) > 5:
        raise ValueError(f"Household accounting fails by {residual}")
    write_json(
        output,
        {
            "schema_version": 1,
            "runtime": identity.model_dump(),
            "year": year,
            "region": region,
            "totals": values,
            "household_records": len(mask[mask]),
            "covariate_fingerprints": covariate_fingerprints(simulation),
            "health_in_household_benefits": bool(
                simulation.tax_benefit_system.parameters(
                    f"{year}-01-01"
                ).gov.simulation.include_health_benefits_in_net_income
            ),
            "parameters": parameter_values(simulation.tax_benefit_system),
            "bundle": simulation.policyengine_bundle,
            "accounting_residual": residual,
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )


def comparison(pe, identity, name, output):
    scenario = SCENARIOS[name]
    simulation = pe.us.managed_microsimulation(
        dataset="populace_us_2024", dataset_end_year=2029, reform=scenario.reform()
    )
    baseline = simulation.baseline
    if baseline is None:
        raise ValueError("Expected the reform's actual baseline branch")
    covariates = covariate_fingerprints(baseline)
    if covariates != covariate_fingerprints(simulation):
        raise ValueError(
            "Policy changes altered source covariates or native memberships"
        )
    base, mask = totals(baseline, scenario.year, "OR")
    ref, other_mask = totals(simulation, scenario.year, "OR")
    if not mask.equals(other_mask):
        raise ValueError("Baseline and reform geographic rows differ")
    changes = {variable: ref[variable] - base[variable] for variable in base}
    residuals = {
        "baseline": accounting_residual(base),
        "reform": accounting_residual(ref),
    }
    if any(abs(value) > 5 for value in residuals.values()):
        raise ValueError(f"Household accounting fails: {residuals}")
    gain = simulation.calc(
        "household_net_income", period=scenario.year, map_to="household"
    ) - baseline.calc("household_net_income", period=scenario.year, map_to="household")
    health = bool(
        baseline.tax_benefit_system.parameters(
            f"{scenario.year}-01-01"
        ).gov.simulation.include_health_benefits_in_net_income
    )
    result = ComparisonResult(
        scenario=name,
        year=scenario.year,
        limitations=LIMITATIONS,
        assumptions=ASSUMPTIONS,
        runtime=identity,
        reform=scenario.reform(),
        baseline_totals=base,
        reform_totals=ref,
        changes=changes,
        budget=budget_impact(changes, health_in_household_benefits=health),
        households_gaining=float((gain[mask] > 0.01).sum()),
        households_losing=float((gain[mask] < -0.01).sum()),
        oregon_household_records=len(mask[mask]),
        covariates_preserved=True,
        accounting_residuals=residuals,
        completed_at=datetime.now(UTC).isoformat(),
    )
    write_json(
        output,
        {
            **result.model_dump(),
            "bundle": simulation.policyengine_bundle,
            "baseline_parameters": parameter_values(baseline.tax_benefit_system),
            "reform_parameters": parameter_values(simulation.tax_benefit_system),
        },
    )


def compare_snapshots(left, right, output):
    base, ref = (json.loads(Path(path).read_text()) for path in (left, right))
    if (base["year"], base["region"]) != (ref["year"], ref["region"]):
        raise ValueError("Snapshot year/region mismatch")
    if base["runtime"]["data_sha256"] != ref["runtime"]["data_sha256"]:
        raise ValueError("Snapshot data differ")
    if base["covariate_fingerprints"] != ref["covariate_fingerprints"]:
        raise ValueError("Source covariates differ between model snapshots")
    changes = {
        variable: ref["totals"][variable] - value
        for variable, value in base["totals"].items()
    }
    if base["health_in_household_benefits"] != ref["health_in_household_benefits"]:
        raise ValueError("Health accounting settings differ")
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
        "mode", choices=("preflight", "scenario", "snapshot", "compare")
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
    if args.mode == "compare":
        compare_snapshots(args.baseline_snapshot, args.candidate_snapshot, args.output)
        return
    pe, identity = activate(args.role)
    if args.mode == "preflight":
        preflight(pe, identity, args.output)
    elif args.mode == "scenario":
        if args.scenario is None:
            parser.error("scenario mode needs --scenario")
        comparison(pe, identity, args.scenario, args.output)
    else:
        snapshot(pe, identity, args.year, args.region, args.output)


if __name__ == "__main__":
    main()
