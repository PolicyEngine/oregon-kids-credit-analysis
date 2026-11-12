# Oregon Kids' Credit analysis

Run four Oregon child tax credit scenarios with PolicyEngine: a $1,200 credit and $40,000 phase-out start in 2027 and 2028, indexed renewal in 2029, and renewal with the expanded values in 2029. A separate national comparison measures the 2026 effect of the CPI-U correction in [PolicyEngine-US #10648](https://github.com/PolicyEngine/policyengine-us/pull/10648).

Oregon runs use the country-model snapshot [b0dc6ed3](https://github.com/PolicyEngine/policyengine-us/tree/b0dc6ed39d9a0666cc767c56f599d9dc2ab9ef4f) and the history-preserving managed scoping implementation at [wrapper commit 628cbaf6](https://github.com/PolicyEngine/policyengine.py/tree/628cbaf63e31dce8477be4b0215c07bcc3b49d47). That source build reports version 6.18.0 and `unreleased_source_build`. Each output records `unverified_development_fixture` and `not_certified`; this model/data pairing has no production certification.

The input retains the verified national release `populace-us-2024-spm-20260915`, with 57,240 households. Oregon calculations select its 1,152 Oregon households and retain their native calibrated weights. The national sample can produce state sampling uncertainty. These estimates do not reproduce an ACS-local website report.

## Scenarios

| Scenario | Year | Changes |
| --- | --- | --- |
| Expansion | 2027 | $1,200 per child; $40,000 phase-out start; changes begin in 2027 |
| Expansion | 2028 | Evaluate the same expansion in 2028 |
| Indexed renewal | 2029 | Continue the credit; retain the model's indexed amounts and thresholds |
| Expanded renewal | 2029 | Continue the credit at $1,200 per child and a $40,000 phase-out start |

Each comparison uses current law under the same country-model wheel. Oregon's credit ends after 2028 under [HB 3235, section 11](https://olis.oregonlegislature.gov/liz/2023R1/Downloads/MeasureDocument/HB3235/Enrolled). [ORS 315.273(5)](https://www.oregonlegislature.gov/bills_laws/ors/ors315.html) specifies its inflation adjustment.

## Reproduction

Use `uv` and Python 3.13. Prepare the small authenticated reference artifact before running the lightweight tests:

```bash
python scripts/bootstrap_runtime.py --prepare-only
uv run --locked --extra test pytest
```

Build wheels from clean checkouts of the exact country and wrapper commits above. The bootstrap verifies each wheel against its source checkout and installed files, then uses the unchanged upstream [development fixture](https://github.com/PolicyEngine/policyengine.py/blob/628cbaf63e31dce8477be4b0215c07bcc3b49d47/tests/fixtures/spm_development.py). Prepare the Oregon runtime with:

```bash
uv run --locked python scripts/bootstrap_runtime.py --role candidate \
  --model-source /path/to/policyengine-us-at-b0dc6ed3 \
  --model-commit b0dc6ed39d9a0666cc767c56f599d9dc2ab9ef4f \
  --model-wheel /path/to/policyengine_us-2.117.4-py3-none-any.whl \
  --wrapper-source /path/to/policyengine.py-at-628cbaf6 \
  --wrapper-commit 628cbaf63e31dce8477be4b0215c07bcc3b49d47 \
  --wrapper-wheel /path/to/policyengine-6.18.0-py3-none-any.whl \
  --runtime-prefix managed-scope
```

This creates a separate environment with Core 3.34.2 and SPM Calculator 1.1.0. The generated manifest records the actual Core wheel and SPM forecast fingerprint; upstream validators check that configuration. The bootstrap preserves earlier runtime environments.

Place the verified source data at `data/populace_us_2024.h5`. Authenticate imports before population calculations:

```bash
.runtime/env-managed-scope-candidate/bin/python scripts/run_managed_scope.py \
  preflight --output results/managed-scope-preflight.json
```

Run each population snapshot through the host's heavy-run wrapper, in a separate process with one BLAS/OpenMP thread. Keep the wrapper's default memory ceiling. For example:

```bash
HEAVY_RUN=/path/to/heavy_run.sh
"$HEAVY_RUN" or-credit-baseline-2027 \
  .runtime/env-managed-scope-candidate/bin/python scripts/run_managed_scope.py \
  snapshot --year 2027 --output results/baseline-2027.json
"$HEAVY_RUN" or-credit-expansion-2027 \
  .runtime/env-managed-scope-candidate/bin/python scripts/run_managed_scope.py \
  snapshot --year 2027 --scenario expansion_2027 --output results/expanded-2027.json
.runtime/env-managed-scope-candidate/bin/python scripts/run_managed_scope.py \
  comparison --scenario expansion_2027 \
  --previous results/baseline-2027.json --current results/expanded-2027.json \
  --output results/expansion-2027-comparison.json
```

Repeat with `expansion_2028`, `indexed_renewal_2029` and `expanded_renewal_2029`, using each scenario's year. Both renewals can share the same actual 2029 current-law snapshot when its runtime and data fingerprints match.

The managed constructor retains every supplied year from 2024 through 2029, scopes all six entity tables to the same household cohort, and releases the national input simulation before constructing the scoped simulation. Earlier income enters Medicare IRMAA premiums and can affect SNAP's medical deduction. Each snapshot checks actual supplied periods, all eleven live identifiers, source weights and dated policy values. An execution observer rejects financial formulas before scoping.

## National CPI comparison

The national comparison uses official wrapper 6.17.2 and compares country snapshots [935b24ff](https://github.com/PolicyEngine/policyengine-us/tree/935b24ff4f022e0548876c0bd859195c517a23bb) and [b0dc6ed3](https://github.com/PolicyEngine/policyengine-us/tree/b0dc6ed39d9a0666cc767c56f599d9dc2ab9ef4f). Both runs retain the full national population and supplied history, with the same Core, SPM and data pins.

Prepare each role with `scripts/bootstrap_runtime.py --role baseline` or `--role candidate`, its exact model source/commit/wheel, and no wrapper override. From `.runtime/policyengine-6.17.2`, launch `scripts/run_analysis.py` by absolute path under the host heavy-run wrapper. Use `snapshot --role ROLE --year 2026 --region us --output FILE` for each role, then `compare --baseline-snapshot FILE --candidate-snapshot FILE --output FILE` for their actual aggregates.

## Accounting and local artifacts

All aggregates use native `MicroSeries` operations and `map_to`. Fiscal accounting includes taxes, cash and in-kind benefits, and shared government health costs, following [PolicyEngine's budgetary-impact definition](https://github.com/PolicyEngine/policyengine.py/blob/628cbaf63e31dce8477be4b0215c07bcc3b49d47/src/policyengine/tax_benefit_models/us/analysis.py). Direct Oregon credit outlays remain a separate measure. The credit calculation covers every eligible modeled tax unit without a separate filing or take-up adjustment; the data retain other programs' participation inputs.

A household gains when its annual net income increases by more than $0.01 and loses when it decreases by more than $0.01. Each snapshot verifies the net-income identity for every household in the model's exact float32 operation order. Saved native vectors reproduce every reported aggregate, index and weight.

Snapshots store native vectors in ignored local pickle files. Those files contain microdata; keep them local and load only files that these scripts created. Runtime environments, source data and numerical results stay outside version control.
