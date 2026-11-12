"""Aggregate-only result schema; no household records enter result files."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .accounting import BudgetImpact

DATA_SHA256 = "6496cc4393d4d3c6574f76eca231de5898c803b9067645591fd5c4d3e65aee84"
DATA_REVISION = "populace-us-2024-spm-20260915"
WRAPPER_VERSION = "6.17.2"
WRAPPER_COMMIT = "5b6d7901cf93ffbc903874aa305689c03fcb9247"
REGIONAL_WRAPPER_COMMIT = "e5e18f80070c50c353f2d65130f0f10e180df770"


class RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wrapper_version: Literal["6.17.2"]
    wrapper_source_commit: Literal[WRAPPER_COMMIT, REGIONAL_WRAPPER_COMMIT]
    wrapper_wheel_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    wrapper_build_status: Literal["official_release", "unreleased_source_build"] = (
        "official_release"
    )
    country_version: str
    country_source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    country_wheel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    core_version: str
    spm_version: Literal["1.1.0"]
    spm_forecast_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_sha256: Literal[DATA_SHA256]
    data_revision: Literal[DATA_REVISION]
    certification: Literal["unverified_development_fixture"]
    data_certification: Literal["not_certified"]

    @model_validator(mode="after")
    def wrapper_provenance(self):
        patched = self.wrapper_source_commit == REGIONAL_WRAPPER_COMMIT
        if patched != (self.wrapper_build_status == "unreleased_source_build"):
            raise ValueError(
                "Patched wrapper must be labeled an unreleased source build"
            )
        if patched and self.wrapper_wheel_sha256 is None:
            raise ValueError("Patched wrapper requires its authenticated wheel SHA")
        return self


class ComparisonResult(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    schema_version: Literal[1] = 1
    scenario: str
    year: int = Field(ge=2026, le=2029)
    region: Literal["OR"] = "OR"
    dataset: Literal["populace_us_2024"] = "populace_us_2024"
    limitations: list[str]
    assumptions: list[str]
    runtime: RuntimeIdentity
    reform: dict[str, dict[str, int | bool]]
    baseline_totals: dict[str, float]
    reform_totals: dict[str, float]
    changes: dict[str, float]
    budget: BudgetImpact
    households_gaining: float = Field(ge=0)
    households_losing: float = Field(ge=0)
    oregon_household_records: int = Field(ge=1)
    covariates_preserved: bool
    accounting_residuals: dict[str, float]
    completed_at: str
