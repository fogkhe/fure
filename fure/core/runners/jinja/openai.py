import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from openai import AsyncOpenAI, Omit, omit
from openai.types.chat import (
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionToolParam,
)
from openai.types.shared_params.response_format_json_schema import (
    ResponseFormatJSONSchema,
)
from referencing import Registry

from fure.core.implementations import JinjaImplementation
from fure.core.run import RunAssistantComment, SucceedRunMeta
from fure.core.runners.base import RunResult
from fure.core.runners.jinja.base import BaseJinjaRunner, BaseJinjaRunnerOptions
from fure.core.types import JSONSchema, JSONValue
from fure.utils.json_extraction import extract_json, parse_json, strip_reasoning

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_TOOL_CALLS = 16


@dataclass(frozen=True, slots=True)
class OpenAIJinjaRunMeta(SucceedRunMeta):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True, slots=True)
class OpenAIJinjaRunnerOptions(BaseJinjaRunnerOptions):
    api_key: str | None
    base_url: str
    model: str
    structured_outputs: bool = True
    reasoning_end_tags: tuple[str, ...] = ()
    tool_function_ids: tuple[UUID, ...] = ()
    max_steps: int | None = DEFAULT_MAX_STEPS
    max_tool_calls: int | None = DEFAULT_MAX_TOOL_CALLS

    def __post_init__(self):
        object.__setattr__(self, "reasoning_end_tags", tuple(self.reasoning_end_tags))
        object.__setattr__(self, "tool_function_ids", tuple(self.tool_function_ids))
        if any(not tag.strip() for tag in self.reasoning_end_tags):
            raise ValueError("Reasoning end tags cannot be empty")
        if len(set(self.tool_function_ids)) != len(self.tool_function_ids):
            raise ValueError("Tool functions must be unique")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.max_tool_calls is not None and self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be non-negative")


@dataclass(frozen=True, slots=True)
class OpenAIJinjaTool:
    name: str
    description: str | None
    input_schema: JSONSchema
    execute: Callable[[dict[str, JSONValue] | None, int, int], Awaitable[JSONValue]]

    @property
    def parameters(self) -> JSONSchema:
        if self.input_schema.get("type") == "null":
            return {"type": "object", "properties": {}, "additionalProperties": False}
        return {**self.input_schema, "type": "object"}


class AgentLimitExceeded(RuntimeError):
    pass


class InvalidToolResponse(RuntimeError):
    pass


async def _call_tool(
    call: ChatCompletionMessageFunctionToolCall,
    tools: dict[str, OpenAIJinjaTool],
    index: int,
    step: int,
) -> dict[str, str]:
    tool = tools.get(call.function.name)
    if tool is None:
        output = {"error": {"code": "tool_unavailable"}}
    else:
        try:
            arguments = parse_json(call.function.arguments)
            if not isinstance(arguments, dict):
                raise TypeError("Tool arguments must be a JSON object")
            null_input = tool.input_schema.get("type") == "null"
            schema = tool.parameters if null_input else tool.input_schema
            validator_for(schema)(schema, registry=Registry()).validate(arguments)
        except RecursionError, TypeError, ValueError, ValidationError:
            output = {"error": {"code": "invalid_input"}}
        else:
            output = await tool.execute(None if null_input else arguments, index, step)
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": json.dumps(output, ensure_ascii=False, allow_nan=False),
    }


