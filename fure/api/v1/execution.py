from typing import Annotated
from uuid import UUID

from fastapi import Query, Response
from fastapi.responses import JSONResponse

from fure.api.v1.presenters import run_response, run_result_response
from fure.api.v1.schemas import (
    ErrorResponse,
    RunDetailResponse,
    RunRequest,
    RunResponse,
)
from fure.core.auth import Permission, Principal
from fure.services.auth import require
from fure.services.errors import Unauthorized
from fure.services.execution import ExecutionService

type WaitForRun = Annotated[
    bool,
    Query(
        description="Wait for the result; false returns the run immediately and requires runs.read"
    ),
]

RUN_RESPONSES = {
    200: {
        "description": "Execution completed successfully",
        "headers": {
            "X-Run-Id": {
                "description": "Persisted execution ID for /v1/runs/{run_id}",
                "schema": {"type": "string", "format": "uuid"},
            }
        },
        "content": {
            "application/json": {
                "example": {
                    "output": "Hello! How can I help you today?",
                    "meta": {
                        "input_tokens": 10,
                        "output_tokens": 261,
                        "total_tokens": 271,
                    },
                    "duration": 0.223,
                }
            }
        },
    },
    202: {
        "model": RunDetailResponse,
        "description": "Execution accepted with wait=false; poll /v1/runs/{id} for its state",
        "headers": {"X-Run-Id": {"schema": {"type": "string", "format": "uuid"}}},
    },
    404: {"model": ErrorResponse, "description": "Function not found or deleted"},
    409: {
        "model": ErrorResponse,
        "description": "Function is disabled, has no current revision, or execution was cancelled",
    },
    500: {"model": ErrorResponse, "description": "Function execution failed"},
    502: {"model": ErrorResponse, "description": "LLM provider request failed"},
    504: {"model": ErrorResponse, "description": "Execution timed out"},
}


async def execute_function(
    function: UUID | str,
    request: RunRequest,
    service: ExecutionService,
    response: Response,
    principal: Principal | None,
    *,
    wait: bool,
) -> RunResponse | Response:
    if not wait:
        if principal is None:
            raise Unauthorized("Authentication required for asynchronous execution")
        require(principal, Permission.RUNS_READ)
        run = await service.submit(function, request.input, principal=principal)
        return JSONResponse(
            status_code=202,
            content=run_response(run, principal).model_dump(mode="json"),
            headers={"X-Run-Id": str(run.id)},
        )
    run = await service.execute(function, request.input, principal=principal)
    response.headers["X-Run-Id"] = str(run.id)
    return run_result_response(run, principal)
