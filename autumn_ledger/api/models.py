from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogWrite(StrictModel):
    value: dict[str, Any]
    expected_version: StrictInt = 0
    actor: StrictStr = "operator"


class CalendarWrite(StrictModel):
    value: dict[str, Any]
    expected_version: StrictInt = 0
    actor: StrictStr = "operator"


class CoverageWrite(StrictModel):
    value: dict[str, Any]
    expected_version: StrictInt = 0
    actor: StrictStr = "operator"


class EventBatch(StrictModel):
    batch_id: StrictStr
    events: list[dict[str, Any]] = Field(min_length=1, max_length=5000)
    actor: StrictStr = "operator"


class FileBatch(StrictModel):
    batch_id: StrictStr
    content: StrictStr = Field(max_length=10000000)
    actor: StrictStr = "operator"


class FileInspect(StrictModel):
    content: StrictStr = Field(max_length=10000000)


class AnalysisRequest(StrictModel):
    name: StrictStr
    start: StrictStr
    end: StrictStr
    cities: list[StrictStr]
    sources: list[StrictStr]
    destinations: list[StrictStr] = Field(default_factory=list)
    family_only: StrictBool = True
    minimum_orders: StrictInt = 1
    revision: StrictInt | None = None
    baseline_start: StrictStr | None = None
    baseline_end: StrictStr | None = None


class EditionRequest(StrictModel):
    request_key: StrictStr
    request: AnalysisRequest


class DecisionRequest(StrictModel):
    action: Literal["approve", "reject", "reopen"]
    expected_version: StrictInt
    actor: StrictStr
    note: StrictStr


class PublishRequest(StrictModel):
    channel: StrictStr
    expected_version: StrictInt
    expected_generation: StrictInt
    actor: StrictStr


class RetireRequest(StrictModel):
    expected_version: StrictInt
    actor: StrictStr
    note: StrictStr


class EditionComparison(StrictModel):
    left: StrictStr
    right: StrictStr


class ProfileRequest(StrictModel):
    request: AnalysisRequest
    dimension: Literal["origin", "destination", "source"] = "origin"


class AnomalyRequest(StrictModel):
    request: AnalysisRequest
    minimum_orders: StrictInt = 3
    multiplier: float = 3.0
