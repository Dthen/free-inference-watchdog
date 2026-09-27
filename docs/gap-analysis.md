# Gateway Metadata Gap Analysis

Point-in-time survey of what the gateway `/v1/models` endpoints return that the
watchdog fetches and then discards. Source: `gap_analysis.json` (2026-09-13).

**Why this lives here:** the watchdog stores the full raw model object per id in
`roster.json` under `provider_models` — the data is already collected. What is
missing is the *rendering*, so this is a UI backlog, not a data-collection problem.

## Status as of 2026-09-27

| Field | Stored in roster | Rendered on dashboard |
|---|---|---|
| context_length | yes | yes — hover tooltip |
| input_modalities | yes | yes — hover tooltip |
| description | yes | no |
| supported_parameters | yes | no |
| max_completion_tokens | yes — nested under `top_provider` | no |
| benchmarks (OpenRouter) | yes | no |
| knowledge_cutoff | yes | no |
| created | yes | no |

Verified against the live `state/roster.json` on 2026-09-27: every row above is
backed by a real key on a real model object. Note `max_completion_tokens` is never
top-level — it is nested (`top_provider.max_completion_tokens`, present on all 44
currently-tracked models), so a consumer must reach through `top_provider` for it.
`top_provider` also carries `context_length` and `is_moderated`, neither rendered.

## Full findings

### context_length
- **Gateways:** nous, openrouter, kilo, tokenrouter, amd, nim
- **Example:** `262144`
- **Impact:** HIGH — Users cannot know model context windows. Hermes Agent documentation explicitly lists endpoint /models as a context_length discovery source. This is the single most valuable metadata field for model selection.

### name
- **Gateways:** nous, openrouter, kilo, amd, nim
- **Example:** `Union Alpha`
- **Impact:** MEDIUM — Display name is never exposed; users only see opaque IDs. The site renders raw IDs as row labels.

### description
- **Gateways:** nous, openrouter, kilo
- **Example:** `Union Alpha is a multimodal model built for research, coding, and agentic workflows...`
- **Impact:** MEDIUM — No way to discover model capabilities without looking up the ID externally.

### architecture.modality
- **Gateways:** nous, openrouter, kilo
- **Example:** `text+image->text`
- **Impact:** HIGH — Cannot distinguish text-only from multimodal models. Critical for agent/tool routing.

### architecture.input_modalities
- **Gateways:** nous, openrouter, kilo
- **Example:** `["text", "image"]`
- **Impact:** HIGH — No way to know which models accept image/file/audio inputs.

### architecture.output_modalities
- **Gateways:** nous, openrouter, kilo
- **Example:** `["text"]`
- **Impact:** MEDIUM — Cannot identify models that output images or audio.

### architecture.tokenizer
- **Gateways:** nous, openrouter, kilo
- **Example:** `GPT`
- **Impact:** LOW — tokenizer family affects token counting accuracy for client-side estimation.

### architecture.instruct_type
- **Gateways:** nous, openrouter, kilo
- **Example:** `chatml`
- **Impact:** MEDIUM — affects prompt formatting requirements.

### pricing (full object beyond prompt/completion==0 check)
- **Gateways:** nous, openrouter, kilo
- **Example:** `{"prompt": "0.0000025", "completion": "0.00001", "input_cache_read": "0.00000025", "request": "0"}`
- **Impact:** HIGH — Cached pricing, image pricing, request pricing, web search pricing all discarded. Free-tier users could optimize costs if they knew per-request or image costs.

### pricing.overrides
- **Gateways:** nous, openrouter
- **Example:** `[{"utc_days": ["monday"...], "utc_start": 30, "prompt": "0.00000056"}]`
- **Impact:** MEDIUM — Time-of-day pricing variants are discarded; users cannot optimize for cheaper off-peak pricing.

### top_provider.context_length
- **Gateways:** nous, openrouter, kilo
- **Example:** `262144`
- **Impact:** MEDIUM — Effective context at the serving provider may differ from model's native context.

