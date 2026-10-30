-- ============================================================================
-- Vachan — Supabase tenancy + Row Level Security
--
-- Run AFTER db/schema.sql. Two files rather than one so the portable schema
-- stays portable (it also boots a plain SQLite file) and everything Supabase
-- specific — auth.users, RLS, security_invoker — lives here.
--
--   psql "$DATABASE_URL" -f db/schema.sql
--   psql "$DATABASE_URL" -f db/supabase_rls.sql
--
-- Or paste both into the Supabase SQL editor, in that order.
--
-- WHO BYPASSES RLS
--   The Python agent connects as the `postgres` role, which OWNS these tables,
--   so RLS does not apply to it. That is deliberate: it is a trusted backend.
--   RLS is here for the browser path, because PostgREST turns a public anon key
--   plus a user's JWT into SQL. Without these policies the anon key would let
--   anyone with the URL read every business's promises — the anon key is public
--   by design and ships inside the page.
--
-- THE MODEL
--   One business per small firm. A row in business_members maps an auth user to
--   a business. Every policy is the same sentence: "a row is visible if its
--   business_id is one of mine."
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Tenancy bridge: auth.users is Supabase's; this is ours.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS business_members (
  business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role        TEXT NOT NULL DEFAULT 'owner',      -- owner | staff
  created_at  TEXT NOT NULL,
  PRIMARY KEY (business_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_user ON business_members(user_id);

-- One helper, so every policy reads the same way and there is a single place
-- to change how membership is decided.
--
-- security definer: the function itself must read business_members, which has
-- RLS. Without this it would recurse. `set search_path` stops a caller from
-- shadowing `business_members` with their own function.
CREATE OR REPLACE FUNCTION vachan_business_ids()
RETURNS SETOF TEXT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT business_id FROM business_members WHERE user_id = auth.uid()
$$;

REVOKE ALL ON FUNCTION vachan_business_ids() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION vachan_business_ids() TO authenticated;

-- ---------------------------------------------------------------------------
-- Bootstrap: a signed-in user must be able to create their first business and
-- then join it. Every other write is scoped to a business they already belong
-- to, so this is the only place a non-member may insert.
-- ---------------------------------------------------------------------------
ALTER TABLE business_members ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS members_read_own ON business_members;
CREATE POLICY members_read_own ON business_members
  FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR business_id IN (SELECT vachan_business_ids()));

DROP POLICY IF EXISTS members_join_self ON business_members;
CREATE POLICY members_join_self ON business_members
  FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS members_leave_self ON business_members;
CREATE POLICY members_leave_self ON business_members
  FOR DELETE TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Tables that carry business_id directly.
-- ---------------------------------------------------------------------------
ALTER TABLE businesses ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS businesses_read ON businesses;
CREATE POLICY businesses_read ON businesses
  FOR SELECT TO authenticated
  USING (id IN (SELECT vachan_business_ids()));

DROP POLICY IF EXISTS businesses_create ON businesses;
CREATE POLICY businesses_create ON businesses
  FOR INSERT TO authenticated
  WITH CHECK (true);   -- followed immediately by members_join_self

DROP POLICY IF EXISTS businesses_update ON businesses;
CREATE POLICY businesses_update ON businesses
  FOR UPDATE TO authenticated
  USING (id IN (SELECT vachan_business_ids()))
  WITH CHECK (id IN (SELECT vachan_business_ids()));

DROP POLICY IF EXISTS businesses_delete ON businesses;
CREATE POLICY businesses_delete ON businesses
  FOR DELETE TO authenticated
  USING (id IN (SELECT vachan_business_ids()));

DO $$
DECLARE
  t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users', 'contacts', 'vendors', 'projects', 'conversations',
    'messages', 'promises', 'follow_ups', 'agent_runs', 'consents'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR ALL TO authenticated
         USING (business_id IN (SELECT vachan_business_ids()))
         WITH CHECK (business_id IN (SELECT vachan_business_ids()))',
      t || '_tenant', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO authenticated', t);
  END LOOP;
END $$;

-- agent_runs.business_id is nullable (a run can fail before it knows the firm).
-- The blanket policy above hides those rows, which is right: an unattributed
-- run is a log line for the operator, not for the customer.

-- ---------------------------------------------------------------------------
-- Tables that reach a business through a parent row. No business_id column, so
-- the policy walks the foreign key instead.
-- ---------------------------------------------------------------------------
ALTER TABLE promise_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS promise_events_tenant ON promise_events;
CREATE POLICY promise_events_tenant ON promise_events
  FOR ALL TO authenticated
  USING (EXISTS (
    SELECT 1 FROM promises p
    WHERE p.id = promise_events.promise_id
      AND p.business_id IN (SELECT vachan_business_ids())
  ))
  WITH CHECK (EXISTS (
    SELECT 1 FROM promises p
    WHERE p.id = promise_events.promise_id
      AND p.business_id IN (SELECT vachan_business_ids())
  ));
