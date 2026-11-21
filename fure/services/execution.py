import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from time import monotonic
from uuid import UUID, uuid7

from jinja2 import UndefinedError
from openai import APIError, APITimeoutError
from sqlalchemy import and_, or_, select, update

from fure.core.auth import Permission, Principal
from fure.core.errors import InputValidationError
from fure.core.revision import Revision
from fure.core.run import (
    CancelRunMeta,
    ErrorRunMeta,
    Run,
    RunAssistantComment,
    RunStatus,
    RunToolCall,
)
from fure.core.runners import (
    InlinePythonRunner,
    OpenAIJinjaRunner,
    OpenAIJinjaRunnerOptions,
)
from fure.core.runners.jinja.openai import AgentLimitExceeded, OpenAIJinjaTool
from fure.core.types import JSONValue
from fure.database.connection import Database
from fure.database.mappers import meta_to_data
from fure.database.models import RunRow
from fure.database.repositories import (
    ACTIVE_STATUSES,
    FunctionRepository,
    RevisionRepository,
    RunRepository,
)
from fure.services.auth import require_function_access
from fure.services.errors import (
    Conflict,
    ExecutionCancelled,
    ExecutionFailed,
    ExecutionTimedOut,
    Forbidden,
    InvalidInput,
    NotFound,
    ProviderFailed,
    ServiceError,
    Unauthorized,
    Unavailable,
)
from fure.services.variables import VariableService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    revision: Revision
    parent_run_id: UUID
    index: int
    ancestors: frozenset[UUID]
    step: int