class OpenAIJinjaRunner(
    BaseJinjaRunner[
        OpenAIJinjaRunnerOptions,
        OpenAIJinjaRunMeta,
    ]
):
    async def run(
        self,
        implementation: JinjaImplementation,
        input: dict[str, JSONValue] | None,
        options: OpenAIJinjaRunnerOptions | None = None,
        *,
        variables: dict[str, JSONValue] | None = None,
        tools: tuple[OpenAIJinjaTool, ...] | None = None,
        report_comment: Callable[[RunAssistantComment], Awaitable[None]] | None = None,
    ) -> RunResult[OpenAIJinjaRunMeta]:
        options = options if options is not None else self._options
        if tools is None and options.tool_function_ids:
            raise ValueError("Tool functions require an execution context")

        output_schema = implementation.output_schema
        json_output = bool(output_schema)
        structured_output = json_output and options.structured_outputs
        schema_text = json.dumps(output_schema, ensure_ascii=False)
        validator = None
        if json_output:
            validator_type = validator_for(output_schema)
            validator_type.check_schema(output_schema)
            validator = validator_type(output_schema, registry=Registry())

        response_format: ResponseFormatJSONSchema | Omit = omit
        if structured_output:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "output",
                    "schema": dict(output_schema),
                    "strict": True,
                },
            }

        start_time = monotonic()

        prompt = implementation.render(input, variables=variables)

        available = {tool.name: tool for tool in tools or ()}
        format_after_tools = structured_output and bool(available)
        if format_after_tools and options.max_steps == 1:
            raise AgentLimitExceeded(
                "Structured output with tools requires at least two model steps"
            )
        instructions = []
        if available:
            instructions.append(
                "Tool calls in the same assistant response run in parallel. "
                "Only group independent calls. If a call needs another tool's "
                "result or must happen after that tool finishes, request the "
                "prerequisite first. Wait for its tool result before requesting "
                "the dependent call in a later response. For a strictly ordered "
                "sequence, request only the next call in each response. Complete "
                "all requested tool actions before writing your final answer."
            )
        if json_output:
            instruction = "The final output must conform to the schema."
            if format_after_tools:
                instruction += (
                    " After completing the tool actions, provide a complete answer "
                    "with the information needed by this JSON Schema.\n" + schema_text
                )
            elif not structured_output:
                instruction = (
                    "Return exactly one JSON value matching this JSON Schema in your final response. "
                    "Do not include explanations or Markdown in the final response.\n"
                    + schema_text
                )
            instructions.append(instruction)
        messages = []
        if instructions:
            messages.append({"role": "system", "content": "\n\n".join(instructions)})
        messages.append(
            {"role": "user", "content": prompt},
        )

        tool_definitions: list[ChatCompletionToolParam] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or tool.name,
                    "parameters": tool.parameters,
                },
            }
            for tool in available.values()
        ]
        call_count = 0
        seen_call_ids: set[str] = set()
        usages = []
        comments = []
        async with AsyncOpenAI(
            api_key=options.api_key if options.api_key is not None else "none",
            base_url=options.base_url,
            timeout=options.timeout,
        ) as client:

            async def complete(
                request_messages,
                *,
                request_format: ResponseFormatJSONSchema | Omit,
                request_tools: list[ChatCompletionToolParam] | Omit = omit,
            ):
                completion = await client.chat.completions.create(
                    model=options.model,
                    messages=request_messages,
                    response_format=request_format,
                    tools=request_tools,
                )
                usages.append(completion.usage)
                if not completion.choices:
                    raise RuntimeError("Chat completion returned no choices")
                return completion.choices[0]

            while True:
                choice = await complete(
                    messages,
                    request_format=omit if format_after_tools else response_format,
                    request_tools=tool_definitions or omit,
                )
                message = choice.message
                steps = len(usages)
                calls = message.tool_calls or []
                if not calls:
                    if available and choice.finish_reason == "tool_calls":
                        raise InvalidToolResponse(
                            "Chat completion indicated tool calls but returned none"
                        )
                    break
                if (
                    not available
                    or message.refusal
                    or choice.finish_reason not in {"tool_calls", "stop"}
                    or not all(
                        isinstance(call, ChatCompletionMessageFunctionToolCall)
                        for call in calls
                    )
                ):
                    raise InvalidToolResponse(
                        "Chat completion returned invalid tool calls"
                    )
                ids = {call.id for call in calls}
                if len(ids) != len(calls) or not all(ids) or ids & seen_call_ids:
                    raise InvalidToolResponse(
                        "Chat completion returned invalid tool call IDs"
                    )
                seen_call_ids.update(ids)
                # Reserve a reply after the batch and, when enabled, a formatting step.
                steps_needed = 2 if format_after_tools else 1
                if (
                    options.max_steps is not None
                    and steps + steps_needed > options.max_steps
                ):
                    raise AgentLimitExceeded(
                        "Agent reached the maximum number of steps"
                    )
                if (
                    options.max_tool_calls is not None
                    and call_count + len(calls) > options.max_tool_calls
                ):
                    raise AgentLimitExceeded(
                        "Agent reached the maximum number of tool calls"
                    )
                text = strip_reasoning(
                    message.content or "", options.reasoning_end_tags
                )
                if text:
                    comment = RunAssistantComment(
                        text=text, step=steps, received_at=datetime.now(UTC)
                    )
                    comments.append(comment)
                    if report_comment is not None:
                        await report_comment(comment)
                assistant = message.model_dump(
                    include={"role", "content", "tool_calls", "reasoning_content"},
                    exclude_none=True,
                )
                messages.append(assistant)
                async with asyncio.TaskGroup() as group:
                    tasks = [
                        group.create_task(_call_tool(call, available, index, steps))
                        for index, call in enumerate(calls, start=call_count + 1)
                    ]
                messages.extend(task.result() for task in tasks)
                call_count += len(calls)

            if format_after_tools:
                if message.refusal or choice.finish_reason != "stop":
                    raise RuntimeError(
                        "Chat completion did not finish the agent response"
                    )
                draft = strip_reasoning(
                    message.content or "", options.reasoning_end_tags
                )
                if not draft:
                    raise RuntimeError("Chat completion returned no text content")
                choice = await complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Convert the supplied answer to this JSON Schema. "
                                "Preserve its facts and values.\n" + schema_text
                            ),
                        },
                        {"role": "user", "content": draft},
                    ],
                    request_format=response_format,
                )
                message = choice.message
                if message.tool_calls:
                    raise InvalidToolResponse(
                        "Chat completion returned tool calls during output formatting"
                    )

        if tools is not None and (message.refusal or choice.finish_reason != "stop"):
            raise RuntimeError("Chat completion did not finish the agent response")
        if json_output:
            if choice.message.refusal:
                raise RuntimeError(
                    "Chat completion refused to produce structured output",
                )
            if choice.finish_reason != "stop":
                raise RuntimeError(
                    "Chat completion did not finish the structured output",
                )
        content = choice.message.content
        if content is None:
            raise RuntimeError(
                "Chat completion returned no text content",
            )

        output = content
        if validator is not None:
            try:
                output = (
                    parse_json(content)
                    if structured_output
                    else extract_json(content, options.reasoning_end_tags)
                )
            except ValueError as error:
                raise ValueError(
                    "Chat completion returned invalid JSON or multiple JSON values",
                ) from error
            validator.validate(output)

        def total(field):
            values = [getattr(usage, field) for usage in usages if usage is not None]
            return (
                sum(values)
                if len(values) == len(usages) and None not in values
                else None
            )

        return RunResult(
            output=output,
            meta=OpenAIJinjaRunMeta(
                input_tokens=total("prompt_tokens"),
                output_tokens=total("completion_tokens"),
                total_tokens=total("total_tokens"),
                assistant_comments=tuple(comments),
            ),
            duration=monotonic() - start_time,
        )
