# Vachan — वचन

A promise ledger for small service businesses. It reads the messy WhatsApp
conversation between an interior designer and their carpenters, electricians and
clients, writes down **who promised what, by when**, drafts the follow-up the
owner forgets to send, closes the loop when a photo proves it happened, and turns
that history into a **reliability score per vendor**.

Everything in this repo runs locally, with no cloud account, no API key and no
install.

---

## Run it now

**1. The browser app — zero install, works offline**

Open [`app/index.html`](app/index.html) in any browser. It loads a sample
interior-design business and runs the whole pipeline live. Paste your own chat,
a forwarded message or a voice-note transcript into **Inbox** to see the
extractor work on your data.

No server, no build step, no `npm install`. This is the path that works even when
your toolchain doesn't — and it can check itself: **Agent team → Run the test
suite** executes 27 assertions against throwaway data and reports pass/fail in
the page.

**2. The Python pipeline — standard library only**

```bash
cd agent
python -m vachan demo            # seed the sample and run everything
python -m vachan ingest "~/Downloads/WhatsApp Chat.txt"
python -m vachan sweep           # what the morning cron runs
python -m vachan scores
python -m vachan init-db
```

Python 3.10+. No `pip install` — `pyproject.toml` declares zero required
dependencies on purpose. Optional extras add Postgres, Telegram and Langfuse.

**3. Against a real model**

```bash
cp .env.example .env
# VACHAN_LLM_MODE=ollama   + ollama pull qwen2.5:7b      (free, local)
# VACHAN_LLM_MODE=openai   + a Groq/GitHub Models/Gemini key
cd agent && python -m vachan demo
```

Mock mode is not a stub: it is a deterministic rule engine that emits the same
JSON the prompt asks a model for, so the pipeline, the demo and the test suite
all run with nothing else running.

## What's in here

```
app/index.html          zero-install local app (browser, localStorage)
agent/vachan/           the pipeline — config, llm, store, agents, cli
agent/vachan/agents.py  extractor · chaser · closer · scorer + rule fallback
agent/tests/            the pipeline tests (stdlib unittest, throwaway SQLite)
db/schema.sql           the ledger — portable across Postgres and SQLite
db/supabase_rls.sql     tenancy + row-level security for the hosted copy
prompts/                the four agent prompts, written to be read by humans
samples/villa42-week.txt a realistic Hinglish week on site
docs/INFRA.md           what infrastructure you need, and what WhatsApp allows
.env.example            every setting, with the free option as the default
```

## The agent team

| Agent | Prompt | Job |
|---|---|---|
| **Extractor** | [`01_extract_promises.md`](prompts/01_extract_promises.md) | Chat or voice transcript → `{who, what, by when}` as strict JSON. Handles Hinglish time ("kal shaam tak", "parso", "Monday tak", "is hafte"). |
| **Chaser** | [`02_follow_up.md`](prompts/02_follow_up.md) | Drafts short follow-ups in the owner's voice. Tone escalates per attempt; one ask per message; never sent without approval. |
| **Closer** | [`03_close_loop.md`](prompts/03_close_loop.md) | Decides whether a photo, receipt or message actually proves the promise was kept — and flags gaming (reused photos, claims with nothing behind them). |
| **Scorer** | [`04_reliability.md`](prompts/04_reliability.md) | A versioned, deterministic formula. Small samples are smoothed, unmeasured dimensions are renormalised away, and the number is never invented by a model. |

## How a promise flows

```
ingest ──▶ extract ──▶ validate & dedupe ──▶ promises (open)
                              │
              due date passes ├──▶ chaser drafts follow-up ──▶ owner sends
                              │
          photo / "done" arrives └──▶ closer verifies ──▶ proof ──▶ done | late
                                                                      │
                                              nightly ──▶ scorer ──▶ reliability
```

Design rules baked into the code, not just the docs:

- **Never chase a promise nobody made.** No date promised → the promise is
  marked `needs_clarification` and the owner is asked, not the vendor.
