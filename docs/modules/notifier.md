# notifier

> Push delivery layer. Receives a `SummaryResult` from the summarizer (via the lifespan-installed `on_summary` callback) and renders it for one or more configured channels. Today the only built-in channel is `email`; the module is structured so additional channels (Telegram, Discord, Slack) can land without touching the summarizer or the dispatcher.

## Responsibility

- Define a marker base class so the dispatcher in `main.py` can route a result to all channels configured on an intent via simple `isinstance` checks
- Own the per-channel config schema — each channel ships its own Pydantic model so the API boundary validates channel parameters before they ever reach `send`
- Render an LLM-produced Markdown digest into HTML safe for HTML-only mail clients (escape user-supplied titles, scrub citation references the LLM hallucinated, embed the brand logo as `cid:`-referenced inline image)
- Render per-citation metadata in the **intent's** timezone, not the deployment-wide one — different intents may belong to different operators
- Surface the matcher's relevance score next to each cited article so a reader can tell at a glance which sources drove the match
- Never raise out of `send` / `send_error`: a delivery failure must not abort the remaining channels in the same tick or crash the summarizer's tick loop
- Provide a separate `send_error` path so that a broken prompt template results in an actionable email to the operator instead of a silently-missed digest

## Not in scope

- Match selection or summarization — the matcher and summarizer own those
- Retry / queue / dead-letter — `notification_log`'s `pending → sent / failed → dead` state machine is on the roadmap; today a failed send is logged and dropped
- Push channels other than email — Telegram / Discord / Slack channels are scaffolded by the marker ABC but not yet implemented
- Outbound rate limiting — a future `pre_push_hook` on the summarizer is the seam for that, not the channel itself

## Public interface

### Marker base (`base.py`)

```python
class BaseChannel(ABC):
    """Marker base. Concrete channels define their own send() signature."""
```

There is no abstract `send` because each channel takes a channel-specific config object whose shape would force the ABC into either `Any` or a generic protocol — both lose more typing power than they save. The dispatcher in `main.py` instead pattern-matches on the channel config type:

```python
for ch in intent.channels:
    if isinstance(ch, EmailChannelConfig):
        await email_ch.send(result, config=ch, ...)
```

A future Telegram channel adds its own `TelegramChannelConfig` type and its own `isinstance` arm; nothing about the existing channels needs to change.

### Email channel config (`email.py`)

```python
class EmailChannelConfig(BaseModel):
    type: Literal["email"] = "email"
    to: list[EmailStr]   = Field(min_length=1, max_length=50)
    cc: list[EmailStr]   = Field(default_factory=list, max_length=20)
    bcc: list[EmailStr]  = Field(default_factory=list, max_length=20)
```

`type` is a Pydantic discriminator value so `Intent.channels` (a `list[Annotated[..., Field(discriminator="type")]]`) can deserialize a heterogeneous list without ambiguity. RFC validation runs at the API boundary — by the time a config reaches `send` the addresses are already syntactically valid. The list bounds prevent fan-out abuse via a single intent.

### Email channel (`email.py`)

```python
class EmailChannel(BaseChannel):
    def __init__(self, settings: Settings) -> None: ...

    async def send(
        self,
        result: SummaryResult,
        *,
        config: EmailChannelConfig,
        intent_name: str,
        intent_timezone: str,
    ) -> None

    async def send_error(
        self,
        intent_name: str,
        kind: str,        # "system" | "instruction"
        name: str,        # template name that failed
        reason: str,      # exception message; first line shown in subject
        *,
        config: EmailChannelConfig,
    ) -> None
```

`send`:

1. If `settings.smtp_host` is empty, log a warning and return — this is the "SMTP not configured yet" path operators hit on first boot, and it must not crash the summarizer's tick
2. Pick the citation list — `result.citations` if populated, else `[result.primary, *result.other_sources]`, else empty (for the rare case where the LLM returned a summary with no cited articles)
3. Resolve `intent_timezone` to a `ZoneInfo`, falling back to UTC on `ZoneInfoNotFoundError` (logged warning)
4. Convert each citation's `published_at` to a display string in that timezone; format the matcher's `score` (if present) as a `0.NN` badge
5. Convert the LLM's Markdown summary to HTML, replacing `[N]` references with `<sup><a href="#cite-N">[N]</a></sup>` anchors. References outside `1..len(citations)` are silently dropped — LLMs occasionally hallucinate `[7]` in a 4-citation digest, and producing dead anchors hurts more than the missing reference
6. Render `templates/email_digest.html.jinja2` with autoescape on
7. Wrap the HTML in `multipart/related` with the brand logo attached as an inline image (`Content-ID: <sembr-logo>`) when the bundled logo file is present; degrade to a single-part `text/html` message otherwise
8. Build To / Cc headers from the config; **Bcc is not placed in headers** — only into the SMTP envelope (RCPT TO), so recipients cannot see who else is copied
9. Hand the message off to `smtplib` on a background thread via `asyncio.to_thread` (smtplib is sync; the event loop must not block on a slow MTA)

