-- ============================================================================
-- Vachan — promise & reliability ledger
-- Portable schema: runs unchanged on PostgreSQL 14+ and SQLite 3.35+
--
-- Deliberate portability choices (so the same file boots your laptop and prod):
--   * TEXT primary keys holding UUIDs — no SERIAL / AUTOINCREMENT
--   * TEXT timestamps in ISO-8601 UTC (the app writes them; no DB defaults)
--   * TEXT/REAL numbers, TEXT JSON blobs — no jsonb, no arrays
-- Postgres nicety left commented at the bottom.
--
-- Run it:  psql  "$DATABASE_URL" -f db/schema.sql
--          sqlite3 ./vachan.db < db/schema.sql
-- ============================================================================

-- SQLite enforces foreign keys per connection, so the app runs
-- `PRAGMA foreign_keys = ON` after opening. Postgres does it already, and the
-- PRAGMA statement itself is a syntax error there — hence no dialect-specific
-- SQL in this file.

-- ---------------------------------------------------------------------------
-- Tenancy: one row per small business using Vachan.
-- ---------------------------------------------------------------------------
CREATE TABLE businesses (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  owner_name    TEXT,
  owner_phone   TEXT,
  trade         TEXT,                 -- interior | contractor | events | other
  timezone      TEXT NOT NULL DEFAULT 'Asia/Kolkata',
  locale        TEXT NOT NULL DEFAULT 'en-IN',
  follow_up_hour INTEGER NOT NULL DEFAULT 10,   -- when to nudge, local time
  created_at    TEXT NOT NULL
);