### top_provider.max_completion_tokens
- **Gateways:** nous, openrouter, kilo
- **Example:** `131072`
- **Impact:** HIGH — Users cannot know max output length; may hit truncation silently.

### top_provider.is_moderated
- **Gateways:** nous, openrouter, kilo
- **Example:** `false`
- **Impact:** LOW — Content moderation status; useful for filtering.

### created
- **Gateways:** nous, openrouter, kilo, tokenrouter, amd, nim
- **Example:** `1789569723`
- **Impact:** LOW — Cannot sort models by age/recency or identify newly released free models.

### canonical_slug
- **Gateways:** nous, openrouter, kilo
- **Example:** `stealth/union-alpha`
- **Impact:** LOW — Stable identifier for cross-referencing models.

### hugging_face_id
- **Gateways:** nous, openrouter, kilo
- **Example:** `None`
- **Impact:** MEDIUM — Links free-tier models to their upstream open-source counterparts.

### expiration_date
- **Gateways:** nous, openrouter, kilo
- **Example:** `2098-12-31`
- **Impact:** HIGH — Cannot warn users that a free model is expiring soon.

### knowledge_cutoff
- **Gateways:** nous, openrouter
- **Example:** `None`
- **Impact:** MEDIUM — Cannot filter models by knowledge freshness.

### per_request_limits
- **Gateways:** nous, openrouter, kilo
- **Example:** `None`
- **Impact:** HIGH — Rate limit info per request is discarded; users must discover limits by trial and error.

### supported_parameters
- **Gateways:** nous, openrouter, kilo
- **Example:** `["max_tokens", "response_format", "temperature", "tool_choice", "tools", "top_p"]`
- **Impact:** HIGH — Cannot know which API parameters are supported (e.g., structured outputs, reasoning, tool use) without trial and error.

### default_parameters
- **Gateways:** nous, openrouter, kilo
- **Example:** `{"temperature": 1, "top_p": 1}`
- **Impact:** MEDIUM — Cannot know recommended temperature/top_p defaults without inspection.

### reasoning
- **Gateways:** nous, openrouter
- **Example:** `{"mandatory": false, "supported_efforts": ["max", "high", "low"], "default_effort": "high"}`
- **Impact:** HIGH — Cannot identify reasoning models or their supported effort levels.

### synthesizedFreeVariant
- **Gateways:** nous
- **Example:** `None`
- **Impact:** LOW — Indicates if a free variant was synthesized.

### aliases
- **Gateways:** nous
- **Example:** `["stealth/union-alpha"]`
- **Impact:** LOW — Alternative names for the same model.

### supported_voices
- **Gateways:** nous, openrouter
- **Example:** `None`
- **Impact:** LOW — TTS voice support indicator.

### links.details
- **Gateways:** nous, openrouter
- **Example:** `/api/v1/models/stealth/union-alpha/endpoints`
- **Impact:** LOW — Direct link to model details endpoint.

### alias_target
- **Gateways:** openrouter
- **Example:** `{"name": "Union Alpha", "slug": "stealth/union-alpha"}`
- **Impact:** LOW — Indicates if this model is an alias for another.

### benchmarks
- **Gateways:** openrouter
- **Example:** `{"artificial_analysis": {"agentic_index": 74.5}, "design_arena": {"elo": 1250}}`
- **Impact:** HIGH — Quality/performance benchmarks are discarded; users cannot compare free models by capability.

### isFree
- **Gateways:** kilo
- **Example:** `true`
- **Impact:** LOW — Already used by detection, but not stored/exposed for inspection.

### mayTrainOnYourPrompts
- **Gateways:** kilo
- **Example:** `false`
- **Impact:** MEDIUM — Privacy signal; users may want to filter out models that train on prompts.

### preferredIndex
- **Gateways:** kilo
- **Example:** `0`
- **Impact:** LOW — Routing priority hint.

