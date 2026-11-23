# MCP

Fure serves a FastMCP server in the same FastAPI process, using Streamable HTTP
at `/mcp/`. With Docker Compose, connect to `http://localhost:8000/mcp/`.
It uses the same database, application lifespan and execution service as REST.
There is no extra container or command to start.

## Configure access

1. Give the user a role with `mcp.access`, `functions.read`, `functions.run`
   and `keys.manage`. The protected administrator role includes these permissions.
   Existing editor/runner roles are not automatically granted MCP access.
2. In **Мой аккаунт → Создать ключ**, select these integration permissions:
   `mcp.access`, `functions.read`, `functions.run`.
3. Select the functions this key may access. An empty list grants no functions.
   There is no wildcard or "all functions" mode.
4. Copy the secret, shown only once. Configure your MCP client to send
   `Authorization: Bearer fure_...` to `/mcp/`.

For clients supporting remote MCP URLs and custom headers:

```json
{
  "mcpServers": {
    "fure": {
      "url": "http://localhost:8000/mcp/",
      "headers": {"Authorization": "Bearer <your API key>"}
    }
  }
}
```

Use the backend address, not the workspace's `/api` proxy. HTTPS termination and
rate/concurrency limits belong to the external reverse proxy. Authentication uses
existing API keys; clients must support configuring a bearer token directly.
OAuth login/discovery and browser-cookie authentication are not provided for MCP.

## Discovery and execution

`tools/list` exposes one tool per selected, enabled, undeleted function that has
a current revision. Tool names match function names; descriptions and input schemas
come from the current function/revision. No source code or runner credentials are
included. `functions.read` is required for discovery; a key with only
`mcp.access` and `functions.run` can call known selected tools but sees an empty list.

Call a tool with its input fields directly, for example:

```json
{"name": "greet", "arguments": {"name": "world"}}
```

A Python function taking `input: None` exposes an empty object schema and is called
with `arguments: {}`. MCP tool arguments are objects. A successful result is returned
as both text and `structuredContent`:

```json
{
  "output": "Hello world",
  "meta": {},
  "duration": 0.223,
  "run_id": "01900000-0000-7000-8000-000000000001"
}
```

Each call executes the current revision and creates a normal history entry owned by
the API-key owner, with the key ID recorded. `runs.read` is not needed to execute;
it is needed to read history through REST. Function input validation and runner
timeouts apply normally. Execution errors use MCP `isError: true`; a persisted
failed/cancelled/timed-out execution includes its `run_id` in structured content.

## Change the selection

Use the **Функции (N)** button next to a key in the account page, or call:

```http
PATCH /v1/keys/{key_id}
Content-Type: application/json

{"function_ids": ["01900000-0000-7000-8000-000000000001"]}
```

This replaces the selection without changing the secret. Only the owner can edit
their active keys, with `keys.manage`. A restricted key cannot grant another key
functions outside its own selection. Function IDs must exist; selections survive
renames and soft deletion/restoration. Up to 1000 distinct function IDs are accepted.

Every HTTP request reloads credentials and effective owner permissions. Scope edits,
revocations and permission reductions affect subsequent requests. `tools/list` reads
the current catalogue without a global tool cache. Clients that cache discovery must
refresh their tool list to see changes; stale tool calls are still checked server-side.
The HTTP transport is stateless and does not send list-change notifications.
Changes do not cancel runs that have already started.

The selection applies equally to REST: function/revision reads and mutations,
execution by UUID or name, and run-history reads/cancellation. Out-of-scope direct
requests return 404; lists and pagination totals contain only accessible records.
Public functions do not bypass restrictions or `functions.run` for a supplied API key.
Revisions with [agent tools](api.md#agent-runs) require authentication, including
on the public REST alias. Selecting an agent in the key grants the ability to
run it with the tools configured in its revision. Direct discovery and tool calls
through MCP still use the key's explicit selection.

## Upgrade

Migration `0003` adds an empty function selection to existing API keys. These keys
retain their secrets and explicit permissions, but must be assigned functions before
their REST integrations can access functions or associated history again. No existing
key automatically receives `mcp.access`. Browser sessions remain governed by roles.
Startup applies this migration when automatic migrations are enabled.
