# summarizer

> Per-intent LLM digest. The matcher hands off a list of `Match` objects via `app.state.on_match`; the pipeline renders a system + instruction prompt from disk-backed Markdown templates, calls the configured LLM backend, wraps the result in `SummaryResult`, and forwards it to the notifier-installed `on_summary` callback.

## Responsibility

- Define the LLM backend ABC (`BaseLLMBackend`) so additional backends (Ollama, mlx-lm, Claude, Gemini) can be plugged in without touching the pipeline
- Ship a working OpenAI-compatible backend (`APIBackend`) that targets any `/v1/chat/completions` endpoint
- Discover, load, and render prompt templates under `prompts_dir` with a strict placeholder whitelist; expose a small reusable helper module for the API and intent layers
- Receive a per-intent batch of `Match` objects and produce one `SummaryResult` per call — there is no in-pipeline grouping or fan-out. The LLM structures the digest itself; the matcher / event buffer decides what to send
- Convert article HTML to plain text (entity-decoded, link-stripped) before the body reaches the LLM, and water-fill bodies into the backend's published prompt budget so short articles stay whole and only the longest get truncated
- Build a list of `Citation` objects so the notifier can render footnotes without re-querying the database
- Provide a pre-push hook seam so a future feature can veto a summary right before delivery (rate limiting, content filters)
- Surface a separate `on_template_error` callback so the operator gets a notification when a renamed or syntactically broken template stops a tick — silent failure on prompts is worse than a delivery error

## Not in scope

- Match selection — the matcher decides which articles a tick covers
- Per-article scoring or grouping by similarity — `summarizer.grouping` exists for the event buffer, not for the cron path. The pipeline treats the whole batch as one digest
- Channel formatting and retries — `notifier` owns those
- Prompt engineering for specific use cases — bundled `prompts/system/default.md` and `prompts/instruction/default.md` are the starting point; users override them per-intent

## Public interface

### Domain types (`models.py`)

```python
@dataclass
class Citation:
    article_id: str
    title: str
    url: str
    source: int               # feed_id; raw integer for downstream lookups
    published_at: str | None
    source_name: str | None    # resolved feed.name; None when the feed was deleted

@dataclass
class SummaryResult:
    intent_id: int
    summary: str
    citations: list[Citation]      # canonical ordered list; [N] in `summary` indexes into this
    primary: Citation | None       # citations[0] for legacy callers
    other_sources: list[Citation]  # citations[1:]

PrePushHook = Callable[[SummaryResult], Awaitable[bool]]
OnSummaryCallback = Callable[[SummaryResult], Awaitable[None]]
```

`citations` is the new contract; `primary` and `other_sources` are preserved for callers (notifier templates, tests) that predate the unified list.

### Pipeline (`pipeline.py`)

```python
class SummaryPipeline:
    def __init__(
        self,
        llm: BaseLLMBackend,
        on_summary: OnSummaryCallback | None = None,
        pre_push_hook: PrePushHook | None = None,
        get_intent_prompt_ctx: IntentPromptCtxFetcher | None = None,
        get_feed_names: FeedNameFetcher | None = None,
        on_template_error: OnTemplateError | None = None,
        prompts_dir: Path = Path("/app/prompts"),
    )

    async def handle(self, matches: list[Match]) -> None
```

`handle` is the entry point installed as `app.state.on_match`. It is contractually never-raise — any exception is logged and the tick is silently skipped, mirroring the matcher's `log_matches` contract.

A single tick:

1. Sort `matches` newest-first by `published_at` (None sorts last)
2. Resolve `(system_template_name, instruction_template_name, intent_text, language)` via the supplied `get_intent_prompt_ctx`; an empty `intent_text` short-circuits the tick
3. Render the system template with `{language}`; a missing or broken system template routes to `on_template_error` and stops the tick
4. Resolve feed names for citations via `get_feed_names`; failure here logs but does not abort
5. Render the instruction template once with `articles=""` to measure the wrapper's character cost
6. Compute the body budget: `llm.max_prompt_chars × 0.85 − len(system) − len(instruction wrapper) − per-entry boilerplate`. If the budget is negative (system + instruction alone exceed 85% of the model's prompt budget), log an error and stop the tick — that is a configuration problem, not a data problem
7. Water-fill the article bodies into the body budget: short articles stay whole, only bodies above the cap are truncated. Log a warning when truncation happens, including the cap level and how many bodies were affected
8. Re-render the instruction template with the assembled `articles` block; the same template-error routing as step 3 applies
9. Call `llm.summarize(prompt, system=system_prompt)`; an LLM error logs and stops the tick (no fallback)
10. Build the `SummaryResult` (citations indexed 1..N matching the LLM's `[N]` references)
11. Optionally consult `pre_push_hook`; a False return drops the result silently
12. Hand off to `on_summary`

### LLM backend ABC (`llm/base.py`)

```python
class BaseLLMBackend(ABC):
    @property
    @abstractmethod
    def max_prompt_chars(self) -> int  # total prompt budget the backend accepts
    async def summarize(self, prompt: str, *, system: str | None = None) -> str  # raises LLMError
    async def health(self) -> bool
    async def aclose(self) -> None
```

`max_prompt_chars` is the contract that lets the pipeline size articles correctly without re-tuning a separate setting on every model swap. Backends that front more than one model must publish the budget for the actually-configured one. Characters (not tokens) because the pipeline operates on strings; tokens-per-character varies by language and tokenizer, so callers should set this conservatively.

`summarize` returns a Markdown string per the system template's contract. `system` is sent as the OpenAI system message; backends that don't support a system role should prepend it to `prompt`. `health` is reachable-only — it does not validate the model name; expect upstream proxies that respond 200 on `/models` even when the configured model is wrong.

### OpenAI-compatible backend (`llm/api.py`)

```python
class APIBackend(BaseLLMBackend):
    def __init__(
        self,
        base_url: str, api_key: str, model: str,
        timeout: float, max_prompt_chars: int,
    )
```

Targets `/v1/chat/completions` on any OpenAI-shaped endpoint (SiliconFlow / DeepSeek / OpenAI / a self-hosted vLLM behind a proxy). Errors are translated to `LLMError`; non-2xx response bodies are scrubbed of the API key before being raised, so a misconfigured proxy that echoes the `Authorization` header in a 401 body does not leak the key into logs.

### Factory (`llm/factory.py`)

```python
def build_llm_backend(settings: Settings) -> BaseLLMBackend
```

Currently returns `APIBackend` unconditionally. Local backends (mlx-lm, Ollama) plug in here when available; the factory is the only place `Settings` is read so the backend constructors stay test-friendly.

### Templates (`templates.py`)

```python
def template_exists(prompts_dir, kind, name) -> bool
def list_templates(prompts_dir, kind) -> list[str]
def load_template(prompts_dir, kind, name) -> str
def render_system(prompts_dir, name, *, language) -> str
def render_instruction(prompts_dir, name, *, intent_text, articles) -> str

class TemplateNotFoundError(FileNotFoundError): ...
class TemplateRenderError(ValueError): ...
```

Templates live under `prompts_dir/{system,instruction}/{name}.md`. `name` is validated to reject path separators, leading dots, and `..` segments; the resolved path is checked with `Path.is_relative_to(prompts_dir)` so a user-supplied name cannot escape the prompts root via symlinks. Files are read on every call (no in-process caching) so an operator's edit takes effect on the next tick without a restart.

Rendering is `str.format_map` with a strict `__missing__` that raises `KeyError` for any placeholder outside the documented whitelist:

| Kind | Allowed placeholders |
|---|---|
| system | `{language}` |
| instruction | `{intent_text}`, `{articles}` |

Anything else triggers `TemplateRenderError`, which the pipeline routes to `on_template_error` so the operator gets a notification rather than a silently-broken digest.

### Title grouping (`grouping.py`)

```python
def normalize(title: str) -> str
class GroupingStep:
    def __init__(self, threshold: float = 0.85)
    def group(self, matches: list[Match]) -> list[list[Match]]
```

`SequenceMatcher`-based union-find clustering of titles. The pipeline does **not** call this — it is exported for the matcher's event-buffer (`matcher/event_buffer.py`), which uses it to merge near-duplicate cross-source reports inside an event tick. The threshold defaults to 0.85 (tight; only catches near-identical headlines).

## Configuration

| Field | Default | Notes |
|---|---|---|
| `llm_api_base_url` | `https://api.siliconflow.cn/v1` | OpenAI-compatible base; SiliconFlow shares its key with the embedder |
| `llm_api_key` | `""` (empty) | Empty value logs a warning at startup; every LLM call returns 401 |
| `llm_model` | `deepseek-ai/DeepSeek-V4-Flash` | Passed verbatim as `"model"` in the request body |
| `llm_timeout_seconds` | `60.0` | Per-request HTTP timeout |
| `llm_max_prompt_chars` | `2_000_000` | Total prompt-side character budget for the LLM backend (system + instruction + articles). Tune to the configured model's context window: 2_000_000 is roomy for DeepSeek-V4-Flash (1M token ctx); drop to ~16_000 for an 8K-token local model. Pipeline reserves 15% for the LLM response and water-fills bodies into the rest |
| `prompts_dir` | `/app/prompts` | Bind-mounted in the bundled `docker-compose.yml`; override via `SEMBR_PROMPTS_DIR` for local dev |

## Upstream dependencies

- `config.Settings` — every LLM and prompts path setting
- `matcher.callback.Match` — the input shape `handle` consumes
- `db.intents.get_intent` (indirectly, via the lifespan-installed `get_intent_prompt_ctx`) — resolves per-intent template names, intent text, and language
- `db.feeds` (indirectly, via `get_feed_names`) — resolves `Citation.source_name`

## Downstream consumers

- `notifier.email.EmailChannel` — receives the `SummaryResult` through the lifespan-installed `on_summary` callback
- `notifier.email.EmailChannel.send_error` — receives template errors through `on_template_error`
- `dashboard.read_model` — reads `notification_log` rows whose state machine the notifier owns; the summarizer itself never touches that table
- `api.prompts` and `api.intents` — reuse `templates.list_templates`, `template_exists`, `load_template` to power the `/api/prompts` browser and the intent-create template-name validation

## Known constraints

- **One summary per intent per tick**: the pipeline does not split a batch into sub-events. The matcher decides what reaches a single tick — under cron mode that is everything that scored above threshold in the lookback window; under event mode that is the flushed `event_pending` rows. If you want per-event splitting on the cron path, wrap `handle` and call it once per pre-grouped sub-list.
- **Templates are read on every call**: no in-process caching. Disk I/O cost is negligible at MVP scale (a few intents firing per minute) but a future high-fan-out deployment should add a cache or move templates to a database table.
- **Truncation is character-aligned, not sentence-aware**: when water-filling forces a body shorter than its full length, the cut happens at character position N — no sentence boundary lookup, no Markdown fence repair. A long article gets a clean middle and a sliced tail. Acceptable for monitoring digests; not appropriate for a summarization product where the full text matters.
- **Token-vs-character heuristics live with the operator**: `llm_max_prompt_chars` is in characters. The pipeline never tokenizes; English ≈ 4 chars/token and Chinese ≈ 1 char/token, so a setting that fits a 1M-token model on English content (4M chars) might overflow on a Chinese-heavy intent. Set conservatively for non-English deployments.
- **`APIBackend` constructs `httpx.AsyncClient` in `__init__`**: the client is bound to whichever event loop is running at construction time. Production wiring constructs it inside `lifespan`, so this is fine; a test that constructs the backend at module import time and then runs an asyncio test will fail with "event loop is closed".
- **`health()` does not validate the model name**: it pings `/models` for reachability. A misconfigured `llm_model` (typo, removed from upstream) will only surface on the first real `summarize` call.
- **No retry policy on transient LLM errors**: `summarize` raises on the first non-2xx; the pipeline logs and drops the tick. Cron mode re-tries naturally on the next schedule; event mode loses the buffered batch (it was already cleared by `flush` before `on_summary`). A future change should add a small retry budget for 429 / 5xx specifically.
- **Default fallback strings have been removed**: earlier versions of the pipeline carried hardcoded copies of `default.md` for both system and instruction templates. Those were never used at runtime — template errors route to `on_template_error` instead — and have been deleted to avoid two versions of the same prompt drifting apart. The on-disk `prompts/system/default.md` and `prompts/instruction/default.md` are now the single source of truth.
