from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from fure.api.v1.schemas.common import PaginationRequest, RequestSchema
from fure.core.run import (
    CancelRunMeta,
    ErrorRunMeta,
    FailRunMeta,
    RunningRunMeta,
    RunStatus,
)
from fure.core.runners.jinja.openai import OpenAIJinjaRunMeta
from fure.core.runners.python.inline import InlinePythonRunMeta

type SuccessMeta = OpenAIJinjaRunMeta | InlinePythonRunMeta
type RunMeta = SuccessMeta | ErrorRunMeta | FailRunMeta | CancelRunMeta | RunningRunMeta


class RunRequest(RequestSchema):
    input: dict[str, JsonValue] | None = None


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    output: JsonValue
    meta: SuccessMeta
    duration: float = Field(ge=0, allow_inf_nan=False)


class RunListRequest(PaginationRequest):
    function_id: UUID | None = None
    revision_id: UUID | None = None
    status: RunStatus | None = None
    parent_run_id: UUID | None = None


class RunDetailResponse(BaseModel):
    id: UUID
    function_id: UUID
    revision_id: UUID
    user_id: UUID | None
    api_key_id: UUID | None
    parent_run_id: UUID | None
    tool_call_index: int | None
    uses_tools: bool
    input: dict[str, JsonValue] | None
    status: RunStatus
    output: JsonValue
    meta: RunMeta | None
    duration: float | None = Field(ge=0, allow_inf_nan=False)
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
