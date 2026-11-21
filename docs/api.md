# REST API v1

The API contract is available in Swagger UI at `/docs`, ReDoc at `/redoc`,
and OpenAPI at `/openapi.json`.
The workspace footer opens Swagger UI at `/api/docs` through the Nuxt proxy.
Swagger uses `/api` as its base URL there.

Routes are backed by SQLAlchemy repositories and application services. `/health`
checks database connectivity and returns `200` with `{"status": "ok"}`. An unavailable
database returns `503`.

All catalogue/history operations require authentication and permissions.
Use a browser session or `Authorization: Bearer <API key>`. Only functions marked
`public` with a current revision that has no tools can run anonymously. See [users, roles and API keys](auth.md) for setup,
management endpoints, scopes and CSRF headers. Read examples below assume an
administrator session or a key with the corresponding permissions.

API keys also require an explicit function selection, shared by REST and
[MCP](mcp.md). Out-of-scope functions, revisions and runs are excluded from lists
and return 404 when addressed directly. Manage the selection with
`PATCH /v1/keys/{key_id}` and a `function_ids` array.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/functions` | List functions; filter by `name`, `enabled`, `deleted` |
| POST | `/v1/functions` | Create a function without an implementation |
| GET | `/v1/functions/{function_id}` | Get a function and its current schemas |
| PATCH | `/v1/functions/{function_id}` | Change name, description, enabled/public flags or current revision |
| DELETE | `/v1/functions/{function_id}` | Soft-delete a function, retaining history |
| POST | `/v1/functions/{function_id}/undelete` | Clear the deletion marker |
| POST | `/v1/functions/{function_id}/run` | Execute the current revision by UUID with authentication |
| GET | `/v1/revisions` | List revisions; filter by `function_id` |
| POST | `/v1/revisions` | Create an immutable implementation version |
| GET | `/v1/variables` | List shared variables and visible values |
| POST | `/v1/variables` | Create a shared variable |
| PATCH | `/v1/variables/{name}` | Replace a variable's value |
| DELETE | `/v1/variables/{name}` | Delete a shared variable |
| GET | `/v1/revisions/{revision_id}` | Get implementation source, schemas and public runner settings |
| GET | `/v1/runs` | List execution history; filter by `function_id`, `revision_id`, `status`, `parent_run_id` |
| GET | `/v1/runs/{run_id}` | Get execution input, output, metadata, status and timestamps |
| POST | `/v1/runs/{run_id}/cancel` | Cancel a pending or running execution |
| POST | `/v1/run/{function_name}` | Name-based alias of `/v1/functions/{function_id}/run` |

The canonical execution route uses the immutable function UUID and always requires
authentication. The name-based alias resolves the current function name and allows
anonymous synchronous execution of public functions whose current revision has no tools.
List responses have `items`, `total`, `page` and `size`. Pages are zero-based;
`page=0` and `size=100` are the defaults, with a maximum size of 1000.
Lists are ordered newest first. Function lists exclude deleted functions by
default; `deleted=true` selects deleted functions.
The function list's `name` filter matches a literal substring regardless of case.
For example, `name=reet` finds `greet` and `GREETING`.

## Agent runs

In a Jinja OpenAI revision, add `tool_function_ids` to `runner_options` to give
the model a selection of Fure functions. In the workspace, choose them in the
revision editor under **Tools**. The model and provider must support native Chat
Completions tool calling. The runner sends each available function's name,
description and input schema and validates the arguments locally before execution.
The active revisions are resolved when each agent starts and reused for the
duration of that agent run. Nested agents work as tools. A branch cannot invoke
a function already on that branch.

Launching a version with tools requires authentication and `functions.run`.
An API key can only expose tools within its function selection. Disabled and
deleted functions drop out of tool discovery, and their status is checked again
at execution. A tool's result or an error code goes back to the model.

Calls in the same model response execute in parallel. `max_steps` defaults to
8 and counts model requests including the final response. `max_tool_calls`
defaults to 16 and counts individual requests, including calls in the same
batch and invalid calls. Both settings apply per agent run and accept `null`
for no limit. A batch that would exceed a limit fails before execution with
`agent_limit_exceeded`. The runner timeout covers the whole agent run and
cancellation propagates to active descendants.

The REST result is the final text or JSON. Token counts cover all model
requests made directly by that agent. Each executed tool has a run record with
its own revision, status, output and metadata. Query
`GET /v1/runs?parent_run_id={run_id}` for direct tool runs in `tool_call_index`
order. History uses the existing owner, permission and API-key scope checks.

Each accepted tool call is also saved immediately in the parent run’s
`meta.tool_calls`, with `index`, `name`, `arguments`, `function_id` and `run_id`.
The current list is available from `GET /v1/runs/{id}` while the agent is
running and remains in history after it finishes. Successful synchronous
responses include the list in their metadata. API keys can see only entries
for functions in their selection.

When an assistant response contains both text and tool calls, the text is
published in `meta.assistant_comments` as `{ "text": "…", "step": 1,
"received_at": "2026-11-21T06:27:50Z" }` and the agent continues with the calls.
Tool calls from the same model response share the same `step`. The step
number is one-based within the parent run. `received_at` is the UTC time when
Fure received the response. Historical comments can have null timing fields.
The final answer remains in `output`. Provider reasoning fields are excluded
from the comments.

## Shared variables

The Variables tab uses `/v1/variables`. A create request contains `name`,
`value` and `secret` (default `false`). Names are case-sensitive and use up to
64 Latin letters, digits or underscores, starting with a letter or underscore.
Ordinary values are JSON. Secret values are non-empty strings and the API omits
their `value` from every response. `PATCH` takes `{"value": ...}`. A variable's
name and secret flag are set at creation. Read access requires `variables.read`
or `variables.manage`. Writes require `variables.manage`.

To enable secrets, generate a key with the command below and set
`VARIABLES_ENCRYPTION_KEY` on the backend. For Docker Compose, set it in the root
`.env` file. Keep the same key across restarts and workers, with a backup separate
from the database. Ordinary variables work without a key.

```sh
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

