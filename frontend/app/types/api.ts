export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }
export type JsonObject = { [key: string]: JsonValue }

export interface Page<T> {
  items: T[]
  total: number
  page: number
  size: number
}

export type VariableRecord = { name: string, updated_at: string } & (
  { secret: true } | { secret: false, value: JsonValue }
)

export interface FunctionRecord {
  id: string
  name: string
  description: string | null
  revision_id: string | null
  enabled: boolean
  public: boolean
  deleted: boolean
  runnable: boolean
  uses_tools: boolean
  input_schema: JsonObject
  output_schema: JsonObject
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export type Implementation
  = | { type: 'python', code: string, input_schema: JsonObject, output_schema: JsonObject }
    | { type: 'jinja', template: string, input_schema: JsonObject, output_schema: JsonObject }

export type RunnerOptions
  = | { type: 'inline_python', timeout: number | null }
    | { type: 'openai_jinja', timeout: number | null, base_url: string, model: string, structured_outputs: boolean, reasoning_end_tags: string[], tool_function_ids: string[], max_steps: number | null, max_tool_calls: number | null }

export interface RevisionRecord {
  id: string
  function_id: string
  implementation: Implementation
  runner_options: RunnerOptions
  created_at: string
}

export type RunStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export interface RunToolCall {
  index: number
  step?: number
  function_id: string
  name: string
  arguments: JsonObject
  run_id: string
}

export interface RunAssistantComment {
  text: string
  step: number | null
  received_at: string | null
}

export interface RunResult {
  output: JsonValue
  meta: { input_tokens?: number | null, output_tokens?: number | null, total_tokens?: number | null, tool_calls?: RunToolCall[], assistant_comments?: RunAssistantComment[] }
  duration: number
}

export interface RunRecord {
  id: string
  function_id: string
  revision_id: string
  user_id: string | null
  api_key_id: string | null
  parent_run_id: string | null
  tool_call_index: number | null
  uses_tools: boolean
  input: JsonObject | null
  status: RunStatus
  output: JsonValue
  meta: (RunResult['meta'] & { code?: string, message?: string }) | null
  duration: number | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export interface RoleRecord {
  id: string
  name: string
  permissions: string[]
  protected: boolean
  created_at: string
}

export interface UserRecord {
  id: string
  username: string
  enabled: boolean
  role_ids: string[]
  roles: RoleRecord[]
  permissions: string[]
  created_at: string
  updated_at: string
}

export interface SessionRecord {
  user: UserRecord
  permissions: string[]
  csrf_token: string | null
}

export interface APIKeyRecord {
  id: string
  user_id: string
  name: string
  prefix: string
  permissions: string[]
  function_ids: string[]
  created_at: string
  expires_at: string | null
  revoked_at: string | null
}
