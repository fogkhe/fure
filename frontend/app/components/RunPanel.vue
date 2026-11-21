<script setup lang="ts">
import type { FunctionRecord, JsonObject, JsonValue, RunRecord, RunResult } from '~/types/api'
import { inputSchema, invalidInputFields, isJsonObject, parseInputJson } from '~/utils/inputSchema'

const { formatDate, formatDuration, errorMessage, translateDetail } = useFormatting()
const { t } = useI18n()

const props = defineProps<{
  record: FunctionRecord
}>()
const api = useApi()
const auth = useAuth()
const toast = useToast()
const initialInput = ref(JSON.stringify(sampleInput(props.record.input_schema), null, 2))
const input = ref(initialInput.value)
const exampleVersion = ref(0)
const rootSchema = computed(() => inputSchema(props.record.input_schema, props.record.input_schema))
const hasFields = computed(() => isJsonObject(rootSchema.value.properties) && Object.keys(rootSchema.value.properties).length > 0)
const inputMode = ref(hasFields.value ? 'fields' : 'json')
const inputModes = computed(() => [{ label: t('inputEditor.fields'), value: 'fields' }, { label: 'JSON', value: 'json' }])
const form = useTemplateRef<HTMLFormElement>('inputForm')
const invalidFields = reactive(new Set<symbol>())
provide(invalidInputFields, invalidFields)