GRANT SELECT, INSERT, UPDATE, DELETE ON promise_events TO authenticated;

ALTER TABLE proofs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS proofs_tenant ON proofs;
CREATE POLICY proofs_tenant ON proofs
  FOR ALL TO authenticated
  USING (EXISTS (
    SELECT 1 FROM promises p
    WHERE p.id = proofs.promise_id
      AND p.business_id IN (SELECT vachan_business_ids())
  ))
  WITH CHECK (EXISTS (
    SELECT 1 FROM promises p
    WHERE p.id = proofs.promise_id
      AND p.business_id IN (SELECT vachan_business_ids())
  ));
GRANT SELECT, INSERT, UPDATE, DELETE ON proofs TO authenticated;

ALTER TABLE proof_flags ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS proof_flags_tenant ON proof_flags;
CREATE POLICY proof_flags_tenant ON proof_flags
  FOR ALL TO authenticated
  USING (EXISTS (
    SELECT 1 FROM proofs pr
    JOIN promises p ON p.id = pr.promise_id
    WHERE pr.id = proof_flags.proof_id
      AND p.business_id IN (SELECT vachan_business_ids())
  ))
  WITH CHECK (EXISTS (
    SELECT 1 FROM proofs pr
    JOIN promises p ON p.id = pr.promise_id
    WHERE pr.id = proof_flags.proof_id
      AND p.business_id IN (SELECT vachan_business_ids())
  ));
GRANT SELECT, INSERT, UPDATE, DELETE ON proof_flags TO authenticated;

ALTER TABLE conversation_participants ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS participants_tenant ON conversation_participants;
CREATE POLICY participants_tenant ON conversation_participants
  FOR ALL TO authenticated
  USING (EXISTS (
    SELECT 1 FROM conversations c
    WHERE c.id = conversation_participants.conversation_id
      AND c.business_id IN (SELECT vachan_business_ids())
  ))
  WITH CHECK (EXISTS (
    SELECT 1 FROM conversations c
    WHERE c.id = conversation_participants.conversation_id
      AND c.business_id IN (SELECT vachan_business_ids())
  ));
GRANT SELECT, INSERT, UPDATE, DELETE ON conversation_participants TO authenticated;

ALTER TABLE vendor_score_snapshots ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS snapshots_tenant ON vendor_score_snapshots;
CREATE POLICY snapshots_tenant ON vendor_score_snapshots
  FOR ALL TO authenticated
  USING (EXISTS (
    SELECT 1 FROM vendors v
    WHERE v.id = vendor_score_snapshots.vendor_id
      AND v.business_id IN (SELECT vachan_business_ids())
  ))
  WITH CHECK (EXISTS (
    SELECT 1 FROM vendors v
    WHERE v.id = vendor_score_snapshots.vendor_id
      AND v.business_id IN (SELECT vachan_business_ids())
  ));
GRANT SELECT, INSERT, UPDATE, DELETE ON vendor_score_snapshots TO authenticated;

-- ---------------------------------------------------------------------------
-- Views bypass RLS unless told otherwise: a view runs with the privileges of
-- its owner (postgres), who owns the tables and therefore skips their policies.
-- Left alone, v_open_promises would hand every business's ledger to anyone
-- holding the anon key. Postgres 15+ can make a view respect the caller's RLS.
-- ---------------------------------------------------------------------------
ALTER VIEW v_open_promises        SET (security_invoker = true);
ALTER VIEW v_vendor_reliability   SET (security_invoker = true);
GRANT SELECT ON v_open_promises, v_vendor_reliability TO authenticated;

-- ---------------------------------------------------------------------------
-- Verify from the SQL editor (runs as postgres, so it reports what the
-- *authenticated* role would see, not what you see):
--
--   SELECT tablename, rowsecurity FROM pg_tables
--    WHERE schemaname = 'public' ORDER BY tablename;
--   -- every row must be true. A false here is an open door.
--
--   SET LOCAL ROLE authenticated;
--   SELECT set_config('request.jwt.claims',
--     json_build_object('sub', '00000000-0000-0000-0000-000000000000')::text, true);
--   SELECT count(*) FROM promises;   -- 0 unless that user is a member
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Free-tier note: Supabase pauses a project after 7 days with no database
-- activity. A ledger that is only opened when someone remembers to chase is
-- exactly the shape that pauses. Either accept a cold start after a holiday,
-- or have the morning sweep (`python -m vachan sweep`) run daily from GitHub
-- Actions/cron — it writes every day, which counts as activity.
-- ---------------------------------------------------------------------------
