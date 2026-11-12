# Oregon Kids' Credit analysis

Run four Oregon child tax credit scenarios with PolicyEngine: a $1,200 credit and $40,000 phase-out start in 2027 and 2028, indexed renewal in 2029, and renewal with the expanded values in 2029. A separate comparison measures the 2026 effect of the CPI-U correction in [PolicyEngine-US #10648](https://github.com/PolicyEngine/policyengine-us/pull/10648).

The runtime uses PolicyEngine 6.17.2 and an authenticated development wheel of the country model. Each output records `unverified_development_fixture` and `not_certified`; the country-model/data pair has no production certification. The data retain the verified national release `populace-us-2024-spm-20260915`, with roughly 57,000 households. Oregon estimates use that national sample and need sampling caution. They do not reproduce an ACS-local website report.

## Scenarios

| Scenario | Year | Changes |
| --- | --- | --- |
| Expansion | 2027 | $1,200 per child; $40,000 phase-out start; changes begin in 2027 |
| Expansion | 2028 | Evaluate the same expansion in 2028 |
| Indexed renewal | 2029 | Continue the credit; retain the model's indexed amounts and thresholds |
| Expanded renewal | 2029 | Continue the credit at $1,200 per child and a $40,000 phase-out start |

Each comparison uses current law under the same country-model wheel. Oregon's credit ends after 2028 under [HB 3235, section 11](https://olis.oregonlegislature.gov/liz/2023R1/Downloads/MeasureDocument/HB3235/Enrolled). [ORS 315.273(5)](https://www.oregonlegislature.gov/bills_laws/ors/ors315.html) specifies its inflation adjustment.

## Reproduction

The analysis uses `uv` and Python 3.13. Tests exercise policy dates, weighted filtering, accounting identities and result labels:

```bash
python scripts/bootstrap_runtime.py --prepare-only
uv run --locked --extra test pytest
```

The runtime bootstrap authenticates the official PolicyEngine source distribution and SPM wheel against PyPI hashes, then uses PolicyEngine's [development fixture](https://github.com/PolicyEngine/policyengine.py/blob/5b6d7901cf93ffbc903874aa305689c03fcb9247/tests/fixtures/spm_development.py). It checks the installed country/calculator files against their wheel bytes. It preserves existing environments.

Each model needs a clean source checkout, its exact commit, and a wheel built from that checkout. Prepare each role with:

```bash
uv run --locked python scripts/bootstrap_runtime.py --role candidate \
  --model-source /path/to/policyengine-us --model-commit COMMIT_SHA \
  --model-wheel /path/to/policyengine_us.whl
```

Use `--role baseline` for the comparison model. Both roles use Core 3.34.2 and SPM Calculator 1.1.0. The generated development manifest records the installed Core wheel and SPM forecast fingerprint. Configuration passes through the unchanged upstream validators; the fixture and forecast data remain unchanged. The bootstrap verifies installed Core and calculator files against their authenticated wheels before deriving these settings.

Place the verified source data at `data/populace_us_2024.h5`. Launch the household preflight from the authenticated wrapper source directory, so Python resolves the upstream fixture:

```bash
export OREGON_ANALYSIS_ROOT="$PWD"
cd .runtime/policyengine-6.17.2
../env-candidate/bin/python -c 'import os, runpy; runpy.run_path(os.environ["OREGON_ANALYSIS_ROOT"] + "/scripts/run_analysis.py", run_name="__main__")' \
  preflight --role candidate --output "$OREGON_ANALYSIS_ROOT/results/preflight-candidate.json"
```

Population commands use the same launch form under the host's heavy-run wrapper. Run `snapshot --year 2027 --region OR` for current law, and `snapshot --year 2027 --region OR --scenario expansion_2027` for its reform, with separate output files and processes. Then run `scenario --scenario expansion_2027 --baseline-snapshot FILE --candidate-snapshot FILE --output FILE` to compare them. Repeat for the other scenarios. The two 2029 renewals may share the same actual current-law snapshot when its runtime and data fingerprints match.

Use `snapshot --year 2026 --region us` under each role for the national CPI comparison, then `compare --baseline-snapshot FILE --candidate-snapshot FILE --output FILE` to compare their actual aggregates. Snapshots retain native `MicroSeries` objects in ignored local pickle files so comparisons preserve household indexes and weights. These files contain microdata: keep them local and load only files created by this script. Set BLAS/OpenMP thread limits to at most four before launching; local runs use one thread.

All aggregates use `MicroSeries` operations and `map_to`. Budget accounting includes taxes, cash and in-kind benefits, and shared government health costs, following [PolicyEngine's budgetary-impact definition](https://github.com/PolicyEngine/policyengine.py/blob/5b6d7901cf93ffbc903874aa305689c03fcb9247/src/policyengine/tax_benefit_models/us/analysis.py). Direct credit outlays remain a separate measure.

Population runs use the host's heavy-run wrapper and execute one comparison per process. Local runtime files, microdata and draft results stay outside version control. This repository currently publishes code; numerical results require completed model runs and review.