function validateFields() {
  if (invalidFields.size) {
    error.value = t('inputEditor.invalidJson')
    return false
  }
  return form.value?.reportValidity()
}
const fieldValue = computed<JsonValue>({
  get: () => {
    try {
      return parseInputJson(input.value)
    } catch {
      return null
    }
  },
  set: (value) => { input.value = JSON.stringify(value, null, 2) }
})
function changeMode(mode: string | number) {
  if (mode === inputMode.value) return
  if (inputMode.value === 'fields' && !validateFields()) return
  if (mode === 'fields') {
    try {
      const parsed = parseInputJson(input.value)
      if (parsed !== null && !isJsonObject(parsed)) throw new Error(t('ui.expectedAJsonObjectOrNull'))
    } catch (problem) {
      error.value = errorMessage(problem)
      return
    }
  }
  error.value = ''
  inputMode.value = String(mode)
}
const running = ref(false)
const cancelling = ref(false)
const cancelled = ref(false)
const cancelOpen = ref(false)
const canCancel = computed(() => auth.can('runs.read') && auth.can('runs.cancel'))
const error = ref('')
const result = shallowRef<RunResult | null>(null)
const currentRun = shallowRef<RunRecord | null>(null)
const runMeta = computed(() => currentRun.value?.meta ?? result.value?.meta)
const toolCalls = computed(() => runMeta.value?.tool_calls ?? [])
const assistantComments = computed(() => runMeta.value?.assistant_comments ?? [])
const hasProgress = computed(() => toolCalls.value.length > 0 || assistantComments.value.length > 0)
const runId = ref<string | null>(null)
const showSchema = ref(false)
let controller: AbortController | undefined
function fillExample() {
  exampleVersion.value++
  initialInput.value = JSON.stringify(sampleInput(props.record.input_schema), null, 2)
  input.value = initialInput.value
}
watch(() => props.record.revision_id, () => {
  if (input.value === initialInput.value)
    fillExample()
})
async function execute() {
  if (running.value || !props.record.runnable)
    return
  if (inputMode.value === 'fields' && !validateFields())
    return
  error.value = ''
  result.value = null
  currentRun.value = null
  runId.value = null
  cancelled.value = false
  cancelling.value = false
  let value: JsonObject | null
  try {
    value = parseInputJson(input.value) as JsonObject | null
    if (value !== null && (typeof value !== 'object' || Array.isArray(value)))
      throw new Error(t('ui.expectedAJsonObjectOrNull'))
  } catch (problem) {
    error.value = errorMessage(problem)
    return
  }
  running.value = true
  controller = new AbortController()
  const signal = controller.signal
  try {
    const endpoint = `/functions/${props.record.id}/run`
    if (auth.can('runs.read')) {
      let run = await api<RunRecord>(endpoint, { method: 'POST', query: { wait: false }, body: { input: value }, signal })
      currentRun.value = run
      runId.value = run.id
      while (run.status === 'pending' || run.status === 'running') {
        run = await api<RunRecord>(`/runs/${run.id}`, { signal })
        currentRun.value = run
        if (run.status === 'pending' || run.status === 'running') {
          await new Promise(resolve => setTimeout(resolve, 500))
          signal.throwIfAborted()
        }
      }
      if (run.status === 'succeeded')
        result.value = { output: run.output, meta: run.meta ?? {}, duration: run.duration ?? 0 }
      else if (run.status === 'cancelled')
        cancelled.value = true
      else
        error.value = run.meta?.message ? translateDetail(run.meta.message) : t('ui.couldNotExecuteTheFunction')
    } else {
      const response = await api.raw<RunResult>(endpoint, { method: 'POST', body: { input: value }, signal })
      result.value = response._data ?? null
      runId.value = response.headers.get('x-run-id')
    }
  } catch (problem) {
    if (!signal.aborted) {
      error.value = errorMessage(problem)
      runId.value = (problem as {
        response?: Response
      }).response?.headers.get('x-run-id') ?? runId.value
    }
  } finally {
    running.value = false
    cancelling.value = false
    cancelOpen.value = false
  }
}
async function cancel() {
  const target = runId.value
  if (!target || !running.value || cancelling.value)
    return
  cancelling.value = true
  try {
    await api(`/runs/${target}/cancel`, { method: 'POST' })
    if (runId.value === target)
      cancelOpen.value = false
  } catch (problem) {
    if (runId.value === target) {
      cancelling.value = false
      toast.add({ title: t('ui.couldNotCancelRun'), description: errorMessage(problem), color: 'error' })
    }
  }
}
onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <div class="space-y-5">
    <UAlert
      v-if="!record.runnable"
      color="warning"
      icon="i-lucide-circle-alert"
      :title="t('ui.thisFunctionIsNotReadyToRun')"
      :description="t('ui.selectACurrentImplementationAndEnableTheFunctionIn')"
    />
    <div class="grid items-start gap-6 lg:grid-cols-2">
      <UCard>
        <template #header>
          <div class="flex items-center justify-between gap-3">
            <h2 class="font-semibold text-highlighted">
              {{ t('ui.inputData') }}
            </h2><UButton
              :label="t('ui.exampleFromSchema')"
              icon="i-lucide-braces"
              size="xs"
              color="neutral"
              variant="ghost"
              :disabled="running"
              @click="fillExample"
            />
          </div>
        </template>
        <form
          ref="inputForm"
          class="space-y-4"
          @submit.prevent="execute"
        >
          <div
            v-if="hasFields"
            class="flex items-center justify-between gap-3"
          >
            <span class="text-sm text-muted">{{ t('inputEditor.editor') }}</span>
            <USelect
              :model-value="inputMode"
              :items="inputModes"
              :aria-label="t('inputEditor.editor')"
              :disabled="running"
              class="w-36"
              @update:model-value="changeMode"
            />
          </div>
          <SchemaInputField
            v-if="hasFields && inputMode === 'fields'"
            :key="`${record.revision_id}:${exampleVersion}`"
            v-model="fieldValue"
            :schema="record.input_schema"
            :root="record.input_schema"
            label="Input"
            required
            :disabled="running"
          />
          <UFormField
            v-else
            label="Input · JSON"
            :description="t('ui.parametersArePassedToTheSelectedImplementation')"
          >
            <CodeEditor
              v-model="input"
              language="json"
              label="Input · JSON"
              :rows="12"
              :disabled="running"
            />
          </UFormField>
          <div class="flex flex-wrap items-center justify-between gap-3">
            <UButton
              v-if="running && canCancel"
              type="button"
              :label="t('ui.cancel2')"
              icon="i-lucide-square"
              color="error"
              :loading="cancelling"
              :disabled="!runId"
              @click="cancelOpen = true"
            />
            <UButton
              v-else
              type="submit"
              :label="t('ui.run2')"
              icon="i-lucide-play"
              :loading="running"
              :disabled="!record.runnable || (!record.public && !auth.can('functions.run'))"
            /><UButton
              :label="t('ui.inputSchema')"
              color="neutral"
              variant="ghost"
              size="sm"
              @click="showSchema = !showSchema"
            />
          </div>
          <JsonView
            v-if="showSchema"
            :value="record.input_schema"
            :title="t('ui.inputSchema')"
          />
        </form>
        <template #footer>
          <p
            v-if="auth.can('runs.read')"
            class="text-xs text-muted"
          >
            <NuxtLink
              :to="`/runs?function_id=${record.id}`"
              class="text-primary hover:underline"
            >{{ t('ui.runHistory') }}</NuxtLink>{{ t('ui.closingThisPageDoesNotCancelExecution') }}
          </p>
        </template>
      </UCard>
      <div
        class="space-y-4"
        aria-live="polite"
      >
        <UAlert
          v-if="cancelled"
          :title="t('ui.runCancelled')"
          :description="t('ui.executionWasInterruptedTheHistoryEntryHasBeenPreserved')"
          icon="i-lucide-ban"
          color="neutral"
        />
        <UAlert
          v-if="error"
          :title="t('ui.couldNotExecuteTheFunction2')"
          :description="error"
          icon="i-lucide-circle-alert"
          color="error"
        />
        <div
          v-if="result"
          class="flex flex-wrap items-center gap-3"
        >
          <StatusBadge status="succeeded" />
          <span class="text-sm text-muted">{{ formatDuration(result.duration) }}</span>
          <UBadge
            v-if="result.meta.total_tokens != null"
            color="neutral"
            variant="subtle"
          >
            {{ t('counts.tokens', result.meta.total_tokens) }}
          </UBadge>
        </div>
        <StatusBadge
          v-else-if="running && hasProgress"
          status="running"
        />
        <JsonView
          v-if="result || hasProgress"
          :value="result?.output"
          :title="t('ui.result')"
          :hide-value="!result"
          text
        >
          <section
            v-if="assistantComments.length"
            class="space-y-3 p-4"
            :class="{ 'border-t border-default': result }"
          >
            <h3 class="text-sm font-medium text-highlighted">
              {{ t('agent.assistantComments') }}
            </h3>
            <ul class="space-y-2">
              <li
                v-for="(comment, index) in assistantComments"
                :key="index"
                class="rounded-md bg-elevated p-2.5"
              >
                <div
                  v-if="comment.step != null || comment.received_at"
                  class="mb-1 flex flex-wrap gap-x-3 text-xs text-muted"
                >
                  <span v-if="comment.step != null">{{ t('agent.step', { step: comment.step }) }}</span>
                  <time
                    v-if="comment.received_at"
                    :datetime="comment.received_at"
                  >{{ formatDate(comment.received_at, 'medium') }}</time>
                </div>
                <p class="text-sm wrap-break-word whitespace-pre-wrap">
                  {{ comment.text }}
                </p>
              </li>
            </ul>
          </section>
          <section
            v-if="toolCalls.length"
            class="space-y-3 p-4"
            :class="{ 'border-t border-default': result || assistantComments.length }"
          >
            <h3 class="text-sm font-medium text-highlighted">
              {{ t('agent.toolCalls') }}
            </h3>
            <ol class="space-y-3">
              <li
                v-for="call in toolCalls"
                :key="call.run_id"
                class="space-y-1.5"
              >
                <div class="text-xs break-all">
                  <span class="text-muted">{{ call.index }}.</span>
                  <code class="ml-1 font-medium text-highlighted">{{ call.name }}</code>
                  <span
                    v-if="call.step != null"
                    class="ml-2 text-muted"
                  >{{ t('agent.step', { step: call.step }) }}</span>
                </div>
                <CodeBlock
                  :source="JSON.stringify(call.arguments, null, 2)"
                  language="json"
                  class="overflow-auto rounded-md bg-elevated p-2.5 text-xs"
                />
              </li>
            </ol>
          </section>
        </JsonView>
        <JsonView
          v-if="runMeta && Object.keys(runMeta).length"
          :value="runMeta"
          :title="t('ui.metadata')"
        />
        <UEmpty
          v-if="!result && !hasProgress && !error && !cancelled"
          :icon="running ? 'i-lucide-loader-circle' : 'i-lucide-terminal'"
          :title="running ? t('ui.functionIsRunning') : t('ui.theResultWillAppearHere')"
          :description="running ? t('ui.waitingForAResponseFromTheSelectedImplementation') : t('ui.enterInputDataAndRunTheFunction')"
          class="min-h-72 border border-dashed border-default"
        />
        <UButton
          v-if="runId && auth.can('runs.read')"
          :to="`/runs/${runId}`"
          :label="t('ui.openRunRecord')"
          icon="i-lucide-arrow-up-right"
          color="neutral"
          variant="link"
        />
      </div>
    </div>
    <UModal
      v-model:open="cancelOpen"
      :title="t('ui.cancelRun2')"
      :description="t('ui.executionWillBeInterruptedInputDataAndTheHistory')"
      :dismissible="!cancelling"
    >
      <template #footer>
        <div class="flex w-full justify-end gap-2">
          <UButton
            :label="t('ui.back')"
            color="neutral"
            variant="ghost"
            :disabled="cancelling"
            @click="cancelOpen = false"
          />
          <UButton
            :label="t('ui.cancelRun')"
            color="error"
            :loading="cancelling"
            @click="cancel"
          />
        </div>
      </template>
    </UModal>
  </div>
</template>
