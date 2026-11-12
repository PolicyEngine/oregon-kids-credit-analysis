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
uv run --extra test pytest
```

The runtime bootstrap authenticates the official PolicyEngine source distribution and SPM wheel against PyPI hashes, then uses PolicyEngine's [development fixture](https://github.com/PolicyEngine/policyengine.py/blob/5b6d7901cf93ffbc903874aa305689c03fcb9247/tests/fixtures/spm_development.py). It checks the installed country/calculator files against their wheel bytes. It preserves existing environments.

All aggregates use `MicroSeries` operations and `map_to`. Budget accounting includes taxes, cash and in-kind benefits, and shared government health costs, following [PolicyEngine's budgetary-impact definition](https://github.com/PolicyEngine/policyengine.py/blob/5b6d7901cf93ffbc903874aa305689c03fcb9247/src/policyengine/tax_benefit_models/us/analysis.py). Direct credit outlays remain a separate measure.

Population runs use the host's heavy-run wrapper and execute one comparison per process. Local runtime files, microdata and draft results stay outside version control. This repository currently publishes code; numerical results require completed model runs and review.
