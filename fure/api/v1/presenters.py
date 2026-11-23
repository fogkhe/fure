from dataclasses import replace

from fure.api.v1.schemas import (
    FunctionResponse,
    RevisionResponse,
    RunDetailResponse,
    RunResponse,
)
from fure.core.auth import Principal
from fure.core.run import BaseRunMeta, Run
from fure.core.runners.jinja.openai import OpenAIJinjaRunnerOptions
from fure.database.mappers import implementation_to_data, options_to_data


def uses_tools(revision):
    options = revision.runner_options if revision else None
    return isinstance(options, OpenAIJinjaRunnerOptions) and bool(
        options.tool_function_ids
    )


def function_response(function):
    return FunctionResponse(
        id=function.id,
        name=function.name,
        description=function.description,
        revision_id=function.revision.id if function.revision else None,
        enabled=function.enabled,
        public=function.public,
        deleted=function.deleted,
        runnable=function.runnable,
        uses_tools=uses_tools(function.revision),
        input_schema=function.input_schema,
        output_schema=function.output_schema,
        created_at=function.created_at,
        updated_at=function.updated_at,
        deleted_at=function.deleted_at,
    )


def revision_response(revision):
    options = options_to_data(revision.runner_options)
    options.pop("api_key", None)
    return RevisionResponse(
        id=revision.id,
        function_id=revision.function.id,
        implementation=implementation_to_data(revision.implementation),
        runner_options=options,
        created_at=revision.created_at,
    )


def _visible_run_meta(run: Run, principal: Principal | None) -> BaseRunMeta | None:
    meta = run.meta
    if (
        meta is not None
        and meta.tool_calls
        and principal is not None
        and principal.key_id is not None
    ):
        meta = replace(
            meta,
            tool_calls=tuple(
                call
                for call in meta.tool_calls
                if principal.allows_function(call.function_id)
            ),
        )
    return meta


def run_result_response(run: Run, principal: Principal | None) -> RunResponse:
    return RunResponse(
        output=run.output, meta=_visible_run_meta(run, principal), duration=run.duration
    )


def run_response(run: Run, principal: Principal | None = None) -> RunDetailResponse:
    return RunDetailResponse(
        id=run.id,
        function_id=run.revision.function.id,
        revision_id=run.revision.id,
        user_id=run.user_id,
        api_key_id=run.api_key_id,
        parent_run_id=run.parent_run_id,
        tool_call_index=run.tool_call_index,
        uses_tools=uses_tools(run.revision),
        input=run.input,
        status=run.status,
        output=run.output,
        meta=_visible_run_meta(run, principal),
        duration=run.duration,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