New Jinja versions receive the shared dictionary as `vars`, for example
`{{ vars.defaultLanguage }}`. Older versions that declared `vars` as a user
input keep that contract. Python functions can opt in by declaring `vars`.
Its annotation does not determine any schema, so built-in `dict[str, object]`
works without a Fure import.

```python
async def function(input: None, vars: dict[str, object]) -> str:
    return str(vars["defaultLanguage"])
```

Each run gets the current values, including secrets, when it starts. `vars` is
separate from user input and is excluded from schemas, MCP tool definitions and
run records. Implementations are trusted to handle their values. Output is
recorded as returned, and Jinja rendering can include values in provider prompts.

## Create, version and run a function

1. `POST /v1/functions`:

   ```json
   {"name": "greet", "description": "A greeting assistant"}
   ```

2. `POST /v1/revisions`, using the function ID from the first response:

   ```json
   {
     "function_id": "01900000-0000-7000-8000-000000000001",
     "implementation": {
       "type": "jinja",
       "template": "Hello, {{ name }}!",
       "output_schema": {}
     },
     "runner_options": {
       "type": "openai_jinja",
       "base_url": "http://localhost:11434/v1",
       "model": "your-model",
       "api_key": null,
       "timeout": 120
     }
   }
   ```

   A Python revision uses `{"type": "python", "code": "..."}` and
   `{"type": "inline_python", "timeout": 10}`. Their schemas are derived from
   annotations; clients cannot override them. Jinja derives its input schema from
   the template and accepts an output schema. Empty output schemas use text output;
   nonempty schemas use Structured Outputs by default. Set `structured_outputs: false`
   in runner options to extract and validate JSON manually; `reasoning_end_tags: []`
   configures optional reasoning removal. Provider API keys are accepted only in
   creation requests and omitted from revision responses.

3. `PATCH /v1/functions/{function_id}` to select the returned revision:

   ```json
   {"revision_id": "01900000-0000-7000-8000-000000000002"}
   ```

   The revision must belong to that function. Creating a revision does not
   automatically select it. Revisions cannot be updated; create a new one to change
   source or runner settings. PATCH updates only supplied fields; `description: null`
   clears the description. Other editable fields cannot be null.

