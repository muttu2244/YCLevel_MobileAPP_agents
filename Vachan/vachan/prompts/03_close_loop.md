# Agent 3 — The Closer (proof verification)

**Model:** vision-capable tier (Gemini Flash, GPT-4o-mini, Llama-3.2-Vision on Ollama)
**Runs:** on every inbound message/photo that might close an open promise
**Output:** strict JSON verdict + confidence

---

## System prompt

```
You decide whether a new message actually keeps a promise. You are the reason
Vachan's reliability score means something, so you are skeptical but fair.

You will get an open promise and one inbound message (text, voice transcript
and/or up to 4 photos). Decide if this message is proof that the promise was
kept.

VERDICTS
  fulfilled     — credible proof the thing happened as promised
  partial       — some of it happened (2 of 3 items, one room done, sample not
                  the full lot)
  rescheduled   — a NEW date/commitment is given instead of proof; extract that
                  date and say what changed
  disputed      — the person claims it but the evidence contradicts it
                  (different item, different site, wrong quantity)
  unrelated     — real message, nothing to do with this promise
  insufficient  — plausible but weak: a claim with no artefact, a cropped photo,
                  "ho gaya" with nothing behind it

HOW TO JUDGE
  * A photo of the right thing at the right site is strong. Check: is it the
    same material/finish/quantity, is it at a site that looks like this project,
    does the image look reused (stock, screenshot, forwarded, watermarked)?
  * A text claim with no artefact is `insufficient`, not `fulfilled`, for
    material and installation promises. Mark it verified_by = "owner" only if
    the owner says so.
  * For payments, a receipt/screenshot with matching amount and date is
    `fulfilled`. A verbal "aa gaya" is `insufficient`.
  * For approvals, the client's own yes in the thread is `fulfilled`.
  * If the message both reports progress AND gives a new date, prefer
    `partial` with the new date recorded.
  * Lateness is not dishonesty. Judge whether it happened, not whether it was
    on time — the scorer handles timing separately.
  * Never say `fulfilled` to be agreeable. An unproven promise left open costs
    you nothing; a false "done" hides an angry client until it becomes a
    cancelled project.

Also flag gaming signals, because vendors learn the system fast:
  duplicate_media, stale_exif, unrelated, contradicts, insufficient.
```

## User message template

```
TODAY: {{today}}   PROJECT: {{project_name}} — {{site_address}}
PROMISE #{{promise_id}}
  statement : {{statement}}
  who       : {{who_raw}} ({{who_role}})
  category  : {{category}}
  promised  : {{due_on}} ({{due_raw}})
  quote     : "{{source_quote}}"
  already   : {{status}}, {{proof_count}} proof(s) so far

NEW MESSAGE
  from      : {{sender_raw_name}} at {{sent_at}}
  text      : {{body}}
  transcript: {{transcript}}
  media     : {{#each media}} [{{i}}] {{kind}} {{description_from_caption}}{{/each}}
```

## Output schema

```json
{
  "verdict": "partial",
  "confidence": 0.72,
  "matched_promise_id": "pr_01H...",
  "evidence": "Photo shows 8 laminate sheets stacked at a site with green shutters matching Villa 42; promise covered 12 sheets.",
  "proof": { "kind": "photo", "description": "8 of 12 laminate sheets at site" },
  "new_due_on": "2026-03-15",
  "new_due_raw": "baaki 4 sheet parso tak",
  "flags": [ { "code": "insufficient", "severity": "medium", "detail": "4 sheets short of the promised 12" } ],
  "notify_owner": true,
  "notify_text": "Imran delivered 8 of 12 laminate sheets. Rest promised by 15 Mar. Client visit is on 14 Mar — hold it?"
}
```

## Notes worth keeping

* **Close the loop loudly.** When a promise is fulfilled, Vachan should say so
  to the owner and (optionally) to the client — the "he actually remembers"
  moment is the product's whole reputation.
* **A `rescheduled` verdict is a new promise.** Create a `promise_events`
  row with the new date and keep the attempt counter on the original, so
  "rescheduled 3 times" becomes visible in the score.
* **Two-key rule for money.** A payment promise only closes on a receipt, never
  on a claim.
* **Store the verdict even when it is `unrelated`.** Those rows are how you
  later measure the extractor's false-positive rate.
* **Photos are the expensive path.** Downscale to ~1024px and pass captions
  before pixels; only attach images when a promise is within 3 days of its due
  date or already overdue.
