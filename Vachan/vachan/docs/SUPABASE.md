# Supabase

Vachan ships with two storage paths and no server in between. This file is about
the hosted one.

| Where | What it stores | Who can read it |
|---|---|---|
| Browser (`localStorage`) | The whole ledger, offline | That browser |
| Supabase (Postgres) | The same ledger, shared | Signed-in members of that business |
| SQLite (`./vachan.db`) | The Python agent's copy | Whoever holds the file |

The app works with **none** of them configured. Connecting Supabase is what
turns "my laptop remembers" into "the office, my phone and the agent all see the
same promises".

## Why an anon key in a public page is not a leak

The `anon` key is designed to be public: it identifies the project, not the user.
It is safe only because the database refuses to return anything the caller is not
a member of. That refusal is Row Level Security, and without it the anon key is a
read-write key to every customer's data — which is why `db/supabase_rls.sql`
exists and why the browser app refuses to sync against a project that has not
had it applied.

The model is one sentence: **a row is visible if its `business_id` is one of
mine**, where "mine" comes from `business_members`, joined to Supabase's own
`auth.users`. A signed-in user may create their first business and then a
membership row for themselves; everything else is scoped.

```
auth.users ──1:1── business_members ──N:1── businesses
                                                 │
                              every other table ─┘  via business_id,
                                                    or via its parent row
```

## Setup

1. **Create a project** at [supabase.com](https://supabase.com/dashboard). The free
   tier is one project per organisation, 500 MB of database, and it pauses after
   seven days without activity (see *Limits* below).
2. **Create the schema.** Open *SQL Editor* and run, in this order:
   - `db/schema.sql`
   - `db/supabase_rls.sql`

   Or from a terminal, which also proves the files parse outside the editor:

   ```bash
   psql "$DATABASE_URL" -f db/schema.sql
   psql "$DATABASE_URL" -f db/supabase_rls.sql
   ```

3. **Check the locks are on.** In the SQL editor:

   ```sql
   SELECT tablename, rowsecurity FROM pg_tables
    WHERE schemaname = 'public' ORDER BY tablename;
   ```

   Every row must say `true`. A `false` is an open table.

4. **Create a user.** Either *Authentication → Users → Add user* in the dashboard,
   or the app's own **Create account** button. With "Confirm email" on (the
   default) the new user must click the link in their inbox before signing in.

5. **Connect the page.** Open `app/index.html`, go to **Database → Shared
   database**, paste the *Project URL* and the *anon public key* from *Project
   settings → API*, then sign in and press **Push to Supabase**.

The first push writes your business row, then a membership row, then the ledger.

## The Python agent

The agent connects directly as the `postgres` role, which **owns** these tables
and therefore bypasses RLS. That is deliberate — it is a trusted backend, and it
is the reason RLS protects the browser path without getting in the agent's way.

Use the direct connection (port 5432) or the pooler (6543); both work:

```bash
# direct — best for long runs and bulk import
DATABASE_URL="postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres?sslmode=require"

# pooler — better behind pgbouncer or a laptop with a changing IP
DATABASE_URL="postgresql://postgres.PROJECT:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres"

pip install "vachan[postgres]"
python -m vachan init-db     # only against an empty database
python -m vachan sweep
```

`store.py` handles the two hosted-only traps for you:

- **Pooler + prepared statements.** Port 6543 runs pgbouncer in transaction mode,
  where server-side prepared statements do not survive between transactions.
  psycopg3 starts using them after five executions, so the classic symptom is a
  job that works all morning and then dies with
  `prepared statement "_pg3_0" does not exist`. Detected from the port or a
  `pooler` host, and `prepare_threshold` is turned off.
- **TLS.** Added as `sslmode=require` for any non-local host that does not already
  specify it — never duplicated, because passing it twice is an error.

## Limits, and the one that matters

Free tier, as of the plan: **500 MB** database, **2** projects, **7 days of
inactivity → the project pauses**. Pausing is not deletion; it resumes on the
next request after a wake-up, but the first request after a holiday can fail.

A promise ledger is exactly the workload that goes quiet: a firm that stops
chasing for a week pauses its own database. Two honest options:

- Accept the cold start.
- Let the morning sweep be the activity. `python -m vachan sweep` writes every
  day, so a scheduled run in GitHub Actions or cron both chases promises and
  keeps the project awake. The free tier counts database activity, and that is
  database activity.

## What is verified, and what is not

Verified on this machine, in the browser:

- 33/33 in the app's own suite, including six cases for this integration: rows
  carry the business they are pushed for, two businesses never collide on a
  primary key, a promise survives a round trip through a database row, a push
  writes parents before children, a pull takes newer rows and keeps yours on a
  tie, and no token can reach a backup.
- A real `cloudPush` against a stubbed endpoint: 71 rows, 11 correct
  `POST /rest/v1/<table>?on_conflict=id` requests in foreign-key order with
  `Prefer: resolution=merge-duplicates,return=minimal`, 29 columns per promise,
  and a 401 surfacing as `Supabase 401 — JWT expired` rather than failing
  silently.
- The panel itself: a bad URL is refused and nothing is stored; a good one is
  normalised and kept.

**Not verified here:** no request has ever been made to a live Supabase project,
because there is no project and no credentials on this machine. The SQL has not
been executed by `psql` either — treat `db/supabase_rls.sql` as written and
reviewed, not run. Run step 3 above before you trust it with real conversations.

## When it goes wrong

| Symptom | Cause |
|---|---|
| `new row violates row-level security policy` | The user is not a member of that `business_id`. Push with the app, which creates the membership, or add the row to `business_members` by hand. |
| `Supabase 401 — JWT expired` | Session aged out. The app refreshes once, then asks you to sign in again. |
| Sign-in fails with "Email not confirmed" | Dashboard default. Click the link, or turn confirmation off for local testing. |
| `relation "business_members" does not exist` | `db/supabase_rls.sql` was not run. |
| Sync succeeded but the app looks unchanged | You pushed; the other device has to **Pull** (or **Sync both ways**). |
| Pull shows `0 new, 0 updated, n kept` | Your copy is newer everywhere. Nothing is wrong. |
| `prepared statement "_pg3_0" does not exist` | Connecting through the pooler without `prepare_threshold` off. Fixed in `store.py`; upgrade if you vendored it. |

## Deliberately not built

- **No server-side sync.** The browser talks to PostgREST directly. That is right
  for one owner on two devices and wrong for a team of ten, where merges need to
  happen where the row is, not where the tab is.
- **No realtime.** Supabase Realtime would be the natural next step: a promise
  closed on the phone should disappear from the laptop without a pull. It needs
  the publication configured per table, and it is not wired here.
- **No file storage.** Photos that prove delivery belong in Supabase Storage with
  `proofs.media_path` holding the object key. `proofs` is ready for it; nothing
  uploads yet.
- **Append-only rows are never reconciled.** `messages`, `proofs` and
  `promise_events` are only ever added, on the assumption that nobody edits the
  past. Promises carry `updated_at` and are last-write-wins.
