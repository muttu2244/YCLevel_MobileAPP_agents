"""The agent team: extractor, chaser, closer, scorer.

Mock mode is not a stub. It is a deterministic rule engine that emits the same
JSON shape the prompt asks a model for, so the entire pipeline, the tests and
the local demo run with no key, no network and no model. Point
``VACHAN_LLM_MODE`` at Ollama or any OpenAI-compatible endpoint and only the
JSON producer changes.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from .config import Settings
from .llm import client_for, parse_json
from .prompts import system_prompt
from .store import Store, new_id, now

# Nothing has arrived this long after it was promised: stop calling it open.
AUTO_MISS_DAYS = 7

# ===========================================================================
# Rule engine (mock mode)
# ===========================================================================

OBJECTS = [
    "colour sample", "color sample", "laminate sheet", "drawer channel", "switch plate",
    "false ceiling", "modular kitchen", "plywood", "laminate", "ply", "tiles", "tile",
    "marble", "granite", "putty", "paint", "hardware", "hinge", "channel", "drawer",
    "glass", "fabric", "sofa", "curtain", "light", "wiring", "wire", "pipe", "door",
    "window", "wardrobe", "kitchen", "sheet", "varnish", "polish", "grill", "ceiling",
    "wallpaper", "sample",
]

COMMIT = [
    (re.compile(r"bhej\s*(?:dunga|dega|denge|diya|doonga)|bhejenge", re.I), 0.25),
    (re.compile(r"kar\s*(?:dunga|dega|denge|diya|doonga)|kar\s*jayega|kar\s*jayenge", re.I), 0.25),
    (re.compile(r"ho\s*(?:jayega|jayenge)|aa\s*(?:jayega|jayenge|gaya|jayegi)"
                r"|pahunch\s*(?:jayega|gaya|jayenge|jaunga)|lag\s*jayega|fit\s*hoga", re.I), 0.20),
    (re.compile(r"de\s*(?:dunga|dega|denge|diya)|pay\s*(?:kar|karna|dunga)|will\s+pay", re.I), 0.25),
    (re.compile(r"order\s*kar\s*(?:diya|dunga|diy[ae]|hai)|arrange\s*kar\s*dunga", re.I), 0.20),
    (re.compile(r"will\s+(?:send|deliver|do|come|visit|share|arrange|complete|pay|confirm)", re.I), 0.25),
    (re.compile(r"aa\s*jao|aa\s*jaunga|mil\s*leta|dekh\s*leta|start\s*kar\s*dunga", re.I), 0.20),
    (re.compile(r"\bpakka\b|\bconfirmed\b|\bguarantee\b", re.I), 0.15),
]

CATEGORY_RULES = [
    ("payment", r"payment|paisa|paise|balance|advance|bill|invoice|salary|\bpay\b|de dunga|de dega|de diya"),
    ("approval", r"approv|sanction|sign\s*kar|ok\s*kar\s*dijiye|confirm\s*kar\s*dijiye"),
    ("quote", r"quote|quotation|estimate|\brate\b|price|rate bata"),
    ("drawing", r"drawing|layout|elevation|3d|design|colour sample|color sample|sample"),
    ("installation", r"fitting|install|fix\b|lagana|lag\s*jayega|polish|varnish|carpentry|wiring|plumbing|kaam\b|work\b|fit\s*hoga|start\s*kar"),
    ("material", r"laminate|ply|tile|marble|granite|putty|paint|hardware|hinge|channel|drawer|glass|fabric|curtain|sheet|grill|wallpaper|order|material"),
    ("delivery", r"deliver|pahunch|bhej|send|dispatch|transport|arrange"),
    ("visit", r"visit|aayenge|aa\s*jao|aa\s*jaunga|milne|dekhenge|site\s*pe"),
    ("manpower", r"labour|labor|mazdoor|worker|mistri|team|staff|helper"),
]

DONE_HINT = re.compile(
    r"aa\s*gaya|pahunch\s*gaya|ho\s*gaya|kar\s*diya|de\s*diya|bhej\s*diya|khatam"
    r"|completed?\b|done\b|delivered",
    re.I,
)
QUESTION_ONLY = re.compile(
    r"\?\s*$|^\s*(?:kya|kyun|kab|kaise|where|when|what|how|bhej\s*sakte|kar\s*sakte|aa\s*sakte)",
    re.I,
)

WEEKDAYS = {
    "monday": 0, "somvar": 0, "somwar": 0,
    "tuesday": 1, "mangalvar": 1, "mangalwar": 1,
    "wednesday": 2, "budhvar": 2,
    "thursday": 3, "guruvar": 3,
    "friday": 4, "shukravar": 4,
    "saturday": 5, "shanivar": 5, "shaniwar": 5,
    "sunday": 6, "ravivar": 6,
}

OWNER_HINTS = re.compile(r"^rakesh\b|\(me\)|^me$", re.I)
ROLE_PREFIX = re.compile(r"^(?:painter|carpenter|electrician|plumber|mistri|sir|mr\.?|mrs\.?|ms\.?|the|bhaiya|bhai|ji)$", re.I)


def _contact_of(message: dict):
    """Rows out of the database say `sender_contact_id`; extracted dicts say
    `contact_id`. One accessor so a caller cannot silently pass the wrong one
    and have every close quietly fail to match."""
    return message.get("contact_id") or message.get("sender_contact_id")


def detect_object(text: str) -> str | None:
    low = text.lower()
    best = None
    for candidate in OBJECTS:
        if candidate in low and (best is None or len(candidate) > len(best)):
            best = candidate
    return best


def detect_category(text: str) -> str:
    low = text.lower()
    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, low):
            return name
    return "other"


def detect_amount(text: str) -> int | None:
    match = re.search(r"(?:₹|rs\.?\s*)(\d[\d,]{2,})", text, re.I) or re.search(r"\b(\d{1,3},\d{3})\b", text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_due(text: str, sent: date):
    """Returns (due_date, raw_phrase, confidence) or None. Order matters."""
    low = text.lower()
    for pattern, days, conf in (
        (r"\b(parso|parson|day after tomorrow)\b", 2, 0.85),
        (r"\b(aaj|today)\b", 0, 0.85),
        (r"\b(kal|tomorrow)\b", 1, 0.85),
    ):
        match = re.search(pattern, low)
        if match:
            return sent + timedelta(days=days), match.group(0), conf

    match = re.search(r"\b(agle\s*haft[ae]|next week)\b", low)
    if match:
        return sent + timedelta(days=7), match.group(0), 0.70
    match = re.search(r"\b(is\s*haft[ae]|this week)\b", low)
    if match:
        return _next_weekday(sent, 4), match.group(0), 0.60
    match = re.search(r"\b(month\s*end|mahine\s*ke\s*end)\b", low)
    if match:
        first_next = (sent.replace(day=1) + timedelta(days=32)).replace(day=1)
        return first_next - timedelta(days=1), match.group(0), 0.70

    for name, target in WEEKDAYS.items():
        match = re.search(rf"\b{name}\b", low)
        if match:
            return _next_weekday(sent, target), match.group(0), 0.80

    match = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:ko|tarikh|tareekh)\b", low)
    if match:
        day = int(match.group(1))
        if 1 <= day <= 31:
            year, month = sent.year, sent.month
            if day < sent.day:
                month += 1
                if month > 12:
                    month, year = 1, year + 1
            return date(year, month, min(day, _days_in_month(year, month))), match.group(0).strip(), 0.75
    return None


def _next_weekday(day: date, target: int) -> date:
    delta = (target - day.weekday()) % 7 or 7
    return day + timedelta(days=delta)


def _days_in_month(year: int, month: int) -> int:
    first_next = date(year + (month == 12), (month % 12) + 1, 1)
    return (first_next - timedelta(days=1)).day


def build_statement(category: str, obj: str | None, who: str, body: str, amount: int | None, payee: str | None) -> str:
    the_obj = f"the {obj}" if obj else "the promised item"
    if category == "payment":
        head = f"₹{amount:,} " if amount else "the "
        return f"Pay {head}pending balance" + (f" to {payee}" if payee else "")
    if category == "approval":
        return f"Get approval on {the_obj}"
    if category == "quote":
        return f"Send the quote / rate for {the_obj}"
    if category == "drawing":
        return f"Send {the_obj} to the client"
    if category == "installation":
        return f"Complete the {obj} installation" if obj else "Complete the promised installation work"
    if category in ("material", "delivery"):
        return f"Deliver {the_obj} to site"
    if category == "visit":
        return "Visit the site as promised"
    if category == "manpower":
        return "Arrange labour as promised"
    words = " ".join(body.split()[:8])
    return words[:1].upper() + words[1:]


def nick_name(who: str) -> str:
    parts = [p for p in re.split(r"\s+", who or "") if p]
    rest = [p for p in parts if not ROLE_PREFIX.match(p)]
    return (rest or parts or [who])[0]


def rule_extract(messages: list[dict], owner_name: str) -> dict:
    """The offline extractor. Mirrors prompts/01_extract_promises.md in code."""
    promises, done_events = [], []
    last_inbound = last_msg = None
    for message in messages:
        prev_inbound, prev_msg = last_inbound, last_msg
        if message["direction"] == "inbound":
            last_inbound = message
        last_msg = message

        body = message.get("transcript") or message.get("body") or ""
        if not body or body == "[media]":
            continue

        is_owner = bool(OWNER_HINTS.match(message.get("sender_raw_name") or ""))
        direction = "we_owe" if message["direction"] == "outbound" else "they_owe"
        who_role = "owner" if is_owner else "vendor"

        commit = next(((rx, w) for rx, w in COMMIT if rx.search(body)), None)
        due = parse_due(body, _as_date(message["sent_at"]))
        obj = detect_object(body)
        category = detect_category(body)

        if not obj:
            for candidate in (prev_msg, prev_inbound):
                if not candidate:
                    continue
                text = candidate.get("transcript") or candidate.get("body") or ""
                inherited = detect_object(text)
                if not inherited:
                    continue
                obj = inherited
                if category in ("delivery", "other"):
                    category = detect_category(text)
                break

        if QUESTION_ONLY.search(body.strip()):
            continue
        if DONE_HINT.search(body) and not due:
            done_events.append({"message": message, "obj": obj})
            continue
        if not commit:
            continue
        if direction == "we_owe" and category == "visit":
            continue

        confidence = 0.55 + commit[1] + (0.25 if due else 0)
        confidence = max(0.2, min(0.97, confidence))
        needs_clarification = due is None or confidence < 0.5
        payee = prev_inbound["sender_raw_name"] if direction == "we_owe" and prev_inbound else None

        promises.append({
            "source_message_id": message["id"],
            "who_raw": message.get("sender_raw_name"),
            "who_role": who_role,
            "contact_id": _contact_of(message),
            "direction": direction,
            "category": category,
            "object": obj,
            "statement": build_statement(category, obj, message.get("sender_raw_name") or "", body,
                                        detect_amount(body), payee),
            "source_quote": body,
            "due_on": due[0].isoformat() if due else None,
            "due_raw": due[1] if due else None,
            "due_confidence": due[2] if due else None,
            "confidence": round(confidence, 2),
            "needs_clarification": needs_clarification,
            "clarifying_question": None if not needs_clarification else (
                f"By when did {message.get('sender_raw_name')} say this would be done?"
                if who_role != "owner" else "By when did you promise this?"),
            "recipient_raw": payee,
            "raw": {"engine": "rules", "matched": commit[0].pattern if commit else None},
        })
    return {"promises": promises, "done_events": done_events}


def dedupe_key_for(promise: dict) -> str:
    """The identity the browser app de-duplicates on, written down.

    De-duplication used to live only in the app's memory: a promise's key was
    built at extraction time and thrown away on insert, so a row read back from
    the database could not be matched against the same promise said again. The
    key is recomputed from the columns rather than trusted from a model -
    "Imran said laminate Thursday" is the same commitment twice if these four
    fields agree, whatever the model wrote.
    """
    return "|".join([
        promise.get("who_role") or "unknown",
        promise.get("who_raw") or "",
        promise.get("category") or "other",
        promise.get("object") or "",
    ])


def _as_date(value: str) -> date:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


# ===========================================================================
# Extractor
# ===========================================================================

class Extractor:
    """Reads conversation and writes `promises` + `promise_events` rows."""

    def __init__(self, store: Store, cfg: Settings):
        self.store = store
        self.cfg = cfg
        self.client = client_for(cfg)

    def extract(self, business_id: str, messages: list[dict]) -> dict:
        if not messages:
            return {"promises": 0, "closed": 0, "engine": "none"}
        if self.client is None:
            result = rule_extract(messages, self.cfg.owner_name)
            engine = "rules"
            model = "mock"
        else:
            result = {"promises": self._extract_with_model(messages), "done_events": []}
            engine = self.cfg.mode
            model = f"{self.cfg.mode}:{self.cfg.model}"

        run_id = new_id("run")
        inserted = 0
        backfilled = 0
        for promise in result["promises"]:
            if self._backfill_date(business_id, promise):
                backfilled += 1
                continue
            if self._duplicate(business_id, promise):
                continue
            promise_id = new_id("pr")
            source = next((m for m in messages if m["id"] == promise.get("source_message_id")), None)
            self.store.insert("promises", {
                "id": promise_id,
                "business_id": business_id,
                "conversation_id": source["conversation_id"] if source else None,
                "source_message_id": promise.get("source_message_id"),
                "who_contact_id": promise.get("contact_id"),
                "who_raw": promise.get("who_raw"),
                "who_role": promise.get("who_role"),
                "direction": promise["direction"],
                "category": promise["category"],
                "object": promise.get("object"),
                "dedupe_key": dedupe_key_for(promise),
                "promised_at": (source or {}).get("sent_at") or now(),
                "statement": promise["statement"],
                "source_quote": promise.get("source_quote"),
                "due_on": promise.get("due_on"),
                "due_raw": promise.get("due_raw"),
                "due_confidence": promise.get("due_confidence"),
                "confidence": promise.get("confidence", 0.5),
                "status": "open",
                "needs_clarification": int(bool(promise.get("needs_clarification"))),
                "clarifying_question": promise.get("clarifying_question"),
                "created_at": now(),
                "updated_at": now(),
            })
            self.store.insert("promise_events", {
                "id": new_id("ev"), "promise_id": promise_id, "kind": "extracted",
                "detail": f"engine={engine}", "actor": f"agent:extractor:{model}", "at": now(),
            })
            inserted += 1

        closed = self._close(business_id, messages, result.get("done_events", []))
        self.store.insert("agent_runs", {
            "id": run_id, "business_id": business_id, "agent": "extractor", "model": model,
            "input_ref": f"{len(messages)} messages", "status": "ok",
            "output_ref": f"{inserted} promises", "started_at": now(), "finished_at": now(),
        })
        self.store.commit()
        return {"promises": inserted, "closed": closed, "backfilled": backfilled, "engine": engine}

    def _extract_with_model(self, messages: list[dict]) -> list[dict]:
        system = system_prompt("01_extract_promises.md")
        lines = "\n".join(
            f"[{m['id']}] {m['sent_at']} {m.get('sender_raw_name')} ({m['direction']})"
            f"{' [voice transcript]' if m.get('transcript') else ''}: {m.get('transcript') or m.get('body') or ''}"
            for m in messages
        )
        user = (
            f"TODAY: {date.today().isoformat()} ({self.cfg.timezone})\n"
            f"BUSINESS: {self.cfg.business_name}\n"
            f"OWNER: {self.cfg.owner_name}\n\nMESSAGES:\n{lines}\n\nExtract every commitment. Return JSON."
        )
        payload = parse_json(self.client.complete(system, user))
        rows = []
        for item in payload.get("promises", []):
            source = next((m for m in messages if m["id"] == item.get("source_message_id")), messages[-1])
            category = item.get("category") or detect_category(item.get("source_quote", ""))
            rows.append({
                "source_message_id": source["id"],
                "who_raw": item.get("who_raw") or source.get("sender_raw_name"),
                "who_role": item.get("who_role") or "vendor",
                "contact_id": _contact_of(source),
                "direction": item.get("direction") if item.get("direction") in ("we_owe", "they_owe") else "they_owe",
                "category": category,
                "statement": item.get("statement") or build_statement(category, None, "", source.get("body") or "", None, None),
                "source_quote": item.get("source_quote") or source.get("body"),
                "due_on": item.get("due_on"),
                "due_raw": item.get("due_raw"),
                "due_confidence": item.get("due_confidence"),
                "confidence": item.get("confidence", 0.6),
                "needs_clarification": bool(item.get("needs_clarification")) or not item.get("due_on"),
                "clarifying_question": item.get("clarifying_question"),
            })
        return rows

    def _backfill_date(self, business_id: str, promise: dict) -> bool:
        """Adopt a date onto the undated promise it was answering.

        Without this, "kis date tak?" followed by "kal tak" leaves the original
        promise forever undated and creates a near-duplicate beside it.
        """
        if not promise.get("due_on"):
            return False
        rows = self.store.select(
            "SELECT id FROM promises WHERE business_id = ? AND status = 'open' "
            "AND who_raw = ? AND category = ? AND due_on IS NULL",
            (business_id, promise.get("who_raw"), promise.get("category")),
        )
        if not rows:
            return False
        promise_id = rows[0]["id"]
        self.store.execute(
            "UPDATE promises SET due_on = ?, due_raw = ?, due_confidence = ?, "
            "needs_clarification = 0, clarifying_question = NULL, updated_at = ? WHERE id = ?",
            (promise.get("due_on"), promise.get("due_raw"), promise.get("due_confidence"),
             now(), promise_id),
        )
        self.store.insert("promise_events", {
            "id": new_id("ev"), "promise_id": promise_id, "kind": "clarified",
            "detail": f"answered with {promise.get('due_raw')} ({promise.get('due_on')})",
            "actor": "agent:extractor", "at": now(),
        })
        return True

    def _duplicate(self, business_id: str, promise: dict) -> bool:
        """Same person, same category, no new information, within two weeks."""
        # `IS ?` is SQLite-only; this form works on both engines.
        rows = self.store.select(
            "SELECT id FROM promises WHERE business_id = ? AND status = 'open' "
            "AND who_raw = ? AND category = ? "
            "AND (due_on = ? OR (due_on IS NULL AND ? IS NULL))",
            (business_id, promise.get("who_raw"), promise.get("category"),
             promise.get("due_on"), promise.get("due_on")),
        )
        return bool(rows)

    def _close(self, business_id: str, messages: list[dict], done_events: list[dict]) -> int:
        """A 'done / it arrived' message closes the promise it refers to."""
        candidates = list(done_events)
        for message in messages:
            body = message.get("transcript") or message.get("body") or ""
            if DONE_HINT.search(body):
                candidates.append({"message": message, "obj": detect_object(body)})
        seen, closed = set(), 0
        for event in candidates:
            message = event["message"]
            if message["id"] in seen:
                continue
            seen.add(message["id"])
            body = message.get("transcript") or message.get("body") or ""
            obj = event.get("obj") or detect_object(body)
            open_promises = self.store.select(
                "SELECT id, due_on, source_message_id, statement FROM promises "
                "WHERE business_id = ? AND status = 'open' AND who_contact_id = ?",
                (business_id, _contact_of(message)),
            )
            for promise in open_promises:
                if promise["source_message_id"] == message["id"]:
                    continue  # never close a promise with its own words
                # "laminate pahunch gaya" must not close the plywood promise
                # just because it is the oldest thing that vendor owes.
                if obj and obj not in (promise["statement"] or "").lower():
                    continue
                due = promise["due_on"]
                late = bool(due) and _as_date(message["sent_at"]) > _as_date(due)
                self.store.execute(
                    "UPDATE promises SET status = ?, closed_at = ?, closed_by = ?, updated_at = ? WHERE id = ?",
                    ("late" if late else "done", message["sent_at"], "agent:closer", now(), promise["id"]),
                )
                self.store.insert("proofs", {
                    "id": new_id("pf"), "promise_id": promise["id"], "message_id": message["id"],
                    "kind": "photo" if message.get("kind") == "image" else "text_claim",
                    "description": (message.get("body") or "")[:120], "verified_by": "agent",
                    "created_at": now(),
                })
                self.store.insert("promise_events", {
                    "id": new_id("ev"), "promise_id": promise["id"], "kind": "fulfilled",
                    "detail": f"closed as {'late' if late else 'done'}", "actor": "agent:closer", "at": now(),
                })
                closed += 1
                break
        return closed


# ===========================================================================
# Aging
# ===========================================================================


class Aging:
    """Closes promises that never arrived — without pretending they were kept.

    closed_at stays NULL because nothing was delivered, and closed_by records
    who decided. That distinction is what stops the scorer treating an
    abandoned promise as a late delivery.
    """

    def __init__(self, store: Store):
        self.store = store

    def sweep(self, business_id: str, today: date | None = None) -> int:
        today = today or date.today()
        rows = self.store.select(
            "SELECT id, due_on FROM promises "
            "WHERE business_id = ? AND status = 'open' AND due_on IS NOT NULL",
            (business_id,),
        )
        changed = 0
        for row in rows:
            overdue = (today - _as_date(row["due_on"])).days
            if overdue <= AUTO_MISS_DAYS:
                continue
            self.store.execute(
                "UPDATE promises SET status = 'missed', closed_by = 'agent:aging', "
                "updated_at = ? WHERE id = ?",
                (now(), row["id"]),
            )
            self.store.insert("promise_events", {
                "id": new_id("ev"), "promise_id": row["id"], "kind": "auto_missed",
                "detail": f"{overdue}d overdue, nothing delivered", "actor": "agent:aging",
                "at": now(),
            })
            changed += 1
        self.store.commit()
        return changed


# ===========================================================================
# Chaser
# ===========================================================================

class Chaser:
    """Drafts follow-ups. Draft-first by design: sending is a human decision."""

    MAX_ATTEMPTS = 3

    def __init__(self, store: Store, cfg: Settings):
        self.store = store
        self.cfg = cfg

    def plan(self, business_id: str, today: date | None = None) -> int:
        today = today or date.today()
        # A promise the aging pass gave up on still needs chasing — the older
        # it is, the more it matters. Only a human decision stops the loop.
        rows = self.store.select(
            "SELECT p.*, c.name AS contact_name FROM promises p "
            "LEFT JOIN contacts c ON c.id = p.who_contact_id "
            "WHERE p.business_id = ? AND p.due_on IS NOT NULL "
            "AND p.needs_clarification = 0 "
            "AND (p.status = 'open' OR (p.status = 'missed' AND p.closed_by = 'agent:aging'))",
            (business_id,),
        )
        created = 0
        for promise in rows:
            overdue_days = (today - _as_date(promise["due_on"])).days
            if overdue_days < 0:
                continue
            attempt = 3 if overdue_days >= 5 else 2 if overdue_days >= 3 else 1
            existing = self.store.select(
                "SELECT id FROM follow_ups WHERE promise_id = ?", (promise["id"],)
            )
            if len(existing) >= self.MAX_ATTEMPTS:
                continue
            if self.store.select(
                "SELECT id FROM follow_ups WHERE promise_id = ? AND attempt = ?",
                (promise["id"], attempt),
            ):
                continue
            tone = {1: "warm", 2: "neutral", 3: "firm"}[attempt]
            text = self._draft(promise, attempt, overdue_days)
            self.store.insert("follow_ups", {
                "id": new_id("fu"), "business_id": business_id, "promise_id": promise["id"],
                "attempt": attempt, "scheduled_for": today.isoformat(), "channel": "whatsapp",
                "tone": tone, "status": "queued", "draft_text": text, "created_at": now(),
            })
            created += 1
        self.store.commit()
        return created

    def _draft(self, promise: dict, attempt: int, overdue_days: int) -> str:
        who = promise.get("contact_name") or promise.get("who_raw") or "ji"
        statement = (promise["statement"] or "").lower()
        due = promise.get("due_raw") or "promise"
        if promise["direction"] == "we_owe":
            tail = ", sorry for the delay." if attempt > 1 else "."
            return f"{promise['statement']} — abhi tak pending hai. Main aaj shaam tak confirm kar deta hoon{tail}"
        if attempt == 1:
            return f"Hi {nick_name(who)}, {statement} {due} ka tha — aaj ho jayega?"
        if attempt == 2:
            return (f"Hi {nick_name(who)}, {statement} {due} ka tha, {overdue_days} din ho gaye. "
                    f"Client wait kar rahe hain. Aaj raat tak ho sakta hai ya nayi date bata dein?")
        project = promise.get("project_id") or "project"
        return (f"{nick_name(who)} ji, {statement} {due} ka tha — {overdue_days} din late. "
                f"{project} ka schedule hil raha hai. Aaj decide kar lein: kal tak ho jayega ya "
                f"hum dusra vendor dekh lein?")


# ===========================================================================
# Scorer
# ===========================================================================

BANDS = ((80, "strong"), (60, "ok"), (40, "watch"), (0, "risk"))


def _laplace(successes: int, total: int) -> float:
    """One kept, one missed as the prior: never let a single promise decide."""
    return (successes + 1) / (total + 2)


class Scorer:
    """Deterministic, versioned formula. A model never invents the number."""

    VERSION = "formula-v2"

    def __init__(self, store: Store):
        self.store = store

    def compute(self, business_id: str, as_of: date | None = None) -> list[dict]:
        as_of = as_of or date.today()
        vendors = self.store.select(
            "SELECT v.id AS vendor_id, v.contact_id AS contact_id, c.name AS name "
            "FROM vendors v JOIN contacts c ON c.id = v.contact_id "
            "WHERE v.business_id = ?",
            (business_id,),
        )
        # Proof is evidence, not a claim: an owner's attestation must not lift
        # a vendor's score. Only media, or a proof an agent inspected, counts.
        verified = {
            row["promise_id"] for row in self.store.select(
                "SELECT pr.promise_id FROM proofs pr "
                "JOIN promises p ON p.id = pr.promise_id "
                "WHERE p.business_id = ? AND (pr.kind IN "
                "('photo', 'video', 'receipt', 'delivery_note') OR pr.verified_by = 'agent')",
                (business_id,),
            )
        }
        results = []
        for vendor in vendors:
            promises = self.store.select(
                "SELECT * FROM promises WHERE business_id = ? AND who_contact_id = ?",
                (business_id, vendor["contact_id"]),
            )
            response_rate, responses = self._response_rate(business_id, vendor["contact_id"])
            scores = self._score(promises, as_of, verified, response_rate, responses)
            # One snapshot per vendor per day: replace rather than pile up.
            self.store.execute(
                "DELETE FROM vendor_score_snapshots WHERE vendor_id = ? AND as_of = ?",
                (vendor["vendor_id"], as_of.isoformat()),
            )
            self.store.insert("vendor_score_snapshots", {
                "id": new_id("snap"), "vendor_id": vendor["vendor_id"], "as_of": as_of.isoformat(),
                "promises_total": scores["total"], "promises_kept": scores["kept"],
                "promises_late": scores["late"], "promises_missed": scores["missed"],
                "on_time_rate": scores["on_time_rate"], "kept_rate": scores["kept_rate"],
                "avg_delay_days": scores["avg_slip"], "response_rate": response_rate,
                "proof_rate": scores["proof_rate"], "reliability": scores["reliability"],
                "band": scores["band"],
                # Binds to a TEXT column: a dict here raises on the first run.
                "breakdown": json.dumps(scores["breakdown"]),
                "computed_at": now(),
            })
            results.append({"name": vendor["name"], "vendor_id": vendor["vendor_id"], **scores})
        self.store.commit()
        return sorted(results, key=lambda r: (r["reliability"] is None, -(r["reliability"] or 0)))

    def _response_rate(self, business_id: str, contact_id: str) -> tuple[float | None, int]:
        """Follow-ups sent, and how many drew any reply.

        follow_ups deliberately has no `answered` column: whether a reply came
        is a fact about the message log, not about the draft.
        """
        sent = self.store.select(
            "SELECT f.id, f.sent_at FROM follow_ups f "
            "JOIN promises p ON p.id = f.promise_id "
            "WHERE f.business_id = ? AND p.who_contact_id = ? "
            "AND f.status = 'sent' AND f.sent_at IS NOT NULL",
            (business_id, contact_id),
        )
        if not sent:
            return None, 0
        answered = 0
        for row in sent:
            hit = self.store.select(
                "SELECT m.id FROM messages m WHERE m.sender_contact_id = ? "
                "AND m.direction = 'inbound' AND m.sent_at > ? LIMIT 1",
                (contact_id, row["sent_at"]),
            )
            if hit:
                answered += 1
        return round(answered / len(sent), 3), len(sent)

    def _score(self, promises: list[dict], as_of: date, verified: set,
               response_rate: float | None, responses: int) -> dict:
        done = [p for p in promises if p["status"] == "done"]
        late = [p for p in promises if p["status"] == "late"]
        missed = [p for p in promises if p["status"] == "missed"]
        kept_rows = done + late
        counted = len(kept_rows) + len(missed)
        open_count = len([p for p in promises if p["status"] == "open"])

        on_time = 0
        slips = []
        for promise in kept_rows:
            if promise.get("due_on") and promise.get("closed_at"):
                slip = (_as_date(promise["closed_at"]) - _as_date(promise["due_on"])).days
                slips.append(max(0, slip))
                if slip <= 0 and promise["status"] == "done":
                    on_time += 1
            elif promise["status"] == "done":
                on_time += 1
        avg_slip = sum(slips) / len(slips) if slips else 0.0

        if counted == 0:
            return {"total": len(promises), "kept": 0, "late": len(late), "missed": len(missed),
                    "open": open_count, "on_time_rate": None, "kept_rate": None, "proof_rate": None,
                    "response_rate": response_rate, "avg_slip": None, "reliability": None,
                    "band": "unscored", "measured": [],
                    "breakdown": {"reason": "nothing delivered or missed yet"}}

        kept_n = len(kept_rows)
        kept_rate = kept_n / counted
        on_time_rate = on_time / counted
        provisional = counted < 5
        if provisional:
            kept_rate = _laplace(kept_n, counted)
            on_time_rate = _laplace(on_time, counted)

        with_proof = len([p for p in kept_rows if p["id"] in verified])
        proof_rate = with_proof / kept_n if kept_n else 0.0

        # Never score a dimension we did not measure. No deliveries means no
        # proof rate; no sent follow-ups means no responsiveness. Renormalise
        # what is left instead of scoring a zero nobody earned.
        components = [("on_time", 0.45, on_time_rate), ("kept", 0.25, kept_rate)]
        if kept_n:
            components.append(("proof", 0.15, proof_rate))
        if responses:
            components.append(("response", 0.15, response_rate or 0.0))
        weight_sum = sum(w for _, w, _ in components)
        raw = sum(w * value for _, w, value in components) / weight_sum

        slip_penalty = min(avg_slip, 14) / 14
        reliability = max(0.0, min(100.0, 100 * raw * (1 - 0.20 * slip_penalty)))
        band = "provisional" if provisional else next(name for floor, name in BANDS if reliability >= floor)
        measured = [name for name, _, _ in components]

        return {
            "total": len(promises), "kept": kept_n, "late": len(late), "missed": len(missed),
            "open": open_count,
            "on_time_rate": round(on_time_rate, 3), "kept_rate": round(kept_rate, 3),
            "proof_rate": round(proof_rate, 3), "response_rate": response_rate,
            "avg_slip": round(avg_slip, 2),
            "reliability": round(reliability, 1), "band": band, "measured": measured,
            "breakdown": {
                "on_time": round(45 * on_time_rate, 1), "kept": round(25 * kept_rate, 1),
                "proof": round(15 * proof_rate, 1) if kept_n else None,
                "response": round(15 * (response_rate or 0.0), 1) if responses else None,
                "slip_penalty": round(0.20 * slip_penalty, 3),
                "measured": measured, "version": self.VERSION,
            },
        }


def run_pipeline(store: Store, cfg: Settings, business_id: str, messages: list[dict]) -> dict:
    """Extract -> age -> chase -> score. One call, in that order."""
    extractor = Extractor(store, cfg)
    result = extractor.extract(business_id, messages)
    aged = Aging(store).sweep(business_id)
    chased = Chaser(store, cfg).plan(business_id)
    scores = Scorer(store).compute(business_id)
    return {**result, "aged": aged, "follow_ups": chased, "scores": scores}