4. `POST /v1/functions/{function_id}/run` (or its alias `POST /v1/run/greet`):

   ```json
   {"input": {"name": "world"}}
   ```

   Successful execution returns:

   ```json
   {
     "output": "Hello! How can I help you today?",
     "meta": {"input_tokens": 10, "output_tokens": 261, "total_tokens": 271},
     "duration": 0.223
   }
   ```

   `output` can be any JSON value. Inline Python metadata is `{}`. OpenAI metadata
   contains nullable token counts; null means usage was not supplied by the provider.
   Duration is measured in seconds. For no-input functions, send `{}` or `{"input": null}`.
   Full execution records are exposed through `/v1/runs`, including failed and
   cancelled runs. Each record identifies its function and immutable revision.
   The `X-Run-Id` response header identifies the stored execution on both success
   and execution errors; use it to fetch `/v1/runs/{run_id}`.

## Execution with an immediate run ID

Add `?wait=false` to either execution route to receive `202` and a full execution
record immediately, including `id`, `function_id`, `revision_id`, `status` and
caller information. `X-Run-Id` contains the same ID. The input body is unchanged.
This mode requires authentication and `runs.read`, in addition to the usual
function execution rules, because the result is read from execution history.

Poll `GET /v1/runs/{id}` while status is `pending` or `running`. Terminal states
are `succeeded`, `failed` and `cancelled`. Result, metadata and duration are stored
in the record; failures include `meta.code` and `meta.message` rather than a
subsequent synchronous HTTP error. Execution continues independently of the
submission request and uses the same heartbeat, timeout and shutdown handling.

Cancel through `POST /v1/runs/{id}/cancel`, with the same permissions and terminal
state protection as history cancellation. In the workspace, users with history
access use this mode, and the Run button becomes Cancel when they also have
`runs.cancel`. Users without history access retain synchronous execution.
The default remains `wait=true`, including for eligible anonymous public calls.

## Status codes

Resource creation returns `201`; soft deletion returns `204` with no body.
Reads, updates, undeletion, cancellation and successful execution return `200`.
Missing resources return `404`; name collisions and invalid resource state return
`409`. Invalid request fields return FastAPI's `422` validation response.
Missing/invalid credentials return `401`, insufficient permissions or invalid CSRF
tokens return `403`. Validation responses omit original input values to avoid
echoing passwords or keys.
Execution timeouts return `504`; LLM provider failures return `502`.
Other execution failures return `500`. Database unavailability and shutdown return
`503`. Errors use `{"detail": "..."}`; request validation errors have a list in
`detail`. Invalid function input returns `422` and also creates a failed run record.

## Execution and persistence

Runs are saved before execution, then atomically transition from pending to running
and to a terminal state. Execution does not hold a database transaction open.
Each run retains its revision even when the function is renamed, updated or deleted.
New revisions must be explicitly selected; soft deletion preserves all history.
Names are unique among active functions and can be reused after deletion. Undeletion
returns `409` when a replacement function already uses the name.

Cancellation persists the terminal state and cancels the owning task. Other workers
observe cancellation through the database. Once a terminal state has won the race,
another completion or cancellation cannot overwrite it. Cancelling a finished run
returns `409`; an interrupted synchronous execution request also returns `409`.
On graceful shutdown, the worker cancels its active runs and closes tasks and DB
connections. Runs whose worker heartbeat expires become failed with
`meta.code = "worker_lost"`.

Failed runs carry `meta.code` and `meta.message`. Codes include `invalid_input`,
`execution_error`, `provider_error`, `timeout` and `worker_lost`. Provider errors
expose a generic message; stored API keys are excluded from public responses.

Python definitions are analyzed when a revision is created; reads reconstruct
objects through `restore` and never execute stored code. Execution prepares its own
runtime from the immutable stored definition. Inline Python shares the server
process: cancellation and timeouts are cooperative and require code to yield to the
event loop. Definition initialization and function code must be trusted. Jinja/LLM
execution is asynchronous and shares the same execution deadline, including retries.

See [application lifecycle and database configuration](storage.md) for startup,
migrations and repository details.
