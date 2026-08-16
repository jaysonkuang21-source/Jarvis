import { Check, Loader2, RefreshCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Slider, Switch } from '@/components/ui/controls'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { MODEL_TODO_ITEMS, modelTodoStatusLabel } from '@/lib/modelTodo'
import { useAppStore, type SettingsTab } from '@/stores/app'
import { useProfileStore } from '@/stores/profile'
import { ChoiceGroup } from './ChoiceGroup'
import { DocumentIngestPanel } from './DocumentIngestPanel'
import { EmbeddingModelField } from './EmbeddingModelField'
import { Field, SettingsSection } from './Field'
import {
  RoleRecommendControl,
  SuggestedModelsPanel,
} from './RoleRecommendControl'
import { RulesEditor } from './RulesEditor'
import { SystemSpecsBanner } from './SystemSpecsBanner'
import { useEffect, useState } from 'react'

type OptionChoice = { value: string; label: string; hint?: string }

/** Offline-safe choice lists when `/api/options` omits newer enums. */
const FALLBACK_QUERY_MODES: OptionChoice[] = [
  { value: 'local', label: 'Local', hint: 'Hybrid retrieve, then walk entity neighbourhoods.' },
  { value: 'global', label: 'Global', hint: 'Map-reduce over community summaries.' },
  { value: 'drift', label: 'DRIFT', hint: 'Probe broadly, then drill into promising regions.' },
  { value: 'auto', label: 'Auto', hint: 'A fast model chooses Local, Global, or DRIFT.' },
]

const FALLBACK_RAG_MODES: OptionChoice[] = [
  { value: 'regular', label: 'Regular', hint: 'One retrieval pass, then answer.' },
  { value: 'agentic', label: 'Agentic', hint: 'Grade relevance and rewrite until docs are useful.' },
]

const FALLBACK_INGEST_MODES: OptionChoice[] = [
  { value: 'regular', label: 'Text', hint: 'Markdown and extracted document text.' },
  { value: 'multimodal', label: 'Visual (ColPali)', hint: 'Page images with late interaction.' },
]

const FALLBACK_INGEST_EFFORTS: OptionChoice[] = [
  { value: 'manual', label: 'Manual', hint: 'Pick the chunker yourself.' },
  { value: 'low', label: 'Low', hint: 'Structure + wikilink-aware chunking.' },
  { value: 'medium', label: 'Medium', hint: 'A fast LLM picks among chunkers.' },
  { value: 'high', label: 'High', hint: 'Try several chunkers and score them.' },
]

const FALLBACK_CHUNKERS: OptionChoice[] = [
  { value: 'recursive', label: 'Recursive', hint: 'Split on headings, then by token budget.' },
  { value: 'semantic', label: 'Semantic', hint: 'Embedding-based boundaries.' },
  { value: 'structure_entity', label: 'Structure + links', hint: 'Headings and wikilink-aware splits.' },
  { value: 'claim_centered', label: 'Claim-centered', hint: 'Claim-sized units.' },
]

/** Map sparse API option rows onto ChoiceGroup items, falling back when empty. */
function optionChoices(
  fromApi: Record<string, string>[] | undefined,
  fallback: OptionChoice[],
): OptionChoice[] {
  if (fromApi && fromApi.length > 0) {
    const mapped: OptionChoice[] = fromApi
      .map((mode) => ({
        value: mode.value,
        label: mode.label || mode.value,
        hint: mode.hint,
      }))
      .filter((mode) => typeof mode.value === 'string' && mode.value.length > 0)
    if (mapped.length > 0) {
      // Prefer API rows, but fill any values the older backend omitted.
      const seen = new Set(mapped.map((m) => m.value))
      for (const row of fallback) {
        if (!seen.has(row.value)) mapped.push(row)
      }
      return mapped
    }
  }
  return fallback
}

