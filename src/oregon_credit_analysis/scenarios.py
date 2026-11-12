"""Each scenario defines a complete policy and an evaluation year."""

from dataclasses import dataclass

CTC = "gov.states.or.tax.income.credits.ctc"
FOREVER = "2100-12-31"


@dataclass(frozen=True)
class Scenario:
    name: str
    year: int
    start_year: int
    renewal: bool = False
    expansion: bool = False

    def reform(self) -> dict[str, dict[str, int | bool]]:
        window = f"{self.start_year}-01-01.{FOREVER}"
        changes = {}
        if self.renewal:
            changes[f"{CTC}.in_effect"] = {window: True}
        if self.expansion:
            changes[f"{CTC}.amount"] = {window: 1_200}
            changes[f"{CTC}.reduction.start"] = {window: 40_000}
        return changes


SCENARIOS = {
    s.name: s
    for s in (
        Scenario("expansion_2027", 2027, 2027, expansion=True),
        Scenario("expansion_2028", 2028, 2027, expansion=True),
        Scenario("indexed_renewal_2029", 2029, 2029, renewal=True),
        Scenario("expanded_renewal_2029", 2029, 2029, renewal=True, expansion=True),
    )
}
