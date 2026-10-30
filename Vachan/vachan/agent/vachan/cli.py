"""Command line for the local pipeline.

    python -m vachan demo                 # seed the sample and run everything
    python -m vachan init-db              # create the schema
    python -m vachan ingest chat.txt      # a real WhatsApp export
    python -m vachan scores               # vendor reliability

Everything is standard library, so it runs on a fresh machine with nothing
installed. Run it from the `vachan/agent` directory (or `make demo`).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta

from .agents import OWNER_HINTS, Aging, Chaser, Scorer, run_pipeline
from .config import SAMPLES_DIR, Settings, settings
from .store import Store, new_id, now

SAMPLE_LINE = re.compile(
    r"^\[D-(\d+),\s*(\d{1,2}:\d{2})(?:,\s*(voice|photo|image))?\]\s*([^:]{1,40}):\s*(.*)$"
)
WA_LINE = re.compile(
    r"^\[?(\d{1,2})/(\d{1,2})/(\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:([ap])\.?m\.?)?\]?\s*(?:-\s*)?([^:]{1,40}):\s*(.*)$",
    re.I,
)

# Closed history so reliability means something on day one, not after a month.
# (vendor, statement, category, due offset in days, closed offset, had proof)
HISTORY = [
    ("Imran bhai", "Deliver ply for the wardrobes", "material", -46, -46, True),
    ("Imran bhai", "Complete the TV unit frame", "installation", -38, -38, True),
    ("Imran bhai", "Deliver drawer channels", "material", -30, -29, True),
    ("Imran bhai", "Fix the wardrobe hinges", "installation", -24, -24, True),
    ("Imran bhai", "Deliver laminate for the kitchen", "material", -17, -17, True),
    ("Imran bhai", "Polish the wardrobe shutters", "installation", -9, -8, True),
    ("Painter Ramesh", "Prime the living room walls", "other", -40, -40, True),
    ("Painter Ramesh", "Deliver the putty", "material", -33, -31, False),
    ("Painter Ramesh", "Second coat of primer", "other", -26, None, False),
    ("Painter Ramesh", "Paint the balcony ceiling", "other", -19, -19, True),
    ("Ravi Electrician", "Conceal the kitchen wiring", "installation", -35, -35, True),
    ("Ravi Electrician", "Install the switch plates", "installation", -27, -27, True),
    ("Ravi Electrician", "Dress the DB panel", "installation", -21, -18, False),
]


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def parse_sample(text: str, today: date | None = None) -> list[dict]:
    """Reads samples/*.txt. D-n dates become real dates relative to today."""
    today = today or date.today()
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_LINE.match(line)
        if not match:
            continue
        offset, clock, kind, who, body = match.groups()
        clock = clock if len(clock.split(":")[0]) == 2 else "0" + clock
        day = today - timedelta(days=int(offset))
        when = datetime.combine(day, datetime.strptime(clock, "%H:%M").time())
        out.append({"when": when, "who": who.strip(), "body": body.strip(), "kind": kind})
    return out


def parse_whatsapp_export(text: str) -> list[dict]:
    """The real thing: a chat .txt exported from WhatsApp."""
    out = []
    for raw in text.splitlines():
        match = WA_LINE.match(raw.strip())
        if not match:
            continue
        day, month, year, clock, meridiem, who, body = match.groups()
        year = int(year) + 2000 if len(year) <= 2 else int(year)
        hour, minute = (int(x) for x in clock.split(":")[:2])
        if meridiem:
            if meridiem.lower() == "p" and hour != 12:
                hour += 12
            if meridiem.lower() == "a" and hour == 12:
                hour = 0
        when = datetime(year, int(month), int(day), hour, minute)
        lower = body.lower()
        kind = "voice" if "[voice" in lower or "ptt" in lower else (
            "image" if "<media omitted>" in lower or "[photo]" in lower else "text")
        out.append({"when": when, "who": who.strip(), "body": body.strip(), "kind": kind})
    return out


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------
def seed(store: Store, cfg: Settings, parsed: list[dict]) -> dict:
    business_id = new_id("biz")
    store.insert("businesses", {
        "id": business_id, "name": cfg.business_name, "owner_name": cfg.owner_name,
        "trade": "interior", "timezone": cfg.timezone, "locale": "en-IN",
        "follow_up_hour": 10, "created_at": now(),
    })
    project_id, conversation_id = new_id("prj"), new_id("cv")
    store.insert("projects", {
        "id": project_id, "business_id": business_id, "name": "Villa 42 — kitchen & wardrobes",
        "site_address": "Villa 42, Whitefield", "status": "active", "created_at": now(),
    })
    store.insert("conversations", {
        "id": conversation_id, "business_id": business_id, "project_id": project_id,
        "channel": "whatsapp", "kind": "group", "external_id": "villa42",
        "title": "Villa 42 — site group", "source": "export", "created_at": now(),
    })

    contacts: dict[str, str] = {}
    trades = {"Imran bhai": "carpenter", "Painter Ramesh": "painter", "Ravi Electrician": "electrician"}

    def contact(name: str, kind: str) -> str:
        if name in contacts:
            return contacts[name]
        cid = new_id("ct")
        store.insert("contacts", {
            "id": cid, "business_id": business_id, "name": name, "kind": kind, "created_at": now(),
        })
        contacts[name] = cid
        if kind == "vendor":
            store.insert("vendors", {
                "id": new_id("vd"), "business_id": business_id, "contact_id": cid,
                "trade": trades.get(name, "other"), "active": 1, "created_at": now(),
            })
        return cid

    contact(cfg.owner_name, "owner")
    for name in trades:
        contact(name, "vendor")
    contact("Mrs Iyer", "client")

    for item in parsed:
        # A WhatsApp export shows the owner's profile name, which is rarely the
        # exact string in the config — "Rakesh (me)" vs "Rakesh". Without the
        # pattern, the owner's own promises are attributed to a vendor.
        is_owner = bool(OWNER_HINTS.match(item["who"])) or item["who"] == cfg.owner_name
        cid = contact(item["who"], "owner" if is_owner else ("client" if "iyer" in item["who"].lower() else "vendor"))
        store.insert("messages", {
            "id": new_id("msg"), "conversation_id": conversation_id, "sender_contact_id": cid,
            "sender_raw_name": item["who"],
            "direction": "outbound" if is_owner else "inbound",
            "kind": item["kind"] or "text",
            "body": item["body"],
            "transcript": item["body"] if item["kind"] == "voice" else None,
            "sent_at": item["when"].isoformat(), "ingested_at": now(),
            "source": "export",
        })

    for name, statement, category, due_off, closed_off, had_proof in HISTORY:
        cid = contacts.get(name)
        if not cid:
            continue
        due = (date.today() + timedelta(days=due_off)).isoformat()
        closed_at = None
        status = "missed"
        if closed_off is not None:
            closed_at = datetime.combine(
                date.today() + timedelta(days=closed_off), datetime.min.time().replace(hour=17)
            ).isoformat()
            status = "late" if closed_off > due_off else "done"
        promise_id = new_id("pr")
        store.insert("promises", {
            "id": promise_id, "business_id": business_id, "project_id": project_id,
            "who_contact_id": cid, "who_raw": name, "who_role": "vendor", "direction": "they_owe",
            "category": category, "statement": statement, "source_quote": statement,
            "due_on": due, "due_raw": "that week", "due_confidence": 0.7, "confidence": 0.8,
            "status": status, "needs_clarification": 0, "closed_at": closed_at,
            "closed_by": "agent:closer", "created_at": now(), "updated_at": now(),
        })
        if had_proof:
            store.insert("proofs", {
                "id": new_id("pf"), "promise_id": promise_id, "kind": "photo",
                "description": "Delivery photo", "verified_by": "agent",
                "created_at": closed_at or now(),
            })

    store.commit()
    return {"business_id": business_id, "conversation_id": conversation_id, "contacts": contacts}


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def _store(cfg: Settings) -> Store:
    return Store(cfg.database_url)


def cmd_init_db(cfg: Settings, args) -> int:
    with _store(cfg) as store:
        store.init_schema()
    print(f"schema created in {cfg.database_url}")
    return 0


def cmd_demo(cfg: Settings, args) -> int:
    sample = SAMPLES_DIR / "villa42-week.txt"
    parsed = parse_sample(sample.read_text(encoding="utf-8"))
    with _store(cfg) as store:
        store.init_schema()
        seeded = seed(store, cfg, parsed)
        messages = store.select(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY sent_at",
            (seeded["conversation_id"],),
        )
        result = run_pipeline(store, cfg, seeded["business_id"], messages)
    _report(cfg, result, store_rows=None)
    return 0


def cmd_ingest(cfg: Settings, args) -> int:
    path = args.path
    with open(path, encoding="utf-8", errors="replace") as handle:
        parsed = parse_whatsapp_export(handle.read())
    if not parsed:
        print(f"no messages recognised in {path}", file=sys.stderr)
        return 2
    with _store(cfg) as store:
        seeded = store.select("SELECT id, business_id FROM conversations ORDER BY created_at LIMIT 1")
        if not seeded:
            print("no business yet — run `python -m vachan demo` first", file=sys.stderr)
            return 2
        conversation_id, business_id = seeded[0]["id"], seeded[0]["business_id"]
        for item in parsed:
            # Same rule as in seed(): the export shows a profile name, not
            # necessarily the string in the config.
            is_owner = bool(OWNER_HINTS.match(item["who"])) or item["who"] == cfg.owner_name
            known = store.select("SELECT id FROM contacts WHERE business_id = ? AND name = ?",
                                 (business_id, item["who"]))
            contact_id = known[0]["id"] if known else None
            store.insert("messages", {
                "id": new_id("msg"), "conversation_id": conversation_id,
                "sender_contact_id": contact_id, "sender_raw_name": item["who"],
                "direction": "outbound" if is_owner else "inbound",
                "kind": item["kind"] or "text", "body": item["body"],
                "transcript": item["body"] if item["kind"] == "voice" else None,
                "sent_at": item["when"].isoformat(), "ingested_at": now(), "source": "export",
            })
        store.commit()
        messages = store.select(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY sent_at", (conversation_id,)
        )
        result = run_pipeline(store, cfg, business_id, messages)
    _report(cfg, result, store_rows=None)
    return 0


def cmd_scores(cfg: Settings, args) -> int:
    with _store(cfg) as store:
        businesses = store.select("SELECT id FROM businesses ORDER BY created_at LIMIT 1")
        if not businesses:
            print("nothing scored yet — run `python -m vachan demo`", file=sys.stderr)
            return 2
        scores = Scorer(store).compute(businesses[0]["id"])
    _print_scores(scores)
    return 0


def cmd_sweep(cfg: Settings, args) -> int:
    """What a cron entry or systemd timer runs: age the ledger, chase, rescore.

    Extraction is deliberately not part of this: nothing new has arrived at
    06:00, but promises have quietly become overdue overnight.
    """
    with _store(cfg) as store:
        businesses = store.select("SELECT id FROM businesses ORDER BY created_at LIMIT 1")
        if not businesses:
            print("no business yet — run `python -m vachan demo` first", file=sys.stderr)
            return 2
        business_id = businesses[0]["id"]
        aged = Aging(store).sweep(business_id)
        chased = Chaser(store, cfg).plan(business_id)
        scores = Scorer(store).compute(business_id)
        store.commit()
    print(f"  aged {aged} promise(s) to missed · {chased} new follow-up draft(s)")
    _print_scores(scores)
    return 0


def _print_scores(scores: list[dict]) -> None:
    print()
    print("  VENDOR RELIABILITY")
    for row in scores:
        value = "unscored" if row["reliability"] is None else f"{row['reliability']:>5.1f} {row['band']}"
        print(f"  {row['name']:<18} {value:<22} kept {row['kept']}/{row['total']} "
              f"missed {row['missed']} open {row['open']}")


def _report(cfg: Settings, result: dict, store_rows=None) -> None:
    print()
    print(f"  Vachan — {cfg.business_name}   (extractor: {result['engine']})")
    print("  " + "-" * 66)
    print(f"  promises extracted : {result['promises']}")
    print(f"  dates backfilled   : {result.get('backfilled', 0)}")
    print(f"  promises closed    : {result['closed']}")
    print(f"  aged to missed     : {result.get('aged', 0)}")
    print(f"  follow-up drafts   : {result['follow_ups']}")
    _print_scores(result["scores"])


def main(argv: list[str] | None = None) -> int:
    cfg = settings()
    parser = argparse.ArgumentParser(prog="vachan", description="Vachan local pipeline")
    parser.add_argument("--mode", help="override VACHAN_LLM_MODE (mock|ollama|openai)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create the schema")
    sub.add_parser("demo", help="seed the sample conversation and run the pipeline")
    ingest = sub.add_parser("ingest", help="ingest a WhatsApp .txt export")
    ingest.add_argument("path")
    sub.add_parser("sweep", help="age overdue promises, draft chases, rescore (cron)")
    sub.add_parser("scores", help="print vendor reliability")

    args = parser.parse_args(argv)
    if args.mode:
        cfg = Settings(**{**cfg.__dict__, "mode": args.mode})

    try:
        if args.command == "init-db":
            return cmd_init_db(cfg, args)
        if args.command == "demo":
            return cmd_demo(cfg, args)
        if args.command == "ingest":
            return cmd_ingest(cfg, args)
        if args.command == "sweep":
            return cmd_sweep(cfg, args)
        return cmd_scores(cfg, args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