/** Profile settings UI for retrieval, models, ingestion, and rules. */
export function SettingsView() {
  const {
    profile,
    options,
    validation,
    indexStatus,
    dirty,
    loading,
    loadError,
    update,
    save,
    load,
    refreshModels,
    refreshIndexStatus,
    startReindex,
  } = useProfileStore()
  const setView = useAppStore((s) => s.setView)
  const settingsTab = useAppStore((s) => s.settingsTab)
  const setSettingsTab = useAppStore((s) => s.setSettingsTab)
  const settingsFocus = useAppStore((s) => s.settingsFocus)
  const clearSettingsFocus = useAppStore((s) => s.clearSettingsFocus)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [reindexing, setReindexing] = useState(false)
  const [reindexError, setReindexError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)

  const chatModels = options?.chat_models ?? []
  const embeddingModels = options?.embedding_models ?? []
  const validationOk = Boolean(validation?.valid)

  /** Scroll to a deep-linked settings section after tab open (retry while async panes mount). */
  useEffect(() => {
    if (!settingsFocus) return
    let attempts = 0
    const tryFocus = () => {
      const el = document.querySelector<HTMLElement>(
        `[data-settings-focus="${settingsFocus}"]`,
      )
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        el.focus({ preventScroll: true })
        clearSettingsFocus()
        return true
      }
      return false
    }
    if (tryFocus()) return
    const timer = window.setInterval(() => {
      attempts += 1
      if (tryFocus() || attempts >= 40) {
        window.clearInterval(timer)
        if (attempts >= 40) clearSettingsFocus()
      }
    }, 50)
    return () => window.clearInterval(timer)
  }, [settingsFocus, settingsTab, clearSettingsFocus])

  /** Persist the draft profile and surface save failures. */
  async function onSave() {
    setSaving(true)
    setSaveError(null)
    try {
      await save()
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Could not save profile')
    } finally {
      setSaving(false)
    }
  }

  /** Re-fetch profile, options, and index status after a degraded bootstrap. */
  async function onRetryLoad() {
    setRetrying(true)
    try {
      await load()
    } finally {
      setRetrying(false)
    }
  }

  /** Kick off a vault reindex; force-clears a stuck indexing flag when needed. */
  async function onReindex(force = false) {
    setReindexing(true)
    setReindexError(null)
    try {
      await startReindex(force || Boolean(indexStatus?.indexing_stale))
    } catch (err) {
      // Live conflict → one force retry so users aren't stuck on "indexing…".
      if (!force && err instanceof Error && /409|already running/i.test(err.message)) {
        try {
          await startReindex(true)
          return
        } catch (retryErr) {
          setReindexError(
            retryErr instanceof Error
              ? retryErr.message
              : 'Force reindex failed',
          )
          return
        }
      }
      setReindexError(
        err instanceof Error
          ? err.message
          : 'Reindex unavailable — restart the backend if /api/index/reindex is missing',
      )
    } finally {
      setReindexing(false)
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-6 py-3">
        <div>
          <h1 className="text-sm font-semibold">Settings</h1>
          <p className="text-xs text-muted-foreground">
            {loading
              ? 'Loading profile…'
              : validationOk
                ? 'This configuration is valid.'
                : 'This configuration cannot run. See the errors below.'}
          </p>
          {loadError && (
            <div className="mt-2 flex flex-wrap items-center gap-2" role="status">
              <p className="text-xs text-warning">{loadError}</p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={retrying || loading}
                onClick={() => void onRetryLoad()}
              >
                {(retrying || loading) && <Loader2 className="size-3.5 animate-spin" />}
                Retry
              </Button>
            </div>
          )}
          {saveError && (
            <p className="mt-1 text-xs text-destructive" role="alert">
              Save failed: {saveError}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {!dirty && !saveError && (
            <span className="flex items-center gap-1 text-xs text-muted-foreground">
              <Check className="size-3.5" />
              Saved
            </span>
          )}
          <Button onClick={() => void onSave()} disabled={!dirty || saving} size="sm">
            {saving && <Loader2 className="size-3.5 animate-spin" />}
            Save profile
          </Button>
        </div>
      </header>

      <div className="scrollbar-thin flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-6 pb-12">
          <Tabs
            value={settingsTab}
            onValueChange={(value) => setSettingsTab(value as SettingsTab)}
          >
            <TabsList className="sticky top-0 z-10 w-full bg-background pt-3">
              <TabsTrigger value="retrieval">Retrieval</TabsTrigger>
              <TabsTrigger value="models">Models</TabsTrigger>
              <TabsTrigger value="ingestion">Ingestion</TabsTrigger>
              <TabsTrigger value="rules">Rules</TabsTrigger>
            </TabsList>

            <TabsContent value="retrieval">
              <SettingsSection
                id="retrieval-query"
                title="Query mode"
                description="Local = hybrid vector + keyword search walking your note graph. Global synthesises themes across community summaries. DRIFT probes then drills. Auto chooses for you."
              >
                <Field
                  name="query_mode"
                  label="Mode"
                  hint="Hybrid RRF powers Local and DRIFT drills. Global reads community reports built at reindex time."
                >
                  <ChoiceGroup
                    field="query_mode"
                    value={profile.query_mode}
                    onChange={(value) => update({ query_mode: value as never })}
                    choices={optionChoices(options?.query_modes, FALLBACK_QUERY_MODES)}
                  />
                </Field>

                <Field
                  name="rag_mode"
                  label="Strategy"
                  hint="Agentic grades retrieved docs, rewrites the query, and retries until relevant or the limit hits. Regular retrieves once then answers."
                >
                  <ChoiceGroup
                    field="rag_mode"
                    value={profile.rag_mode}
                    onChange={(value) => update({ rag_mode: value as never })}
                    choices={optionChoices(options?.rag_modes, FALLBACK_RAG_MODES)}
                  />
                </Field>

                {profile.rag_mode === 'agentic' && (
                  <Field
                    name="agentic_max_iters"
                    label="Agentic retries"
                    hint="Maximum retrieve → grade → rewrite cycles before graceful failure."
                  >
                    <SliderRow
                      value={profile.agentic_max_iters}
                      min={1}
                      max={8}
                      step={1}
                      onChange={(value) => update({ agentic_max_iters: value })}
                    />
                  </Field>
                )}

                <Field
                  name="community_level"
                  label="Community level"
                  hint="Depth of community reports Global and DRIFT may read. Deeper means more, smaller reports."
                >
                  <SliderRow
                    value={profile.community_level}
                    min={0}
                    max={4}
                    step={1}
                    onChange={(value) => update({ community_level: value })}
                  />
                </Field>

                <Field
                  name="top_k"
                  label="Results"
                  hint="How many fused candidates to keep after hybrid search and neighbourhood expansion."
                >
                  <SliderRow
                    value={profile.top_k}
                    min={1}
                    max={50}
                    step={1}
                    onChange={(value) => update({ top_k: value })}
                  />
                </Field>

                <Field
                  name="rrf_k"
                  label="RRF k"
                  hint="Constant in score = Σ 1/(k + rank). 60 is the common default."
                >
                  <SliderRow
                    value={profile.rrf_k}
                    min={1}
                    max={200}
                    step={1}
                    onChange={(value) => update({ rrf_k: value })}
                  />
                </Field>

                <Field name="hybrid_vector_top_k" label="Vector top-k">
                  <SliderRow
                    value={profile.hybrid_vector_top_k}
                    min={1}
                    max={100}
                    step={1}
                    onChange={(value) => update({ hybrid_vector_top_k: value })}
                  />
                </Field>

                <Field name="hybrid_keyword_top_k" label="Keyword top-k">
                  <SliderRow
                    value={profile.hybrid_keyword_top_k}
                    min={1}
                    max={100}
                    step={1}
                    onChange={(value) => update({ hybrid_keyword_top_k: value })}
                  />
                </Field>

                <Field
                  name="expand_to_parent"
                  label="Expand to section"
                  hint="Grows a retrieved chunk to its enclosing heading by reading the note from disk."
                >
                  <Switch
                    checked={Boolean(profile.expand_to_parent)}
                    onCheckedChange={(checked) => update({ expand_to_parent: checked })}
                  />
                </Field>

                <Field
                  name="rerank_model"
                  label="Rerank / evaluator model"
                  hint="Small utility LLM: scores fused hits and grades agentic relevance. Default qwen3.5:2b; changeable here."
                >
                  <Select
                    value={profile.rerank_model || undefined}
                    onValueChange={(value) => {
                      const model = chatModels.find((m) => m.id === value)
                      update({
                        rerank_model: value,
                        rerank_provider: model?.provider ?? 'ollama',
                      })
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Select a rerank model" />
                    </SelectTrigger>
                    <SelectContent>
                      {chatModels.length === 0 && (
                        <div className="px-2 py-3 text-xs text-muted-foreground">
                          No models found. Start Ollama or set an OpenAI key.
                        </div>
                      )}
                      {chatModels.map((model) => (
                        <SelectItem
                          key={model.id}
                          value={model.id}
                          disabled={!model.available}
                        >
                          {model.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <RoleRecommendControl role="rerank" />
                </Field>
              </SettingsSection>
            </TabsContent>

            <TabsContent value="models">
              <SettingsSection
                title="This machine"
                description="Local specs drive fit gates. Online metrics optionally enrich scores from Hugging Face Hub."
              >
                <SystemSpecsBanner />
                <Field
                  name="model_metrics_online"
                  label="Online model metrics"
                  hint="When on, recommendations phone home to huggingface.co for downloads/likes (cached 24h). Off by default."
                >
                  <Switch
                    checked={Boolean(profile.model_metrics_online)}
                    onCheckedChange={(checked) =>
                      update({ model_metrics_online: checked })
                    }
                  />
                </Field>
              </SettingsSection>

              <SettingsSection
                title="Suggested for this machine"
                description="Top pick per role from the curated catalog scored against your specs."
              >
                <SuggestedModelsPanel />
              </SettingsSection>

              <SettingsSection title="Chat model">
                <Field name="chat_model" label="Model">
                  <div className="flex gap-2">
                    <Select
                      value={profile.chat_model || undefined}
                      onValueChange={(value) => {
                        const model = chatModels.find((m) => m.id === value)
                        update({
                          chat_model: value,
                          chat_provider: model?.provider ?? 'ollama',
                        })
                      }}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Select a model" />
                      </SelectTrigger>
                      <SelectContent>
                        {chatModels.length === 0 && (
                          <div className="px-2 py-3 text-xs text-muted-foreground">
                            No models found. Start Ollama or set an OpenAI key.
                          </div>
                        )}
                        {chatModels.map((model) => (
                          <SelectItem
                            key={model.id}
                            value={model.id}
                            disabled={!model.available}
                          >
                            <span className="flex flex-col gap-0.5">
                              <span>{model.label}</span>
                              <span className="text-[10px] text-muted-foreground">
                                {model.provider}
                                {model.supports_vision && ' | vision'}
                                {!model.available && ` | ${model.unavailable_reason}`}
                              </span>
                            </span>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <Button
                      variant="outline"
                      size="icon"
                      title="Rescan for models"
                      disabled={refreshing}
                      onClick={() => {
                        setRefreshing(true)
                        void refreshModels().finally(() => setRefreshing(false))
                      }}
                    >
                      <RefreshCw
                        className={refreshing ? 'size-4 animate-spin' : 'size-4'}
                      />
                    </Button>
                  </div>
                  <RoleRecommendControl role="chat" />
                </Field>

                <Field
                  name="max_context_tokens"
                  label="Context budget"
                  hint="Upper bound on retrieved context packed into the prompt."
                >
                  <SliderRow
                    value={profile.max_context_tokens}
                    min={1024}
                    max={32768}
                    step={512}
                    onChange={(value) => update({ max_context_tokens: value })}
                  />
                </Field>
              </SettingsSection>

              <SettingsSection
                title="Voice model"
                description="Smaller LLM for the radar voice agent only. Chat / vault RAG keeps the Chat model above."
              >
                <Field
                  name="voice_model"
                  label="Model"
                  hint="Prefer a tiny instruct model (e.g. qwen3.5:2b) so Fish TTS and the chat Qwen do not fight for VRAM."
                >
                  <Select
                    value={profile.voice_model || undefined}
                    onValueChange={(value) => {
                      const model = chatModels.find((m) => m.id === value)
                      update({
                        voice_model: value,
                        voice_provider: model?.provider ?? 'ollama',
                      })
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Select a voice model" />
                    </SelectTrigger>
                    <SelectContent>
                      {chatModels.length === 0 && (
                        <div className="px-2 py-3 text-xs text-muted-foreground">
                          No models found. Start Ollama or set an OpenAI key.
                        </div>
                      )}
                      {chatModels.map((model) => (
                        <SelectItem
                          key={model.id}
                          value={model.id}
                          disabled={!model.available}
                        >
                          <span className="flex flex-col gap-0.5">
                            <span>{model.label}</span>
                            <span className="text-[10px] text-muted-foreground">
                              {model.provider}
                              {!model.available && ` | ${model.unavailable_reason}`}
                            </span>
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <RoleRecommendControl role="voice" />
                </Field>
              </SettingsSection>

              <SettingsSection
                title="Embedding model"
                description="Pinned for the vector index (qwen3-embedding:8b / 4096-d by default). Locked in Settings; advanced change forces a full re-index."
              >
                <Field name="embedding_model" label="Model">
                  <EmbeddingModelField models={embeddingModels} />
                </Field>
              </SettingsSection>

              <SettingsSection
                title="Models TODO"
                description="Models and ingest paths still incomplete. Full list: Models TODO rail and docs/MODEL_TODO.md."
              >
                <ul className="space-y-2">
                  {MODEL_TODO_ITEMS.map((item) => (
                    <li
                      key={item.id}
                      className="flex items-start justify-between gap-3 border-b border-border/50 pb-2 text-xs last:border-0"
                    >
                      <div className="min-w-0">
                        <p className="font-medium">{item.title}</p>
                        <p className="mt-0.5 text-muted-foreground">{item.note}</p>
                      </div>
                      <span className="shrink-0 font-mono text-[10px] tracking-wider text-muted-foreground">
                        {modelTodoStatusLabel(item.status)}
                      </span>
                    </li>
                  ))}
                </ul>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => setView('models')}
                >
                  Open Models TODO
                </Button>
              </SettingsSection>

              <SettingsSection
                title="Tracing"
                description="LangSmith uploads prompts and retrieved note content to its cloud."
              >
                <Field
                  name="tracing_enabled"
                  label="Send traces"
                  hint="Leave off if you are running local models for privacy: tracing sends the note text you retrieved off your machine."
                >
                  <Switch
                    checked={Boolean(profile.tracing_enabled)}
                    onCheckedChange={(checked) => update({ tracing_enabled: checked })}
                  />
                </Field>
              </SettingsSection>
            </TabsContent>

            <TabsContent value="ingestion">
              <DocumentIngestPanel onIndexed={() => void refreshIndexStatus()} />
              <SettingsSection id="ingestion-pipeline" title="Pipeline">
                <Field
                  name="ingest_mode"
                  label="Ingestion"
                  hint="Visual ingestion builds a separate page-image index with no entities or communities, so Global and DRIFT cannot read it and the answering model must support vision."
                >
                  <ChoiceGroup
                    field="ingest_mode"
                    value={profile.ingest_mode}
                    onChange={(value) => update({ ingest_mode: value as never })}
                    choices={optionChoices(options?.ingest_modes, FALLBACK_INGEST_MODES)}
                  />
                </Field>

                <Field
                  name="ingest_effort"
                  label="Ingestion effort"
                  hint="Manual picks a chunker. Low uses structure and wikilinks. Medium and high need a fast decision model."
                >
                  <ChoiceGroup
                    field="ingest_effort"
                    value={profile.ingest_effort}
                    onChange={(value) => update({ ingest_effort: value as never })}
                    choices={optionChoices(options?.ingest_efforts, FALLBACK_INGEST_EFFORTS)}
                  />
                </Field>

                {profile.ingest_effort === 'manual' && (
                  <Field
                    name="chunker"
                    label="Chunking"
                    hint="Recursive splits on headings, then by token budget. Structure + links respects the Obsidian graph. Semantic costs an embedding call per sentence."
                  >
                    <ChoiceGroup
                      field="chunker"
                      value={profile.chunker}
                      onChange={(value) => update({ chunker: value as never })}
                      choices={optionChoices(options?.chunkers, FALLBACK_CHUNKERS)}
                    />
                  </Field>
                )}

                {(profile.ingest_effort === 'medium' ||
                  profile.ingest_effort === 'high') && (
                  <Field
                    name="chunk_decision_model"
                    label="Decision model"
                    hint="Fast/small chat model that picks (medium) or scores (high) chunking strategies. Default qwen3.5:2b; changeable anytime."
                  >
                    <Select
                      value={profile.chunk_decision_model || undefined}
                      onValueChange={(value) => {
                        const model = chatModels.find((m) => m.id === value)
                        update({
                          chunk_decision_model: value,
                          chunk_decision_provider: model?.provider ?? 'ollama',
                        })
                      }}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Select a fast model" />
                      </SelectTrigger>
                      <SelectContent>
                        {chatModels.length === 0 && (
                          <div className="px-2 py-3 text-xs text-muted-foreground">
                            No models found. Start Ollama or set an OpenAI key.
                          </div>
                        )}
                        {chatModels.map((model) => (
                          <SelectItem
                            key={model.id}
                            value={model.id}
                            disabled={!model.available}
                          >
                            <span className="flex flex-col gap-0.5">
                              <span>{model.label}</span>
                              <span className="text-[10px] text-muted-foreground">
                                {model.provider}
                                {!model.available && ` | ${model.unavailable_reason}`}
                              </span>
                            </span>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <RoleRecommendControl role="chunk_decision" />
                  </Field>
                )}

                <Field
                  name="chunk_size"
                  label="Chunk size"
                  hint="Tokens per chunk. Smaller chunks usually preserve more local detail for an Obsidian vault."
                >
                  <SliderRow
                    value={profile.chunk_size}
                    min={256}
                    max={2400}
                    step={50}
                    onChange={(value) => update({ chunk_size: value })}
                    suffix="tokens"
                  />
                </Field>

                <Field name="chunk_overlap" label="Overlap">
                  <SliderRow
                    value={profile.chunk_overlap}
                    min={0}
                    max={400}
                    step={10}
                    onChange={(value) => update({ chunk_overlap: value })}
                    suffix="tokens"
                  />
                </Field>

                <Field
                  name="prepend_note_context"
                  label="Prepend note context"
                  hint="Adds the note title and heading path to each chunk. Vault notes are terse and full of unresolved pronouns, so this is the highest-leverage setting for extraction quality."
                >
                  <Switch
                    checked={Boolean(profile.prepend_note_context)}
                    onCheckedChange={(checked) =>
                      update({ prepend_note_context: checked })
                    }
                  />
                </Field>
                <Field
                  name="extraction_model"
                  label="Extraction model"
                  hint="LLM used at reindex time to extract entities and relationships (alongside Obsidian wikilinks)."
                >
                  <Select
                    value={profile.extraction_model || undefined}
                    onValueChange={(value) => {
                      const model = chatModels.find((m) => m.id === value)
                      update({
                        extraction_model: value,
                        extraction_provider: model?.provider ?? 'ollama',
                      })
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Select an extraction model" />
                    </SelectTrigger>
                    <SelectContent>
                      {chatModels.length === 0 && (
                        <div className="px-2 py-3 text-xs text-muted-foreground">
                          No models found. Start Ollama or set an OpenAI key.
                        </div>
                      )}
                      {chatModels.map((model) => (
                        <SelectItem
                          key={model.id}
                          value={model.id}
                          disabled={!model.available}
                        >
                          {model.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <RoleRecommendControl role="extraction" />
                </Field>
              </SettingsSection>

              <SettingsSection id="ingestion-index" title="Index">
                <div className="flex flex-wrap gap-1.5 py-3">
                  <Badge variant="outline">engine {indexStatus?.engine ?? 'none'}</Badge>
                  <Badge variant={indexStatus?.ready ? 'success' : 'warning'}>
                    {indexStatus?.ready ? 'ready' : 'not built'}
                  </Badge>
                  <Badge variant={indexStatus?.indexing ? 'warning' : 'outline'}>
                    {indexStatus?.indexing ? 'indexing…' : 'idle'}
                  </Badge>
                  <Badge variant="outline">
                    {indexStatus?.indexed_notes ?? 0} / {indexStatus?.total_notes ?? 0} notes
                  </Badge>
                  <Badge variant="outline">{indexStatus?.entities ?? 0} entities</Badge>
                  <Badge variant="outline">
                    {indexStatus?.relationships ?? 0} relationships
                  </Badge>
                  <Badge variant="outline">
                    {indexStatus?.communities ?? 0} communities
                  </Badge>
                  {indexStatus?.extraction_model && (
                    <Badge variant="outline">
                      extracted with {indexStatus.extraction_model}
                    </Badge>
                  )}
                  {indexStatus?.embedding_model && (
                    <Badge variant="outline">
                      embedded with {indexStatus.embedding_model}
                    </Badge>
                  )}
                </div>
                <div className="flex flex-wrap gap-2 pb-3">
                  <Button
                    size="sm"
                    disabled={reindexing}
                    onClick={() =>
                      void onReindex(
                        Boolean(indexStatus?.indexing || indexStatus?.indexing_stale),
                      )
                    }
                  >
                    {(reindexing || (indexStatus?.indexing && !indexStatus?.indexing_stale)) && (
                      <Loader2 className="size-3.5 animate-spin" />
                    )}
                    {indexStatus?.indexing_stale || indexStatus?.indexing
                      ? 'Force reindex'
                      : 'Reindex vault'}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => void refreshIndexStatus()}
                  >
                    Refresh status
                  </Button>
                </div>
                {indexStatus?.indexing_stale && (
                  <p className="pb-2 text-xs text-critical/90" role="status">
                    Indexing looks stuck from a previous session. Use Force reindex to clear it.
                  </p>
                )}
                {reindexError && (
                  <p className="pb-2 text-xs text-destructive" role="alert">
                    {reindexError}
                  </p>
                )}
                <p className="pb-3 text-xs leading-relaxed text-muted-foreground">
                  Requires JARVIS_DATABASE_URL and a running Postgres with pgvector.
                  Extraction and embedding models used for a build are recorded on the
                  index; changing the embedding model invalidates stored vectors.
                </p>
              </SettingsSection>
            </TabsContent>

            <TabsContent value="rules">
              <RulesEditor />
            </TabsContent>
          </Tabs>
        </div>
      </div>
    </div>
  )
}

/** Numeric slider with a monospace readout of the current value. */
function SliderRow({
  value,
  min,
  max,
  step,
  suffix,
  onChange,
}: {
  value: number
  min: number
  max: number
  step: number
  suffix?: string
  onChange: (value: number) => void
}) {
  // Older/sparse profiles can omit slider fields; never call toLocaleString on undefined.
  const safe = Number.isFinite(value) ? value : min
  return (
    <div className="flex items-center gap-3">
      <Slider
        value={[safe]}
        min={min}
        max={max}
        step={step}
        onValueChange={([next]) => onChange(next)}
        className="flex-1"
      />
      <span className="w-24 shrink-0 text-right font-mono text-xs tabular-nums text-muted-foreground">
        {safe.toLocaleString()}
        {suffix ? ` ${suffix}` : ''}
      </span>
    </div>
  )
}