### autoRouting.models
- **Gateways:** kilo
- **Example:** `["anthropic/claude-sonnet-5", "deepseek/deepseek-v4.1-flash", ...]`
- **Impact:** MEDIUM — Auto-router model lists are discarded; users cannot know which underlying models a router may use.

### enkrypt
- **Gateways:** kilo
- **Example:** `{"risk_score": 12, "jailbreak_score": 5, "source": "kilo"}`
- **Impact:** MEDIUM — Security scan scores (OWASP, NIST, toxicity) discarded; users cannot filter by safety.

### opencode.variants
- **Gateways:** kilo
- **Example:** `{"high": {"reasoning": {"effort": "high", "enabled": true}, "verbosity": "high"}}`
- **Impact:** LOW — Reasoning/verbosity variant configs discarded.

### terminalBench
- **Gateways:** kilo
- **Example:** `{"avgAttemptCostUsd": 0.02, "overallScore": 1450}`
- **Impact:** MEDIUM — Benchmark cost/score data discarded.

### owned_by
- **Gateways:** nim, tokenrouter, amd
- **Example:** `01-ai`
- **Impact:** LOW — Model owner/creator; useful for filtering or attribution.

### object
- **Gateways:** nim, tokenrouter, amd
- **Example:** `model`
- **Impact:** NEGLIGIBLE — Always 'model' in OpenAI-compatible APIs.

## Open site gaps (original list)

- context_length not displayed — users cannot compare model context windows
- architecture/modality not shown — no indication of multimodal vs text-only
- description not rendered — model purpose/capabilities invisible
- created date not shown — cannot identify new releases
- expiration_date not displayed — no warning before free tier expires
- supported_parameters not surfaced — cannot know if model supports tools/structured outputs/reasoning
- max_completion_tokens (top_provider) not shown — truncation risk invisible
- benchmarks not displayed (OpenRouter) — no quality comparison across free models
- reasoning support not shown — cannot identify reasoning-capable models
- enkrypt security scores not shown (Kilo) — safety signals discarded
- no model detail link — cannot navigate to provider's model page directly
- no owned_by/creator column — cannot filter by upstream model creator
- per_request_limits not shown — rate limit info from API discarded
- no pricing granularity — only free vs not-free; no cached-token or per-request cost info
- no recency indicator — cannot see which models were newly added this tick
- gateway column shows only presence dot, not provider-specific metadata (e.g., is_moderated, top_provider context)
- no filtering capability — cannot filter by context_length, modality, or capabilities

## Original summary

> Complete gap analysis of all 7 gateway APIs (Nous, OpenRouter, Kilo, TokenRouter, AMD, B.AI, NVIDIA NIM) against what the watchdog stores and exposes. The watchdog fetches rich model metadata from each gateway's /v1/models endpoint but discards everything except the model `id` field. The roster.json stores only {provider: [ids]} plus tick_epoch, stale_providers, transients, unconfirmed, and ratelimits (response headers, not model metadata). All four MCP tools (list_free_models, get_model, watchdog_status, list_endpoints) surface only IDs and wiring info (chat_completions_url, api_type from config). The static site renders a presence matrix with IDs, dots, and wiring URLs — no per-model metadata. Lost fields total 37+ across gateways. Critical losses include: context_length (6 gateways), architecture/modality (3), description (3), max_completion_tokens (3), supported_parameters (3), pricing sub-fields like cached-token and request pricing (3), per_request_limits (3), expiration_date (3), reasoning support (2), benchmarks (OpenRouter), security scores (Kilo), and knowledge_cutoff (2). The most impactful gap is context_length — explicitly listed by Hermes Agent docs as a /models discovery source — followed by supported_parameters (tool/structured-output capability), max_completion_tokens (truncation avoidance), and expiration_date (free-tier sunset warnings). The problem extends well beyond context_length: the entire model metadata surface is being thrown away at the fetch_provider boundary in providers.py:_extract_ids, which coerces all items to their `id` string and drops everything else.
