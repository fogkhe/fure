# Users, roles and API keys

Run `uv run python -m fure.cli create-admin admin` against the configured database
to create the first administrator. In Docker use
`docker compose exec app python -m fure.cli create-admin admin`. The password is
prompted without echo and must have 12–256 characters. `--password-stdin` reads
one line for automation without putting the password in command-line arguments.
This command refuses to create another account once any users exist.

There is no open registration. Administrators create accounts through the UI or API.
Usernames use lowercase ASCII letters, numbers, dots, underscores and hyphens,
start with a letter or number, and have at most 64 characters.

## Permissions

Users share one catalogue. Users can have multiple roles; their permissions are
the union of the assigned roles. Roles contain values from the fixed catalogue
below. Individual user overrides and per-function ACLs are not implemented.
Python and Jinja use the same permissions.

| Permission | Access |
| --- | --- |
| `mcp.access` | Connect to MCP using an API key; listing/running tools also needs the corresponding function permissions |
| `functions.read` | Function metadata, schemas and revision source/settings |
| `functions.create` | Create functions |
| `functions.update` | Edit functions, create revisions and choose the current revision |
| `functions.delete` | Soft-delete and restore functions |
| `functions.publish` | Change public execution; also requires create/update for the operation |
| `functions.run` | Execute private functions and revisions with agent tools |
| `runs.read` | Read the user's own runs |
| `runs.read_all` | Extend `runs.read` to every run, including anonymous/legacy runs |
| `runs.cancel` | Cancel the user's own active runs (requires `runs.read`) |
| `runs.cancel_all` | Cancel others' active runs, including anonymous runs (requires `runs.read` and `runs.read_all` for the returned execution details) |
| `users.manage` | Create/update users, assign roles, reset passwords and block access |
| `roles.manage` | Create/update/delete roles |
| `variables.read` | List shared variables, with secret values omitted |
| `variables.manage` | List, create, replace and delete shared variables |
| `keys.manage` | Create/list/revoke the current user's API keys and change their function selections |

The `admin` role includes every permission and cannot be edited or deleted.
At least one enabled account assigned that role must remain. The initial
`editor` and `runner` roles can be edited; editor includes function creation,
editing, deletion, reading and execution, and runner includes reading/execution.
Both include own-history reading/cancellation and key management. Neither can
publish functions or manage users/roles. Assigned roles cannot be deleted.

`users.manage` and `roles.manage` are administrative powers: they can grant other
permissions by assigning or modifying roles. Implementation editing is intended
for trusted authors; Python code executes in the application process.

## Browser sessions

`POST /v1/auth/login` takes `{"username":"admin","password":"..."}` and requires
`X-Fure-Client: web`. A successful response sets an HttpOnly, SameSite=Lax session
cookie and returns `{user, permissions, csrf_token}`. Passwords are hashed with
Argon2id; session secrets and API-key secrets are stored only as SHA-256 hashes.

Use `GET /v1/auth/me` to restore the session and obtain current permissions and a
CSRF token. Every authenticated cookie-based POST/PATCH/DELETE request must send
`X-CSRF-Token` with that token. `POST /v1/auth/logout` revokes the current session.
The Nuxt interface handles cookies and headers automatically through its API proxy.

Sessions last `SESSION_LIFETIME_SECONDS` (default 604800, seven days). Set
`SESSION_COOKIE_SECURE=true` when clients connect over HTTPS, including through a
reverse proxy; it defaults to false for local HTTP development. Changing or
resetting a password revokes all sessions. Blocking a user additionally revokes
all API keys; enabling them does not revive previous credentials.

`POST /v1/auth/password` takes `current_password` and `new_password` and requires
a browser session. It logs out all sessions, including the requesting browser.

## API keys

Create a key with `POST /v1/keys`:

```json
{"name":"integration","permissions":["functions.run"],"function_ids":["01900000-0000-7000-8000-000000000001"],"expires_at":null}
```