CREATE TABLE users (
  id           TEXT PRIMARY KEY,
  business_id  TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  name         TEXT,
  phone        TEXT,
  email        TEXT,
  role         TEXT NOT NULL DEFAULT 'owner',  -- owner | staff
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_users_business ON users(business_id);

-- ---------------------------------------------------------------------------
-- Everyone the business talks to: clients, vendors, the owner, unknowns.
-- Phones are stored normalised to E.164 (e.g. +919876543210) when we know them.
-- ---------------------------------------------------------------------------
CREATE TABLE contacts (
  id           TEXT PRIMARY KEY,
  business_id  TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  name         TEXT,
  phone        TEXT,
  kind         TEXT NOT NULL DEFAULT 'unknown',  -- client|vendor|owner|unknown
  company      TEXT,
  notes        TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE (business_id, phone)
);
CREATE INDEX idx_contacts_business_kind ON contacts(business_id, kind);

-- A vendor is a contact with a trade, plus how they prefer to be reached.
CREATE TABLE vendors (
  id                 TEXT PRIMARY KEY,
  business_id        TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  contact_id         TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  trade              TEXT,               -- carpenter|painter|electrician|tiles|ply|fabricator|plumber|other
  reliability        REAL,               -- cached 0-100, recomputed into vendor_score_snapshots
  reliability_band   TEXT,               -- strong|ok|watch|risk
  active             INTEGER NOT NULL DEFAULT 1,
  created_at         TEXT NOT NULL,
  UNIQUE (business_id, contact_id)
);
CREATE INDEX idx_vendors_contact ON vendors(contact_id);

-- ---------------------------------------------------------------------------
-- A job: what the promises are actually about (Kitchen for Villa 42 ...).
-- ---------------------------------------------------------------------------
CREATE TABLE projects (
  id                TEXT PRIMARY KEY,
  business_id       TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  name              TEXT NOT NULL,
  client_contact_id TEXT REFERENCES contacts(id) ON DELETE SET NULL,
  site_address      TEXT,
  status            TEXT NOT NULL DEFAULT 'active',  -- active|on_hold|closed
  starts_on         TEXT,
  due_on            TEXT,
  created_at        TEXT NOT NULL
);
CREATE INDEX idx_projects_business_status ON projects(business_id, status);

-- ---------------------------------------------------------------------------
-- Conversations. Vachan never silently reads a personal WhatsApp group; a
-- conversation row is created from whatever the owner *gives* us:
--   source='forward'  a message forwarded into the Vachan chat
--   source='export'   an uploaded WhatsApp .txt export
--   source='telegram' the prototype bot
--   source='api'      WhatsApp Cloud API number we own
-- ---------------------------------------------------------------------------
CREATE TABLE conversations (
  id          TEXT PRIMARY KEY,
  business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  project_id  TEXT REFERENCES projects(id) ON DELETE SET NULL,
  channel     TEXT NOT NULL,            -- whatsapp|telegram|manual
  kind        TEXT NOT NULL DEFAULT 'dm', -- dm | group | self
  external_id TEXT,                     -- chat jid / telegram chat id
  title       TEXT,
  source      TEXT NOT NULL,            -- api|forward|export|telegram|manual
  created_at  TEXT NOT NULL,
  UNIQUE (business_id, channel, external_id)
);
CREATE INDEX idx_conversations_project ON conversations(project_id);

CREATE TABLE conversation_participants (
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  contact_id      TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  role_in_chat    TEXT,                 -- client|vendor|owner
  PRIMARY KEY (conversation_id, contact_id)
);

-- ---------------------------------------------------------------------------
-- Raw messages. Voice notes are stored as media + a transcript; the transcript
-- is what the extractor actually reads, so a whisper-based local transcriber
-- and a paid one are interchangeable.
-- ---------------------------------------------------------------------------
CREATE TABLE messages (
  id                TEXT PRIMARY KEY,
  conversation_id   TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_contact_id TEXT REFERENCES contacts(id) ON DELETE SET NULL,
  sender_raw_name   TEXT,               -- as it appeared in the chat
  direction         TEXT NOT NULL,      -- inbound | outbound
  kind              TEXT NOT NULL,      -- text | voice | image | document | system
  body              TEXT,
  transcript        TEXT,               -- for voice notes
  media_path        TEXT,
  duration_seconds  INTEGER,
  sent_at           TEXT NOT NULL,
  ingested_at       TEXT NOT NULL,
  source            TEXT NOT NULL,      -- api|forward|export|telegram|manual
  raw               TEXT                -- original payload, untouched
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, sent_at);
CREATE INDEX idx_messages_sent_at ON messages(sent_at);

-- ---------------------------------------------------------------------------
-- The promise: who, what, by when. This is Vachan's central table.
-- direction = 'they_owe' (vendor/client promised us) or 'we_owe' (we promised).
-- status    = open | done | late | missed | cancelled | disputed
-- ---------------------------------------------------------------------------
CREATE TABLE promises (
  id                 TEXT PRIMARY KEY,
  business_id        TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  project_id         TEXT REFERENCES projects(id) ON DELETE SET NULL,
  conversation_id    TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  source_message_id  TEXT REFERENCES messages(id) ON DELETE SET NULL,
  who_contact_id     TEXT REFERENCES contacts(id) ON DELETE SET NULL,
  who_raw            TEXT,               -- "Imran bhai" before we resolved the contact
  who_role           TEXT,               -- vendor | client | owner | unknown
  direction          TEXT NOT NULL DEFAULT 'they_owe',
  category           TEXT,               -- material|installation|payment|approval|visit|quote|delivery|other
  statement          TEXT NOT NULL,      -- normalised: "Deliver laminate to site"
  object             TEXT,               -- "laminate" — what it is about; de-duplication matches on this
  dedupe_key         TEXT,               -- stable identity, so a re-import never doubles a promise
  source_quote       TEXT,               -- the exact words we extracted it from
  promised_at        TEXT,               -- when it was said (imports and forwards arrive out of order)
  attempts           INTEGER NOT NULL DEFAULT 0,  -- follow-ups sent so far
  reschedules        INTEGER NOT NULL DEFAULT 0,  -- how often the date moved
  auto_missed        INTEGER NOT NULL DEFAULT 0,  -- 1 = aged by the sweep, not confirmed by the owner
  due_on             TEXT,               -- ISO date; NULL = no date promised
  due_raw            TEXT,               -- "kal shaam tak"
  due_confidence     REAL,
  confidence         REAL NOT NULL DEFAULT 0.5,
  status             TEXT NOT NULL DEFAULT 'open',
  needs_clarification INTEGER NOT NULL DEFAULT 0,
  clarifying_question TEXT,
  closed_at          TEXT,
  closed_by          TEXT,               -- agent | owner
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE INDEX idx_promises_open ON promises(business_id, status, due_on);
CREATE INDEX idx_promises_dedupe ON promises(business_id, dedupe_key);
CREATE INDEX idx_promises_who ON promises(who_contact_id);
CREATE INDEX idx_promises_project ON promises(project_id);

-- Append-only history: how a promise moved from extracted to closed.
CREATE TABLE promise_events (
  id         TEXT PRIMARY KEY,
  promise_id TEXT NOT NULL REFERENCES promises(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,   -- extracted|clarified|followed_up|replied|fulfilled|disputed|rescheduled|escalated
  detail     TEXT,
  actor      TEXT,            -- agent:extractor | agent:chaser | owner | vendor
  at         TEXT NOT NULL
);
CREATE INDEX idx_promise_events_promise ON promise_events(promise_id, at);

-- ---------------------------------------------------------------------------
-- Follow-ups Vachan sends on the owner's behalf. Never sent without a draft
-- the owner has seen, unless the project has explicitly opted into auto-send.
-- ---------------------------------------------------------------------------
CREATE TABLE follow_ups (
  id               TEXT PRIMARY KEY,
  business_id      TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  promise_id       TEXT NOT NULL REFERENCES promises(id) ON DELETE CASCADE,
  attempt          INTEGER NOT NULL DEFAULT 1,
  scheduled_for    TEXT NOT NULL,
  channel          TEXT NOT NULL,          -- whatsapp|telegram
  tone             TEXT NOT NULL DEFAULT 'warm',  -- warm|neutral|firm
  status           TEXT NOT NULL DEFAULT 'queued', -- queued|sent|skipped|snoozed|cancelled
  draft_text       TEXT,
  final_text       TEXT,
  approved_by      TEXT,
  sent_at          TEXT,
  sent_message_id  TEXT REFERENCES messages(id) ON DELETE SET NULL,
  created_at       TEXT NOT NULL
);
CREATE INDEX idx_follow_ups_due ON follow_ups(status, scheduled_for);
CREATE INDEX idx_follow_ups_promise ON follow_ups(promise_id);

-- ---------------------------------------------------------------------------
-- Proof: photos, delivery slips, "done" notes that close the loop.
-- ---------------------------------------------------------------------------
CREATE TABLE proofs (
  id           TEXT PRIMARY KEY,
  promise_id   TEXT NOT NULL REFERENCES promises(id) ON DELETE CASCADE,
  message_id   TEXT REFERENCES messages(id) ON DELETE SET NULL,
  kind         TEXT NOT NULL,   -- photo|video|voice|receipt|delivery_note|text_claim
  description  TEXT,
  media_path   TEXT,
  verified_by  TEXT NOT NULL DEFAULT 'agent',  -- agent|owner
  confidence   REAL,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_proofs_promise ON proofs(promise_id);

-- Fraud/quality signals we keep separately from the raw proof: "photo looks
-- like a stock image", "same photo used for two vendors", "promise closed
-- before the site visit it claims". Feeding these forward is what makes the
-- reliability score hard to game.
CREATE TABLE proof_flags (
  id         TEXT PRIMARY KEY,
  proof_id   TEXT NOT NULL REFERENCES proofs(id) ON DELETE CASCADE,
  code       TEXT NOT NULL,     -- duplicate_media|stale_exif|unrelated|contradicts|insufficient
  detail     TEXT,
  severity   TEXT NOT NULL DEFAULT 'low',  -- low|medium|high
  created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Reliability, snapshotted rather than overwritten so you can show a vendor
-- trending and the owner can dispute a score months later.
-- ---------------------------------------------------------------------------
CREATE TABLE vendor_score_snapshots (
  id                TEXT PRIMARY KEY,
  vendor_id         TEXT NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
  as_of             TEXT NOT NULL,
  promises_total    INTEGER NOT NULL DEFAULT 0,
  promises_kept     INTEGER NOT NULL DEFAULT 0,
  promises_late     INTEGER NOT NULL DEFAULT 0,
  promises_missed   INTEGER NOT NULL DEFAULT 0,
  on_time_rate      REAL,        -- kept / (kept + late + missed)
  kept_rate         REAL,
  avg_delay_days    REAL,
  response_rate     REAL,        -- follow-ups that got a reply / follow-ups sent
  proof_rate        REAL,        -- kept promises with proof / promises kept
  reliability       REAL,        -- 0-100
  band              TEXT,        -- strong|ok|watch|risk
  breakdown         TEXT,        -- JSON: component scores + the reasons
  computed_at       TEXT NOT NULL
);
CREATE INDEX idx_snapshots_vendor ON vendor_score_snapshots(vendor_id, as_of);

-- ---------------------------------------------------------------------------
-- Agent audit trail: every model call, its cost and what it changed.
-- ---------------------------------------------------------------------------
CREATE TABLE agent_runs (
  id           TEXT PRIMARY KEY,
  business_id  TEXT REFERENCES businesses(id) ON DELETE CASCADE,
  agent        TEXT NOT NULL,     -- extractor|chaser|closer|scorer|clarifier
  model        TEXT,
  input_ref    TEXT,
  status       TEXT NOT NULL DEFAULT 'ok',   -- ok|error|skipped
  output_ref   TEXT,
  tokens_in    INTEGER,
  tokens_out   INTEGER,
  latency_ms   INTEGER,
  error        TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT
);
CREATE INDEX idx_agent_runs_started ON agent_runs(started_at);

-- ---------------------------------------------------------------------------
-- First-class "who is allowed to message whom": keeps Vachan off numbers and
-- groups the owner never opted in.
-- ---------------------------------------------------------------------------
CREATE TABLE consents (
  id          TEXT PRIMARY KEY,
  business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  contact_id  TEXT REFERENCES contacts(id) ON DELETE SET NULL,
  scope       TEXT NOT NULL,     -- capture|follow_up|share_score
  granted     INTEGER NOT NULL DEFAULT 0,
  granted_at  TEXT,
  revoked_at  TEXT,
  evidence    TEXT
);
CREATE INDEX idx_consents_business ON consents(business_id, scope);

-- ============================================================================
-- Views (read models)
-- Deliberately no date functions, CASTs or dialect-specific functions here:
-- urgency/overdue bucketing is computed in the app, so this view is identical
-- on Postgres and SQLite.
-- ============================================================================

-- Everything the owner's "Today" screen needs, aged and bucketed.
CREATE VIEW v_open_promises AS
SELECT
  p.id,
  p.business_id,
  p.statement,
  p.category,
  p.due_on,
  p.due_raw,
  p.status,
  p.confidence,
  p.needs_clarification,
  COALESCE(c.name, p.who_raw)     AS who,
  p.who_role,
  pr.name                         AS project
FROM promises p
LEFT JOIN contacts c ON c.id = p.who_contact_id
LEFT JOIN projects pr ON pr.id = p.project_id
WHERE p.status IN ('open', 'late');

-- Vendor scoreboard straight from live promises (the snapshot table is the
-- audit copy; this view is what the UI reads).
CREATE VIEW v_vendor_reliability AS
SELECT
  v.id                              AS vendor_id,
  ct.name                           AS vendor_name,
  v.trade,
  COUNT(p.id)                       AS promises_total,
  SUM(CASE WHEN p.status = 'done'   THEN 1 ELSE 0 END) AS promises_kept,
  SUM(CASE WHEN p.status = 'late'   THEN 1 ELSE 0 END) AS promises_late,
  SUM(CASE WHEN p.status = 'missed' THEN 1 ELSE 0 END) AS promises_missed,
  SUM(CASE WHEN p.status = 'open'   THEN 1 ELSE 0 END) AS promises_open,
  0 AS avg_slip_days  -- delay math happens in the scorer, in days, portably
FROM vendors v
JOIN contacts ct ON ct.id = v.contact_id
LEFT JOIN promises p ON p.who_contact_id = ct.id
GROUP BY v.id, ct.name, v.trade;

-- ============================================================================
-- Optional PostgreSQL-only extras (uncomment on Postgres)
-- ============================================================================
-- CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid()
-- CREATE INDEX idx_messages_body_trgm ON messages USING gin (body gin_trgm_ops);
-- ALTER TABLE promises ALTER COLUMN due_on TYPE date USING due_on::date;
-- COMMENT ON TABLE promises IS 'One row per commitment extracted from a conversation.';
