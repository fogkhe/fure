from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from fure.api.dependencies import (
    ExecutionServiceDep,
    PrincipalDep,
    RunServiceDep,
    require_permission,
)
from fure.api.v1.presenters import run_response
from fure.api.v1.schemas import (
    ErrorResponse,
    PaginationResponse,
    RunDetailResponse,
    RunListRequest,
)
from fure.core.auth import Permission

router = APIRouter()


@router.get(
    "",
    summary="List execution history",
    dependencies=[Depends(require_permission(Permission.RUNS_READ))],
)
async def list_runs(
    request: Annotated[RunListRequest, Query()],
    service: RunServiceDep,
    principal: PrincipalDep,
) -> PaginationResponse[RunDetailResponse]:
    """List runs, newest first. With parent_run_id, list tool runs in call order."""
    items, total = await service.list(**request.model_dump())
    return PaginationResponse(
        items=[run_response(item, principal) for item in items],
        total=total,
        page=request.page,
        size=request.size,
    )


@router.get(
    "/{run_id}",
    summary="Get a run",
    dependencies=[Depends(require_permission(Permission.RUNS_READ))],
    responses={404: {"model": ErrorResponse, "description": "Run not found"}},
)
async def get_run(
    run_id: Annotated[UUID, Path()],
    service: RunServiceDep,
    principal: PrincipalDep,
) -> RunDetailResponse:
    """Get input, output, metadata, status and timestamps for a specific run."""
    return run_response(await service.get(run_id), principal)


@router.post(
    "/{run_id}/cancel",
    summary="Cancel a pending or running execution",
    dependencies=[Depends(require_permission(Permission.RUNS_READ))],
    responses={
        404: {"model": ErrorResponse, "description": "Run not found"},
        409: {"model": ErrorResponse, "description": "Run has already finished"},
    },
)
async def cancel_run(
    run_id: Annotated[UUID, Path()],
    service: ExecutionServiceDep,
    principal: PrincipalDep,
) -> RunDetailResponse:
    return run_response(await service.cancel(run_id, principal=principal), principal)