The response contains `secret` once. `GET /v1/keys` lists only the current user's
keys and never returns secrets or hashes. `DELETE /v1/keys/{key_id}` revokes a key
without removing its history. Expiration is optional and must be in the future.

Send `Authorization: Bearer fure_...` to authenticate an integration. CSRF is not
required for bearer credentials. Every request reloads the user and roles; the
effective permissions are the intersection of the key's explicit permissions
and the owner's current permissions. Permission reductions apply immediately;
new owner permissions do not expand existing keys. A key cannot mint a child key
with permissions beyond its own effective permissions. Keys can manage only the
owner's keys. Invalid, expired or revoked credentials return 401, including when
calling a public function. Valid credentials with insufficient permissions return 403.

Keys additionally have an explicit `function_ids` selection, empty by default.
There is no wildcard mode. The selection restricts REST and MCP access to functions,
their revisions and their history. Out-of-scope records return 404 and are excluded
from lists and totals. A public function does not bypass a key's scope or run permission.
A selected [agent](api.md#agent-runs) can invoke the tools configured in its active
revision during a run. Direct tool access still requires selection in the key.
`PATCH /v1/keys/{key_id}` with `{"function_ids": [...]}` replaces an active key's
selection without changing its secret; changes apply to subsequent requests.
Child-key creation and scope edits cannot grant functions beyond the requesting key's
own selection. See [MCP setup and upgrade notes](mcp.md), including assignment of
functions to pre-existing keys after migration.

## Public functions and history

Functions default to `public=false`. An authorized publisher can set `public=true`
using function creation/update. This permits anonymous `POST /v1/run/{name}` calls
to the enabled function's current revision. `POST /v1/functions/{id}/run` always
requires authentication. Source, settings and execution history
remain protected. Public functions do not appear in an anonymous catalogue.
Rate/concurrency limiting is delegated to the external reverse proxy.
Both execution routes default to waiting for the result. `wait=false` requires
authentication and `runs.read` and returns the persisted run immediately so it
can be polled or cancelled through the existing history endpoints.

Runs record `user_id` and `api_key_id`. Browser and API-key calls belong to the
same user; anonymous calls have no owner. Unrelated runs return 404 without
revealing their contents. Old runs retain null ownership and are visible with
`runs.read` plus `runs.read_all`; migration preserves all existing function and
revision data and makes existing functions private.

## Management routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET/POST | `/v1/users` | List/create accounts (`users.manage`) |
| PATCH | `/v1/users/{id}` | Username, password, enabled flag, role_ids |
| GET | `/v1/roles` | List roles (`users.manage` or `roles.manage`) |
| POST | `/v1/roles` | Create a role (`roles.manage`) |
| PATCH/DELETE | `/v1/roles/{id}` | Edit/delete a role (`roles.manage`) |
| GET | `/v1/roles/permissions` | Fixed permission catalogue (authenticated) |

List routes use the usual page/size pagination; role lists are sorted by name.
Users and roles use immutable IDs. PATCH cannot be empty or contain null fields.

## Manual LLM JSON extraction

OpenAI/Jinja runner options include:

```json
{"type":"openai_jinja","base_url":"http://localhost:11434/v1","model":"local-model","structured_outputs":false,"reasoning_end_tags":["</think>"]}
```

`structured_outputs` defaults to true. With a nonempty output schema, true sends
the unchanged schema as strict Structured Outputs; false omits `response_format`
and includes the schema in a system instruction. A single JSON value is extracted
from the response or Markdown block. Multiple candidates, broken JSON, duplicate
object keys, non-finite numbers, refusal and truncated generation fail the run.
Extracted output is validated against the original schema in both modes.
External schema references are not downloaded.

`reasoning_end_tags` defaults to an empty list. In manual mode, configured closing
markers remove preceding reasoning text (case insensitive); an empty list disables
that stripping. Complete JSON is parsed as-is, including literal marker strings.
With an empty output schema, the original text is returned in either mode.
No automatic mode fallback, JSON repair or extra LLM request is performed.
