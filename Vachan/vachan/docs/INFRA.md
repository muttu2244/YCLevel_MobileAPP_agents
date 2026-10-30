# What infrastructure Vachan needs

Short version: **to run it today, nothing.** To run it for real, one free VM, one
free Postgres, one model endpoint, and a scheduler. The only thing you cannot do
for free is WhatsApp itself.

---

## 1. To run it on your machine today — nothing at all

| Path | Needs | Command |
|---|---|---|
| **Browser app** | any browser | open `vachan/app/index.html` |
| **Python pipeline** | Python 3.10+ (standard library only) | `cd vachan/agent && python -m vachan demo` |
| **Local model** (optional) | [Ollama](https://ollama.com) | `ollama pull qwen2.5:7b` then `VACHAN_LLM_MODE=ollama` |

No Docker, no account, no API key, no network. The browser app and the Python
CLI run the same pipeline: extract → close the loop → draft follow-ups → score.
Mock mode is a real rule engine, not a stub — the point is that the whole thing
works with zero spend, and the model is a plug-in upgrade.

## 2. To run it for real, on free tiers

| Need | Free option | Why this one |
|---|---|---|
| **Host / always-on worker** | Oracle Cloud **Always Free** ARM VM (up to 4 OCPU / 24 GB) | The follow-up scheduler must wake up every morning. A free VM with a systemd timer is the least moving parts. Cloud Run also works but needs Cloud Scheduler to fire it. |
| **Database** | **Supabase** or **Neon** free Postgres (~0.5 GB) | `db/schema.sql` is portable and runs unchanged on both. Supabase also needs `db/supabase_rls.sql` for tenancy — see [SUPABASE.md](SUPABASE.md). SQLite is fine until you have a second user. Note that a Supabase free project pauses after 7 days with no activity. |
| **Model** | **Ollama** on the VM (free forever), or **Groq / GitHub Models / Gemini** free tiers | Ollama for volume and privacy. A free API tier for quality on the harder extractions. Both are already wired in `llm.py`. |
| **Voice notes** | **whisper.cpp** / faster-whisper on the VM | Local transcription, no per-minute cost. A 7B whisper on 4 ARM cores is fine for 20-second site voice notes. |
| **Scheduler** | systemd timer (VM) or GitHub Actions cron | "Every morning at 10:00, chase what slipped." It runs `python -m vachan sweep`: age what never arrived, draft the chases, rescore. GitHub Actions cron is free and needs no server. |
| **Tracing / evals** | **Langfuse** free tier (or self-host) | You need to see what the extractor got wrong. Non-negotiable for prompt iteration. |
| **CI** | **GitHub Actions** | Free minutes; run `python -m unittest discover -s tests` and `ruff` on every push. Both are declared in `pyproject.toml` under the `dev` extra. |
| **TLS + domain** | **Caddy** (automatic Let's Encrypt) or Cloudflare Tunnel | Two lines of config, no certificate work. |
| **Intake (phase 1)** | **Telegram bot** via @BotFather | Free, instant webhooks, no business verification. This is the prototype channel from the plan. |
| **Secrets** | `.env` locally, host secret store in prod | `.env` is git-ignored; `.env.example` documents every key. |

That column adds up to **₹0/month** for everything except WhatsApp messaging.

## 3. WhatsApp: the Groups API verdict (checked against Meta's own docs, Oct 2026)

Your instinct was right, and the situation has changed in a useful way.

**Confirmed: Vachan cannot silently read an existing personal group.** Meta's
Groups API groups are *created by the business number* and are invite-only;
participants join through an invite link and a join request the business
approves. There is no endpoint to add a participant directly. Nothing in the API
lets a bot attach itself to a group your vendors already chat in.

What the Groups API actually is:

| | |
|---|---|
| **Eligibility** | Official Business Account (OBA) + live on the Cloud API + `whatsapp_business_messaging` permission. Number must not be on the WhatsApp Business *app*, and must not be in Multi-solution Conversations. |
| **Group size** | 8 participants max — and the business number occupies one slot, so 7 humans per group. |
| **Scale** | Up to 10,000 groups per business number; only one Cloud API business per group. |
| **Supported** | Create / list / update / delete groups, invite links, join requests, remove participants, send text + media + templates, **receive group messages via webhooks**. |
| **Not supported** | Adding a participant directly, calling, disappearing messages, view-once, editing or deleting a sent message, hiding the participant list. |
| **Pricing** | Per-message. Group messaging is **not** on the free tier. |

**What this means for the build.**

- **Phase 1 (now, free)** — the workaround in your plan is still the right
  first move, and the local app already implements it: paste or upload a
  WhatsApp export, forward a thread, drop in a voice-note transcript, or use the
  Telegram bot. This needs no OBA, no review, and no per-message fees.
- **Phase 2 (after you have an OBA)** — create a Vachan group *per project*,
  invite the owner and that project's vendors, and receive group messages by
  webhook. Seven human seats is genuinely enough for one site. This turns Vachan
  from "a tool the owner feeds" into "a member of the site group", which is the
  product you actually described.
- **Voice on the API path** — Meta's groups doc lists the unsupported message
  types as calling, disappearing, view-once, auth, commerce and interactive; it
  does not list audio as unsupported. Confirm audio messages against the groups
  messaging reference before you promise voice extraction on the API path. The
  local transcript path always works regardless.

Sources: [Groups API overview](https://developers.facebook.com/documentation/business-messaging/whatsapp/groups)
and the [endpoint-by-endpoint breakdown](https://www.unipile.com/whatsapp-group-api/)
(cross-checked August 2026). Re-read the Groups API pricing page before you
commit to anything financial.

## 4. Things that are genuinely not free

1. **WhatsApp itself** — per-message pricing once you move past the free service
   window.
2. **OBA verification** — becoming an Official Business Account means real
   business registration and Meta review. Plan for weeks, not hours.
3. **Free-tier ceilings** — Neon/Supabase storage, Groq/Gemini rate limits,
   Oracle's idle-reclamation policy for Always Free resources. Fine for one
   pilot business, not for a hundred.
4. **Your time.** The expensive part of this project is prompt calibration and
   the vendor-relationship judgment calls, not compute.

## 5. Still to verify (I did not verify these)

- **Free-tier limits change constantly.** Check each provider's current pricing
  page before you design around a number in this file. Nothing here should be
  treated as a contract.
- **The competitor scan in section 2 of your plan.** I did not run it, so I
  cannot tell you Vachan is novel. That is the check to do before committing,
  and the honest answer may be "the promise-extraction part exists; the vendor
  reliability ledger does not".
- **Production WhatsApp throughput and template approval** for the 1:1 path.

## 6. The minimum viable deployment

If you want the smallest thing that is genuinely useful:

```
Oracle Always Free ARM VM
  ├── Caddy            (TLS, reverse proxy)
  ├── systemd service  vachan-webhook   (Telegram intake, FastAPI)
  ├── systemd timer    vachan-morning   (python -m vachan sweep, 10:00 local)
  └── Ollama + whisper.cpp              (extraction + transcription)
Neon free Postgres    (the ledger)
Langfuse free tier    (agent traces)
GitHub Actions        (tests on push)
Telegram bot          (the intake channel the owner actually uses)
```

One VM, one database, one bot. Everything above it in this document is
optimisation.
