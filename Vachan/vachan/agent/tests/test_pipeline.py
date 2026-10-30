"""Tests for the Vachan pipeline.

Nothing to install:

    cd agent
    python -m unittest discover -s tests -v

They build the real schema from db/schema.sql in a throwaway SQLite file, so
they test the tables the deployment will actually use rather than a mock.
(They also run under pytest if you have it: `python -m pytest -q`.)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vachan.agents import (  # noqa: E402
    AUTO_MISS_DAYS,
    Aging,
    Chaser,
    Extractor,
    Scorer,
    parse_due,
    rule_extract,
    run_pipeline,
)
from vachan.cli import parse_sample, parse_whatsapp_export, seed  # noqa: E402
from vachan.config import SAMPLES_DIR, Settings  # noqa: E402
from vachan.store import Store, new_id, now  # noqa: E402

OWNER = "Rakesh (me)"


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = handle.name
        self.cfg = Settings(database_url="sqlite:///" + self.path)
        self.store = Store(self.cfg.database_url)
        self.store.init_schema()

        self.biz = new_id("biz")
        self.store.insert("businesses", {
            "id": self.biz, "name": "Sharma Interiors", "owner_name": OWNER,
            "trade": "interior", "timezone": "Asia/Kolkata", "locale": "en-IN",
            "follow_up_hour": 10, "created_at": now(),
        })
        self.conv = new_id("cv")
        self.store.insert("conversations", {
            "id": self.conv, "business_id": self.biz, "channel": "whatsapp",
            "kind": "group", "title": "site group", "source": "export", "created_at": now(),
        })
        self.owner = self.contact(OWNER, "owner")
        self.imran = self.contact("Imran bhai", "vendor", "carpenter")
        self.store.commit()

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # pragma: no cover - already closed
            pass
        try:
            os.unlink(self.path)
        except OSError:  # pragma: no cover - temp file already gone
            pass

    # ---------------------------------------------------------------- helpers
    def contact(self, name: str, kind: str, trade: str | None = None) -> str:
        contact_id = new_id("ct")
        self.store.insert("contacts", {
            "id": contact_id, "business_id": self.biz, "name": name,
            "kind": kind, "created_at": now(),
        })
        if kind == "vendor":
            self.store.insert("vendors", {
                "id": new_id("vd"), "business_id": self.biz, "contact_id": contact_id,
                "trade": trade or "other", "active": 1, "created_at": now(),
            })
        self.store.commit()
        return contact_id

    def message(self, body: str, sender: str = "Imran bhai", direction: str = "inbound",
                when: datetime | None = None, kind: str = "text") -> dict:
        message_id = new_id("msg")
        when = when or datetime.now()
        contact_id = self.owner if direction == "outbound" else self.imran
        transcript = body if kind == "voice" else None
        sent_at = when.isoformat()
        self.store.insert("messages", {
            "id": message_id, "conversation_id": self.conv, "sender_contact_id": contact_id,
            "sender_raw_name": sender, "direction": direction, "kind": kind,
            "body": body, "transcript": transcript, "sent_at": sent_at,
            "ingested_at": now(), "source": "export",
        })
        self.store.commit()
        return {
            "id": message_id, "conversation_id": self.conv, "contact_id": contact_id,
            "sender_raw_name": sender, "direction": direction, "kind": kind,
            "body": body, "transcript": transcript, "sent_at": sent_at,
        }

    def promise(self, status: str = "open", due: str | None = None,
                closed: str | None = None, statement: str = "Deliver the ply to site") -> str:
        promise_id = new_id("pr")
        self.store.insert("promises", {
            "id": promise_id, "business_id": self.biz, "who_contact_id": self.imran,
            "who_raw": "Imran bhai", "who_role": "vendor", "direction": "they_owe",
            "category": "material", "statement": statement, "source_quote": "test",
            "due_on": due, "due_raw": "that week", "due_confidence": 0.8,
            "confidence": 0.8, "status": status, "needs_clarification": 0,
            "closed_at": closed, "closed_by": "agent:closer" if closed else None,
            "created_at": now(), "updated_at": now(),
        })
        self.store.commit()
        return promise_id

    # ------------------------------------------------------------ rule engine
    def test_hinglish_dates(self) -> None:
        sent = date(2026, 3, 10)
        self.assertEqual(parse_due("laminate kal shaam tak", sent)[0], date(2026, 3, 11))
        self.assertEqual(parse_due("parso tak pakka", sent)[0], date(2026, 3, 12))
        self.assertEqual(parse_due("aaj raat tak", sent)[0], date(2026, 3, 10))
        self.assertIsNone(parse_due("theek hai bhai", sent))

    def test_question_is_not_a_promise(self) -> None:
        out = rule_extract([self.message("kitchen kab tak ready hoga?")], OWNER)
        self.assertEqual(out["promises"], [])

    def test_extracts_a_dated_commitment(self) -> None:
        msg = self.message("Sir laminate kal shaam tak site pe pahunch jayega",
                           when=datetime(2026, 3, 10, 18, 31))
        out = rule_extract([msg], OWNER)
        self.assertEqual(len(out["promises"]), 1)
        promise = out["promises"][0]
        self.assertEqual(promise["due_on"], "2026-03-11")
        self.assertEqual(promise["object"], "laminate")
        self.assertEqual(promise["direction"], "they_owe")

    def test_owner_instruction_is_not_a_promise(self) -> None:
        msg = self.message("haan aaj aa jao", sender=OWNER, direction="outbound")
        self.assertEqual(rule_extract([msg], OWNER)["promises"], [])

    def test_a_promise_is_never_closed_by_its_own_words(self) -> None:
        msg = self.message("laminate bhej diya, kal shaam tak pakka",
                           when=datetime(2026, 3, 10, 12, 0))
        out = rule_extract([msg], OWNER)
        self.assertEqual(len(out["promises"]), 1)
        self.assertEqual(out["done_events"], [])

    # ---------------------------------------------------------- store & agents
    def test_extractor_closes_a_delivered_promise(self) -> None:
        self.message("laminate kal shaam tak bhej dunga", when=datetime(2026, 3, 10, 18, 31))
        self.message("laminate site pe pahunch gaya", when=datetime(2026, 3, 11, 10, 0))
        # Rows straight out of the database, exactly as the CLI hands them over:
        # the column is `sender_contact_id`, not `contact_id`.
        messages = self.store.select(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY sent_at", (self.conv,)
        )
        result = Extractor(self.store, self.cfg).extract(self.biz, messages)
        self.assertEqual(result["promises"], 1)
        self.assertEqual(result["closed"], 1)
        rows = self.store.select("SELECT status FROM promises WHERE business_id = ?", (self.biz,))
        self.assertEqual(rows[0]["status"], "done")

    def test_closing_picks_the_promise_the_message_is_about(self) -> None:
        ply = self.promise(statement="Deliver the ply to site", due="2026-03-11")
        laminate = self.promise(statement="Deliver the laminate to site", due="2026-03-11")
        self.message("laminate site pe pahunch gaya", when=datetime(2026, 3, 11, 10, 0))
        rows = self.store.select("SELECT * FROM messages WHERE conversation_id = ?", (self.conv,))
        Extractor(self.store, self.cfg).extract(self.biz, rows)
        statuses = {
            row["id"]: row["status"]
            for row in self.store.select(
                "SELECT id, status FROM promises WHERE business_id = ?", (self.biz,)
            )
        }
        self.assertEqual(statuses[laminate], "done")
        self.assertEqual(statuses[ply], "open", "the ply promise is a different promise")

    def test_a_later_message_dates_the_promise_it_answers(self) -> None:
        self.message("wardrobe fitting kar dunga", when=datetime(2026, 3, 10, 9, 0))
        first = self.store.select("SELECT * FROM messages WHERE conversation_id = ?", (self.conv,))
        self.assertEqual(Extractor(self.store, self.cfg).extract(self.biz, first)["promises"], 1)

        self.message("wardrobe fitting kal tak pakka kar dunga",
                     when=datetime(2026, 3, 10, 19, 0))
        second = self.store.select(
            "SELECT * FROM messages WHERE conversation_id = ? AND sender_raw_name = ? "
            "ORDER BY sent_at DESC LIMIT 1", (self.conv, "Imran bhai")
        )
        result = Extractor(self.store, self.cfg).extract(self.biz, second)
        self.assertEqual(result["backfilled"], 1, "the date should land on the waiting promise")

        rows = self.store.select("SELECT due_on, needs_clarification FROM promises WHERE business_id = ?",
                                 (self.biz,))
        self.assertEqual(len(rows), 1, "no near-duplicate should be created")
        self.assertEqual(rows[0]["due_on"], "2026-03-11")
        self.assertEqual(rows[0]["needs_clarification"], 0)

    def test_aging_never_pretends_something_was_delivered(self) -> None:
        promise_id = self.promise(due=(date.today() - timedelta(days=AUTO_MISS_DAYS + 3)).isoformat())
        self.assertEqual(Aging(self.store).sweep(self.biz), 1)
        row = self.store.select("SELECT * FROM promises WHERE id = ?", (promise_id,))[0]
        self.assertEqual(row["status"], "missed")
        self.assertEqual(row["closed_by"], "agent:aging")
        self.assertIsNone(row["closed_at"], "nothing arrived, so there is no closure time")
        events = self.store.select("SELECT kind FROM promise_events WHERE promise_id = ?", (promise_id,))
        self.assertEqual(events[0]["kind"], "auto_missed")

    def test_aging_leaves_a_fresh_promise_alone(self) -> None:
        promise_id = self.promise(due=(date.today() - timedelta(days=2)).isoformat())
        self.assertEqual(Aging(self.store).sweep(self.biz), 0)
        row = self.store.select("SELECT status FROM promises WHERE id = ?", (promise_id,))[0]
        self.assertEqual(row["status"], "open")

    def test_chaser_keeps_working_on_an_auto_missed_promise(self) -> None:
        promise_id = self.promise(due=(date.today() - timedelta(days=10)).isoformat())
        Aging(self.store).sweep(self.biz)
        self.assertEqual(Chaser(self.store, self.cfg).plan(self.biz), 1,
                         "the oldest promises are exactly the ones to chase")
        rows = self.store.select("SELECT attempt, tone FROM follow_ups WHERE promise_id = ?",
                                 (promise_id,))
        self.assertEqual(rows[0]["attempt"], 3)
        self.assertEqual(rows[0]["tone"], "firm")

    def test_a_promise_is_never_chased_more_than_three_times(self) -> None:
        promise_id = self.promise(due=(date.today() - timedelta(days=6)).isoformat())
        for attempt in (1, 2, 3):
            self.store.insert("follow_ups", {
                "id": new_id("fu"), "business_id": self.biz, "promise_id": promise_id,
                "attempt": attempt, "scheduled_for": date.today().isoformat(),
                "channel": "whatsapp", "tone": "warm", "status": "sent",
                "draft_text": "x", "created_at": now(),
            })
        self.store.commit()
        self.assertEqual(Chaser(self.store, self.cfg).plan(self.biz), 0)

    # ----------------------------------------------------------------- scoring
    def test_unscored_vendor_is_null_not_zero(self) -> None:
        self.promise(status="open")
        scores = Scorer(self.store).compute(self.biz)
        self.assertIsNone(scores[0]["reliability"])
        self.assertEqual(scores[0]["band"], "unscored")
        self.assertEqual(scores[0]["measured"], [])

    def test_an_owner_attestation_is_not_proof(self) -> None:
        promise_id = self.promise(status="done", due=date.today().isoformat(), closed=now())
        self.store.insert("proofs", {
            "id": new_id("pf"), "promise_id": promise_id, "kind": "text_claim",
            "verified_by": "owner", "created_at": now(),
        })
        self.store.commit()
        self.assertEqual(Scorer(self.store).compute(self.biz)[0]["proof_rate"], 0.0)

        self.store.insert("proofs", {
            "id": new_id("pf"), "promise_id": promise_id, "kind": "photo",
            "verified_by": "agent", "created_at": now(),
        })
        self.store.commit()
        self.assertEqual(Scorer(self.store).compute(self.biz)[0]["proof_rate"], 1.0)

    def test_a_missed_promise_scores_below_a_delivered_one(self) -> None:
        self.promise(status="missed")
        missed = Scorer(self.store).compute(self.biz)[0]["reliability"]

        self.store.execute("DELETE FROM promises")
        self.store.commit()
        promise_id = self.promise(status="done", due=date.today().isoformat(), closed=now())
        self.store.insert("proofs", {
            "id": new_id("pf"), "promise_id": promise_id, "kind": "photo",
            "verified_by": "agent", "created_at": now(),
        })
        self.store.commit()
        delivered = Scorer(self.store).compute(self.biz)[0]["reliability"]
        self.assertGreater(delivered, missed)

    def test_one_snapshot_per_vendor_per_day(self) -> None:
        self.promise(status="done", due=date.today().isoformat(), closed=now())
        Scorer(self.store).compute(self.biz)
        Scorer(self.store).compute(self.biz)
        rows = self.store.select("SELECT COUNT(*) AS n FROM vendor_score_snapshots")
        self.assertEqual(rows[0]["n"], 1, "recomputing must replace, not pile up")

    def test_breakdown_is_stored_as_json_text(self) -> None:
        self.promise(status="done", due=date.today().isoformat(), closed=now())
        Scorer(self.store).compute(self.biz)
        rows = self.store.select("SELECT breakdown FROM vendor_score_snapshots")
        self.assertIn('"version"', rows[0]["breakdown"])

    # ------------------------------------------------------------------ ingestion
    def test_whatsapp_export_parsing(self) -> None:
        text = ("[15/03/26, 6:31 PM] Imran bhai: laminate kal shaam tak\n"
                "15/03/26, 18:33 - Ramesh: theek hai")
        messages = parse_whatsapp_export(text)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["when"].hour, 18, "6:31 PM must read as 18h")
        self.assertEqual(messages[1]["who"], "Ramesh")

    def test_the_sample_business_runs_end_to_end(self) -> None:
        text = (SAMPLES_DIR / "villa42-week.txt").read_text(encoding="utf-8")
        parsed = parse_sample(text, today=date(2026, 3, 10))
        self.assertTrue(parsed, "the sample file should parse")
        seeded = seed(self.store, self.cfg, parsed)
        messages = self.store.select(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY sent_at",
            (seeded["conversation_id"],),
        )
        result = run_pipeline(self.store, self.cfg, seeded["business_id"], messages)
        self.assertGreater(result["promises"], 0, "the sample contains real commitments")
        self.assertGreater(result["follow_ups"], 0, "something in the sample is overdue")
        self.assertTrue(result["scores"], "vendors should be scored")
        self.assertTrue(all("reliability" in row for row in result["scores"]))
        names = " ".join(row["name"] for row in result["scores"])
        self.assertIn("Imran", names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