`send_error` follows the same shape but uses a different template (`email_template_error.html.jinja2`) and a different subject (`[sembr][error] {intent} — {kind} template '{name}' — {reason}`). It exists because a renamed or syntactically broken prompt template produces no digest at all — the operator needs an active alert, not silence on the next cron tick.

Both methods are wrapped in a top-level `try / except` that logs and swallows. The dispatcher in `main.py` independently logs and swallows; this is intentional defense-in-depth because either side suffering an exception silently is strictly worse than a duplicate log line.

### Templates

`templates/email_digest.html.jinja2` — the digest layout. Inline-styled in addition to a `<style>` block because Outlook and several webmail clients still strip `<style>` tags. Uses `multipart/related` rather than `multipart/alternative` so the SpamAssassin `MIME_HTML_ONLY` rule does not fire.

`templates/email_template_error.html.jinja2` — the operator-facing error layout, used by `send_error`. Renders the failed template kind, name, reason, and the resolved on-disk `prompts_dir` so the operator can find the file to fix.

`templates/assets/logo.png` — read once at module import. Missing or unreadable logo logs a warning and degrades the message to single-part HTML; sending continues.

## Configuration

| Field | Default | Notes |
|---|---|---|
| `smtp_host` | `""` | Empty disables email delivery — `send` becomes a no-op with a one-line warning. Set to e.g. `smtp.gmail.com` for Gmail, `smtp.sendgrid.net` for SendGrid |
| `smtp_port` | `587` | 587 for STARTTLS (the default), 465 for SMTP_SSL |
| `smtp_username` | `""` | SMTP auth username; leave empty to skip `AUTH` |
| `smtp_password` | `""` (SecretStr) | SMTP auth password; never logged or echoed |
| `smtp_from` | `""` | `From:` header. Falls back to `smtp_username` when empty |
| `smtp_use_starttls` | `True` | Run `STARTTLS` after connecting on plain SMTP |
| `smtp_use_ssl` | `False` | Use `SMTP_SSL` directly (port 465 style). When `True`, `smtp_use_starttls` is ignored |
| `display_timezone` | `Asia/Shanghai` | Server-wide default timezone — surfaced to the dashboard. **Not** consulted for email rendering: the per-intent timezone is used. Kept for cross-channel UI consistency |

The path string surfaced inside the `send_error` body (`/app/prompts`) is read directly from the module-level `sembr.summarizer.templates.PROMPTS_DIR` constant — not from a Settings field. The legacy `Settings.prompts_dir` was removed in the template-management refactor.

The per-intent timezone lives on the `Intent` row (`intents.timezone`, schema default `'UTC'`). The dispatcher in `main.py` reads it at send time and threads it through `EmailChannel.send(intent_timezone=...)`.

## Upstream dependencies

- `config.Settings` — SMTP host / port / credentials / TLS flags
- `sembr.summarizer.templates.PROMPTS_DIR` — path string surfaced in the operator-facing error email
- `summarizer.models.SummaryResult` — input to `send`; `Citation.score` and `Citation.published_at` drive the per-source line in the rendered HTML
- `db.intents.Intent` — `name`, `channels`, `timezone` are read by the dispatcher in `main.py` and passed to `send` per call

## Downstream consumers

- The dispatcher in `main.py` is the only caller of `send` and `send_error`. It is registered as the summarizer's `on_summary` and `on_template_error` callbacks via the lifespan setup
- Final delivery is handled by `smtplib` in a worker thread; failures bubble up through the channel's exception handler

## Known constraints

- **No retries / no DLQ today**: a failed `smtplib` call is logged and the result is dropped. The `notification_log` table that the schema reserves for `pending → sent / failed → dead` state is not yet wired through this module. Cron-driven intents pick up missed deliveries on the next tick by virtue of the lookback window; event-driven intents lose the buffered tick on send failure
- **HTML-only message body**: there is no `text/plain` alternative. Plain-text-only mail clients render the raw HTML markup. Modern clients are exclusively HTML, so the trade-off is acceptable, but a future change should ship a `multipart/alternative` shell with a Markdown-source plain-text part for compliance and accessibility
- **Synchronous SMTP via `asyncio.to_thread`**: a slow or hung MTA holds a thread for the duration of the SMTP exchange. At 1.0 fan-out (a handful of intents firing per minute) this is fine. A high-fan-out deployment should swap `smtplib` for an async SMTP library or a queue-backed sender
- **Single-channel today**: only `EmailChannelConfig` is recognized by the dispatcher. The `BaseChannel` ABC and the per-channel config pattern were chosen so that adding a `TelegramChannelConfig` + a `TelegramChannel` only requires (a) a new `isinstance` arm in the dispatcher and (b) a new entry in the `Intent.channels` discriminated union — but the work has not happened yet
- **Logo bytes loaded at import time**: a few hundred KB held in memory for the lifetime of the process. Cheap and avoids re-reading per send; replace with a per-channel cache only if the deployment ships many large brand assets
- **Citation score is a cosine similarity, not a probability**: the badge displays the matcher's raw similarity score (typically 0.60–0.95 with the bundled embedder). Users new to ANN search may misread it as a confidence percentage. Document the scale in operator-facing docs, not in the email itself — header-level captioning would clutter the digest
