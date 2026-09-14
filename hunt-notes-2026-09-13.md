# Free-Inference Hunt Notes — 2026-09-13

> Point-in-time field notes from the 2026-09-13 hunt. The roster since then
> is providers/*.json — names below may be retired or not yet added.

THE PROCESS (Dthen's own words, 2026-09-13, after Kimbo went off-piste twice):
- Starting point: search NEWISH OPEN-WEIGHT MODEL NAMES + "free" in a bunch of places (web, redlib/Reddit).
- Deliberately AVOID the big GitHub repo lists — they track steady-state free tiers, not what we hunt.
- Target: PROMO OFFERS — gateways running a model free-for-a-while as a LOSS LEADER.
- Per hunt: this file is the record of what we did and what we found out.

Already held (chain, as of today): tokenrouter default (z-ai/glm-5.3-free) → bai (qwen3.8-flash) → amd (DeepSeek-V4-Flash) → nous (meituan/longcat-2.0:free) → nvidia (nemotron-3-ultra) → kilocode (nemotron-3-ultra-550b-a55b:free). Watchdog watches nous/kilo/openrouter/zen/cline/command_code + config providers.

## Round 1 — newish open-weight model names × "free"

Models freshly released (target promo bait, per this week's releases):
- DeepSeek V4.1 Flash (GA ~this week)
- Qwen3.8 family (Max GA Sept 2, Flash/Flash-Next recent)
- Hunyuan HY3 (Tencent, 2026)
- Kimi K3
- GLM 4.7 flash variants
- (already-held: qwen3.8-flash on bai, longcat-2.0, nemotron-3.5/3 ultra)

### Web leads
- **Dthen's earlier finds:** b.ai + tokenrouter (both wired already)
- **Verdent.ai** (from 2026-09-09 session, never settled): advertises free DeepSeek V4 Flash + GLM 5.3 Flash, no payment. UNRESOLVED.
- **Krater.ai** (from 2026-09-09 session, never settled): DeepSeek V4 Flash free tier. UNRESOLVED.
- YouTube "Code With Yousaf" (11 Sep 2026): "DeepSeek V4.1 Flash FREE for 2 Weeks — Unlimited Credits" — presumably a gateway promo; video transcript/endpoint not yet chased.
- **AIHubMix**: "coding-kimi-k3-free" — 5 RPM / 500 req/day / 1M tokens/day per account. OpenAI-compatible. Worth a listing probe.
- lmspeed.net/free/qwen — aggregator page of free qwen models; not chased yet.
- flaq.ai/models/alibaba/qwen-3-8-max — "Free Alibaba Qwen 3.8 Max API" — not chased yet.
- YouTube: "Unlimited Free Kimi K3 + Claude Code" via NVIDIA — that's just NIM (held, always-free).
- Qwen 3.8 free no-key endpoints from YouTube videos (lmspeed-type) — likely gray/no-key endpoints, low trust.

### Redlib leads
(pending round 2)

## Off-piste stuff binned today (do not redo)
- OmniRoute gateways.ts census crawl + 15 raw /v1/models probes (60 free-flagged gateways parsed) — the census angle, NOT the hunt process.
- Canonical-memory "playbook" attempt — wrong home; retired.