- **Never close a promise with its own words.** A message that *created* a
  promise can't also be the proof that it was kept.
- **Short answers inherit their subject.** "aaj shaam tak bhej dunga" is
  meaningless alone; the object and category come from the message above it.
- **Draft first, send second.** Auto-send is a per-project opt-in, never the
  default.
- **Don't score what you never measured.** No follow-ups sent yet → the
  responsiveness term is dropped and the remaining weights renormalised, rather
  than silently scored 0.

## Data

`db/schema.sql` is one file that runs on both engines. It holds the ledger:
businesses, contacts, vendors, projects, conversations, messages, **promises**,
promise_events, follow_ups, proofs, proof_flags, vendor_score_snapshots,
agent_runs, consents — plus two read-model views.

The browser app persists to `localStorage` and can export SQL for its current
state (**Database** tab), which is the quickest way to see how the UI maps onto
the real tables.

It can also use a **Supabase project as the shared copy** (Database → Shared
database): the same rows, over PostgREST, with the anon key plus Row Level
Security doing the access control. `db/supabase_rls.sql` holds the tenancy model,
and [docs/SUPABASE.md](docs/SUPABASE.md) covers setup, the free-tier pause that
will bite a quiet ledger, and the pooler trap that breaks `psycopg` prepared
statements. Nothing about the local build changes if you never connect one.

## Verification

The browser build carries its own test suite: **Agent team → Run the test
suite**. It covers Hinglish date parsing, the extractor's rules, closing,
de-duplication, aging, the chasing loop, the score formula and the data-safety
paths (migration, import validation, SQL export). Each case runs against
throwaway state and the runner restores yours afterwards, so it is safe on real
data. It exists because this project is often used on a machine with no working
shell, where "run the tests" is not an option.

The Python suite is the same idea against the real schema:

```bash
cd agent && python -m unittest discover -s tests -v
```

Both are standard library only. The browser suite has been run and passes
**33/33**, including six cases for the Supabase layer: rows carry the business
they are pushed for, two businesses never collide on a primary key, a promise
survives a round trip through a database row, a push writes parents before
children, a pull takes newer rows and keeps yours on a tie, and no token can
reach a backup.

The Python suite has **not** been executed on this machine — there is no working
shell here — so treat it as written-but-unproven until you run it once. That
distinction is deliberate: unrun code in this repo has already contained a real
crash (a dict bound into a TEXT column) and a silently broken join
(`sender_contact_id` vs `contact_id`), both fixed above.

## Status

**Working, and exercised:** extraction with Hinglish time parsing and context
inheritance, dedupe with date backfill (answering "kis date tak?" closes the
loop instead of creating a duplicate), proof-based closing, aging that marks a
promise missed without pretending it was delivered, follow-up drafting with
escalation that keeps chasing the oldest promises, the Laplace-smoothed
reliability score with verified-proof gating and one snapshot per vendor per
day, PostgreSQL + SQLite schema, WhatsApp export parsing, JSON backup/restore
with validation, complete SQL export, a promise-event audit trail, and a
searchable ledger with reopen / mark-missed / set-a-date actions.

**Shared database:** a Supabase project can hold the same ledger, pushed and
pulled from the app (see [docs/SUPABASE.md](docs/SUPABASE.md)). The request layer
and the merge rule are exercised in the suite; no request has been made to a live
project from this machine, and `db/supabase_rls.sql` has not been run by `psql`.

**Deliberately not built yet:** Telegram bot wiring (it's the intake adapter in
`docs/INFRA.md`, not code), the WhatsApp Groups API path (needs an Official
Business Account — see `docs/INFRA.md` §3), voice transcription (feed the
transcript in; whisper.cpp is the intended local transcriber), and the
cross-business score-sharing network.

**Not verified:** the novelty/competitor scan from section 2 of the plan. Until
that is done, treat the product thesis as untested.
