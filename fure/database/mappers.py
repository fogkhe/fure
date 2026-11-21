from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from fure.core.function import Function
from fure.core.implementations import JinjaImplementation, PythonImplementation
from fure.core.implementations.base import BaseImplementation
from fure.core.revision import Revision
from fure.core.run import (
    BaseRunMeta,
    CancelRunMeta,
    ErrorRunMeta,
    FailRunMeta,
    Run,
    RunAssistantComment,
    RunningRunMeta,
    RunStatus,
    RunToolCall,
    SucceedRunMeta,
)
from fure.core.runners.base import BaseRunnerOptions
from fure.core.runners.jinja.openai import OpenAIJinjaRunMeta, OpenAIJinjaRunnerOptions
from fure.core.runners.python.base import BasePythonRunnerOptions
from fure.core.runners.python.inline import (
    InlinePythonRunMeta,
    InlinePythonRunnerOptions,
)
from fure.database.models import FunctionRow, RevisionRow, RunRow


def implementation_to_data(implementation: BaseImplementation) -> dict:
    schemas = {
        "input_schema": implementation.input_schema,
        "output_schema": implementation.output_schema,
    }
    if isinstance(implementation, PythonImplementation):
        return {"type": "python", "code": implementation.code, **schemas}
    if isinstance(implementation, JinjaImplementation):
        return {"type": "jinja", "template": implementation.template, **schemas}
    raise TypeError("Unsupported implementation type")


def implementation_from_data(data: dict) -> BaseImplementation:
    fields = {key: value for key, value in data.items() if key != "type"}
    if data["type"] == "python":
        return PythonImplementation.restore(**fields)
    if data["type"] == "jinja":
        return JinjaImplementation.restore(**fields)
    raise ValueError("Unknown stored implementation type")


def options_to_data(options: BaseRunnerOptions) -> dict:
    if isinstance(options, OpenAIJinjaRunnerOptions):
        data = asdict(options)
        data["tool_function_ids"] = [str(id) for id in options.tool_function_ids]
        return {"type": "openai_jinja", **data}
    if isinstance(options, BasePythonRunnerOptions):
        return {"type": "inline_python", "timeout": options.timeout}
    raise TypeError("Unsupported runner options type")


def options_from_data(data: dict) -> BaseRunnerOptions:
    fields = {key: value for key, value in data.items() if key != "type"}
    if data["type"] == "openai_jinja":
        fields["tool_function_ids"] = tuple(
            map(UUID, fields.get("tool_function_ids", ()))
        )
        return OpenAIJinjaRunnerOptions(**fields)
    if data["type"] == "inline_python":
        return InlinePythonRunnerOptions(**fields)
    raise ValueError("Unknown stored runner type")


META_TYPES = {
    "openai_jinja": OpenAIJinjaRunMeta,
    "inline_python": InlinePythonRunMeta,
    "error": ErrorRunMeta,
    "fail": FailRunMeta,
    "cancel": CancelRunMeta,
    "running": RunningRunMeta,
    "succeed": SucceedRunMeta,
}


def meta_to_data(meta: BaseRunMeta | None) -> dict | None:
    if meta is None:
        return None
    for kind, cls in META_TYPES.items():
        if type(meta) is cls:
            fields = asdict(meta)
            calls = fields.pop("tool_calls")
            fields.pop("assistant_comments")
            if meta.assistant_comments:
                fields["assistant_comments"] = [
                    {
                        "text": comment.text,
                        "step": comment.step,
                        "received_at": comment.received_at.isoformat()
                        if comment.received_at is not None
                        else None,
                    }
                    for comment in meta.assistant_comments
                ]
            if calls:
                fields["tool_calls"] = [
                    {
                        **call,
                        "function_id": str(call["function_id"]),
                        "run_id": str(call["run_id"]),
                    }
                    for call in calls
                ]
            return {"type": kind, **fields}
    raise TypeError("Unsupported run metadata type")


def meta_from_data(data: dict | None) -> BaseRunMeta | None:
    if data is None:
        return None
    cls = META_TYPES[data["type"]]
    fields = {
        key: value
        for key, value in data.items()
        if key not in {"type", "tool_calls", "assistant_comments"}
    }
    calls = tuple(
        RunToolCall(
            **{
                **call,
                "function_id": UUID(call["function_id"]),
                "run_id": UUID(call["run_id"]),
            }
        )
        for call in data.get("tool_calls", ())
    )
    comments = []
    for entry in data.get("assistant_comments", ()):
        if isinstance(entry, str):
            comments.append(
                RunAssistantComment(text=entry, step=None, received_at=None)
            )
        else:
            received_at = entry["received_at"]
            comments.append(
                RunAssistantComment(
                    text=entry["text"],
                    step=entry["step"],
                    received_at=datetime.fromisoformat(received_at)
                    if received_at is not None
                    else None,
                )
            )
    return cls(
        tool_calls=calls,
        assistant_comments=tuple(comments),
        **fields,
    )


def function_from_row(row: FunctionRow, *, include_revision=True) -> Function:
    function = Function.restore(
        id=row.id,
        name=row.name,
        description=row.description,
        revision=None,
        enabled=row.enabled,
        public=row.public,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )
    if include_revision and row.revision is not None:
        # A trusted mapper wires the cyclic object graph without invoking the setter.
        function._revision = revision_from_row(row.revision, function)
    return function


def revision_from_row(row: RevisionRow, function: Function | None = None) -> Revision:
    owner = (
        function
        if function is not None
        else function_from_row(row.function, include_revision=False)
    )
    return Revision.restore(
        id=row.id,
        function=owner,
        implementation=implementation_from_data(row.implementation),
        runner_options=options_from_data(row.runner_options),
        created_at=row.created_at,
    )


def run_from_row(row: RunRow) -> Run:
    return Run.restore(
        id=row.id,
        revision=revision_from_row(row.revision),
        user_id=row.user_id,
        api_key_id=row.api_key_id,
        parent_run_id=row.parent_run_id,
        tool_call_index=row.tool_call_index,
        input=row.input,
        status=RunStatus(row.status),
        output=row.output,
        meta=meta_from_data(row.meta),
        duration=row.duration,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