class ExecutionService:
    def __init__(
        self,
        database: Database,
        *,
        variables: VariableService | None = None,
        heartbeat_interval: float = 1.0,
        lease_seconds: float = 30.0,
    ) -> None:
        self.database = database
        self.variables = (
            variables if variables is not None else VariableService(database)
        )
        self.worker_id = uuid7()
        self.heartbeat_interval = heartbeat_interval
        self.lease_seconds = lease_seconds
        self._tasks: dict[UUID, asyncio.Task[Run]] = {}
        self._monitor_task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        await self._reap_stale()
        self._monitor_task = asyncio.create_task(
            self._monitor(), name="fure-execution-monitor"
        )

    async def stop(self) -> None:
        self._closing = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
        tasks = list(self._tasks.values())
        try:
            now = datetime.now(UTC)
            async with self.database.sessions.begin() as session:
                await RunRepository(session).finish_active(
                    RunRow.worker_id == self.worker_id,
                    status=RunStatus.CANCELLED.value,
                    meta=CancelRunMeta(),
                    finished_at=now,
                    updated_at=now,
                )
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=5)
                if pending:
                    logger.error(
                        "%s inline executions did not cooperate with cancellation",
                        len(pending),
                    )

    async def submit(
        self,
        function: UUID | str,
        input: dict[str, JSONValue] | None,
        *,
        principal: Principal | None = None,
    ) -> Run:
        run, _ = await self._start_run(function, input, principal)
        return run

    async def _start_run(
        self,
        function: UUID | str,
        input: dict[str, JSONValue] | None,
        principal: Principal | None,
        *,
        invocation: ToolInvocation | None = None,
    ) -> tuple[Run, asyncio.Task[Run]]:
        if self._closing:
            raise Unavailable("Application is shutting down")
        async with self.database.sessions.begin() as session:
            runs = RunRepository(session)
            if invocation is not None and not await runs.transition(
                invocation.parent_run_id,
                [RunStatus.RUNNING.value],
                status=RunStatus.RUNNING.value,
            ):
                raise ExecutionCancelled(
                    "Parent execution was cancelled", code="cancelled"
                )
            repository = FunctionRepository(session)
            function = (
                await repository.get(function, lock=True)
                if isinstance(function, UUID)
                else await repository.by_name(function, lock=True)
            )
            if function is None or function.deleted:
                raise NotFound("Function not found or deleted")
            require_function_access(principal, function.id)
            revision = (
                invocation.revision if invocation is not None else function.revision
            )
            agent = revision is not None and self._uses_tools(revision.runner_options)
            requires_permission = (
                not function.public
                or agent
                or invocation is not None
                or (principal is not None and principal.key_id is not None)
            )
            if requires_permission:
                if principal is None:
                    raise Unauthorized("Authentication required")
                if not principal.allows(Permission.FUNCTIONS_RUN):
                    raise Forbidden("Permission required: functions.run")
            if not function.runnable or revision is None:
                raise Conflict("Function is disabled or has no current revision")
            run = Run(
                revision,
                input,
                user_id=principal.user.id if principal else None,
                api_key_id=principal.key_id if principal else None,
                parent_run_id=invocation.parent_run_id if invocation else None,
                tool_call_index=invocation.index if invocation else None,
            )
            await runs.add(run, self.worker_id)
            if invocation is not None:
                await runs.record_tool_call(
                    invocation.parent_run_id,
                    RunToolCall(
                        index=invocation.index,
                        function_id=function.id,
                        name=revision.function.name,
                        arguments=run.input or {},
                        run_id=run.id,
                        step=invocation.step,
                    ),
                )
        if self._closing:
            await self._cancel_record(run.id)
            raise Unavailable("Application is shutting down", run_id=run.id)
        ancestors = invocation.ancestors if invocation else frozenset()
        task = asyncio.create_task(
            self._perform(run, principal, ancestors), name=f"fure-run-{run.id}"
        )
        self._tasks[run.id] = task
        task.add_done_callback(lambda finished: self._forget(run.id, finished))
        return run, task

    async def execute(
        self,
        function: UUID | str,
        input: dict[str, JSONValue] | None,
        *,
        principal: Principal | None = None,
    ) -> Run:
        run, task = await self._start_run(function, input, principal)
        return await self._wait_for_run(run, task)

    async def _wait_for_run(self, run: Run, task: asyncio.Task[Run]) -> Run:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                task.cancel()
                await asyncio.shield(self._cancel_record(run.id))
                raise
            async with self.database.sessions() as session:
                final = await RunRepository(session).get(run.id)
            if final is not None and final.status is RunStatus.SUCCEEDED:
                return final
            if final is not None and final.status is RunStatus.FAILED:
                raise ExecutionFailed(
                    "Execution worker stopped responding",
                    code="worker_lost",
                    run_id=run.id,
                ) from None
            raise ExecutionCancelled(
                "Execution was cancelled", code="cancelled", run_id=run.id
            ) from None

    @staticmethod
    def _uses_tools(options) -> bool:
        return isinstance(options, OpenAIJinjaRunnerOptions) and bool(
            options.tool_function_ids
        )

    async def _agent_tools(
        self,
        run: Run,
        options: OpenAIJinjaRunnerOptions,
        principal: Principal,
        ancestors: frozenset[UUID],
    ) -> tuple[OpenAIJinjaTool, ...]:
        ancestors = ancestors | {run.revision.function.id}
        ids = tuple(
            id for id in options.tool_function_ids if principal.allows_function(id)
        )
        if not ids:
            return ()
        async with self.database.sessions() as session:
            functions = await FunctionRepository(session).get_many(ids)
        return tuple(
            OpenAIJinjaTool(
                name=function.name,
                description=function.description,
                input_schema=function.input_schema,
                execute=partial(
                    self._execute_tool, function.revision, run.id, principal, ancestors
                ),
            )
            for function in functions
            if function.runnable and function.id not in ancestors
        )

    async def _execute_tool(
        self,
        revision: Revision,
        parent_run_id: UUID,
        principal: Principal,
        ancestors: frozenset[UUID],
        input: dict[str, JSONValue] | None,
        index: int,
        step: int,
    ) -> JSONValue:
        try:
            # Resolve a submission already in progress before cancelling its run.
            submission = asyncio.create_task(
                self._start_run(
                    revision.function.id,
                    input,
                    principal,
                    invocation=ToolInvocation(
                        revision, parent_run_id, index, ancestors, step
                    ),
                )
            )
            try:
                run, task = await asyncio.shield(submission)
            except asyncio.CancelledError:
                try:
                    run, task = await submission
                except ServiceError:
                    pass
                else:
                    task.cancel()
                    await asyncio.shield(self._cancel_record(run.id))
                raise
            return (await self._wait_for_run(run, task)).output
        except ServiceError as error:
            code = error.code if error.code != "internal_error" else "tool_unavailable"
            return {"error": {"code": code}}

    def _forget(self, id: UUID, task: asyncio.Task[Run]) -> None:
        self._tasks.pop(id, None)
        if not task.cancelled():
            task.exception()

    async def cancel(self, id: UUID, *, principal: Principal) -> Run:
        async with self.database.sessions() as session:
            row = await session.get(RunRow, id)
            if row is None:
                raise NotFound("Run not found")
            revision = await RevisionRepository(session).get(row.revision_id)
            require_function_access(principal, revision.function.id)
            if row.user_id == principal.user.id:
                if not principal.allows(Permission.RUNS_CANCEL):
                    raise Forbidden("Permission required: runs.cancel")
            elif not (
                principal.allows(Permission.RUNS_CANCEL_ALL)
                and principal.allows(Permission.RUNS_READ_ALL)
            ):
                raise NotFound("Run not found")
        await self._cancel_record(id, require_active=True)
        task = self._tasks.get(id)
        if task is not None:
            task.cancel()
        async with self.database.sessions() as session:
            run = await RunRepository(session).get(id)
            if run is None:
                raise NotFound("Run not found")
            return run

    async def _cancel_record(self, id: UUID, *, require_active: bool = False) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions.begin() as session:
            repository = RunRepository(session)
            values = {
                "status": RunStatus.CANCELLED.value,
                "meta": CancelRunMeta(),
                "finished_at": now,
                "updated_at": now,
            }
            changed = await repository.finish_active(
                RunRow.id == id,
                **values,
            )
            if changed:
                await repository.cancel_descendants(id, **values)
            if require_active and not changed:
                if (
                    await session.scalar(select(RunRow.id).where(RunRow.id == id))
                    is None
                ):
                    raise NotFound("Run not found")
                raise Conflict("Run has already finished")

    async def _record_assistant_comment(
        self, run_id: UUID, comment: RunAssistantComment
    ) -> None:
        async with self.database.sessions.begin() as session:
            if not await RunRepository(session).record_assistant_comment(
                run_id, comment
            ):
                raise asyncio.CancelledError

    async def _perform(
        self, run: Run, principal: Principal | None, ancestors: frozenset[UUID]
    ) -> Run:
        start = monotonic()
        has_secrets = False
        try:
            run.start()
            async with self.database.sessions.begin() as session:
                changed = await RunRepository(session).transition(
                    run.id,
                    [RunStatus.PENDING.value],
                    status=RunStatus.RUNNING.value,
                    started_at=run.started_at,
                    updated_at=run.updated_at,
                    heartbeat_at=datetime.now(UTC),
                )
            if not changed:
                raise asyncio.CancelledError
            options = run.revision.runner_options
            async with asyncio.timeout(options.timeout):
                implementation = run.revision.implementation
                variables = None
                if implementation.accepts_vars:
                    variables, has_secrets = await self.variables.execution_values()
                if isinstance(options, OpenAIJinjaRunnerOptions):
                    tools = None
                    if options.tool_function_ids:
                        assert (
                            principal is not None
                        )  # Verified when the run was submitted.
                        tools = await self._agent_tools(
                            run, options, principal, ancestors
                        )
                    result = await OpenAIJinjaRunner(options).run(
                        implementation,
                        run.input,
                        variables=variables,
                        tools=tools,
                        report_comment=partial(self._record_assistant_comment, run.id),
                    )
                else:
                    result = await InlinePythonRunner(options).run(
                        implementation, run.input, variables=variables
                    )
            json.dumps(result.output, allow_nan=False)
            async with self.database.sessions.begin() as session:
                runs = RunRepository(session)
                meta = result.meta
                if self._uses_tools(options):
                    meta = meta.with_progress(await runs.get_meta(run.id))
                run.succeed(result.output, meta, result.duration)
                changed = await runs.transition(
                    run.id,
                    [RunStatus.RUNNING.value],
                    status=run.status.value,
                    output=run.output,
                    meta=meta_to_data(run.meta),
                    duration=run.duration,
                    finished_at=run.finished_at,
                    updated_at=run.updated_at,
                )
            if not changed:
                raise asyncio.CancelledError
            return run
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_record(run.id))
            raise
        except (Exception, SystemExit, KeyboardInterrupt) as error:
            if isinstance(error, (InputValidationError, UndefinedError)):
                message = (
                    "Function input or variables are invalid"
                    if has_secrets
                    else str(error)
                )
                failure = InvalidInput(message, code="invalid_input", run_id=run.id)
            elif isinstance(error, (TimeoutError, APITimeoutError)):
                failure = ExecutionTimedOut(
                    "Execution timed out", code="timeout", run_id=run.id
                )
            elif isinstance(error, APIError):
                failure = ProviderFailed(
                    "LLM provider request failed", code="provider_error", run_id=run.id
                )
            elif isinstance(error, AgentLimitExceeded):
                failure = ExecutionFailed(
                    str(error), code="agent_limit_exceeded", run_id=run.id
                )
            elif isinstance(error, Unavailable):
                failure = Unavailable(
                    "Shared variables are unavailable",
                    code="variables_unavailable",
                    run_id=run.id,
                )
            else:
                failure = ExecutionFailed(
                    "Function execution failed", code="execution_error", run_id=run.id
                )
            now = datetime.now(UTC)
            async with self.database.sessions.begin() as session:
                changed = await RunRepository(session).finish_active(
                    RunRow.id == run.id,
                    status=RunStatus.FAILED.value,
                    meta=ErrorRunMeta(failure.code, str(failure)),
                    duration=max(0, monotonic() - start),
                    finished_at=now,
                    updated_at=now,
                )
            if not changed:
                raise ExecutionCancelled(
                    "Execution was cancelled", code="cancelled", run_id=run.id
                ) from None
            raise failure from error

    async def _reap_stale(self) -> None:
        now = datetime.now(UTC)
        async with self.database.sessions.begin() as session:
            await RunRepository(session).finish_active(
                or_(
                    RunRow.heartbeat_at < now - timedelta(seconds=self.lease_seconds),
                    and_(
                        RunRow.heartbeat_at.is_(None),
                        RunRow.created_at < now - timedelta(seconds=self.lease_seconds),
                    ),
                ),
                status=RunStatus.FAILED.value,
                meta=ErrorRunMeta("worker_lost", "Execution worker stopped responding"),
                finished_at=now,
                updated_at=now,
            )

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                ids = list(self._tasks)
                if ids:
                    async with self.database.sessions.begin() as session:
                        await session.execute(
                            update(RunRow)
                            .where(
                                RunRow.worker_id == self.worker_id,
                                RunRow.id.in_(ids),
                                RunRow.status.in_(ACTIVE_STATUSES),
                            )
                            .values(heartbeat_at=datetime.now(UTC))
                        )
                        stopped = (
                            await session.scalars(
                                select(RunRow.id).where(
                                    RunRow.id.in_(ids),
                                    or_(
                                        RunRow.status == RunStatus.CANCELLED.value,
                                        and_(
                                            RunRow.status == RunStatus.FAILED.value,
                                            RunRow.meta["code"].as_string()
                                            == "worker_lost",
                                        ),
                                    ),
                                )
                            )
                        ).all()
                    for id in stopped:
                        task = self._tasks.get(id)
                        if task is not None:
                            task.cancel()
                await self._reap_stale()
            except Exception:
                logger.exception("Execution heartbeat failed; will retry")
