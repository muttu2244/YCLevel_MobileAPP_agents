# Agent 4 — The Scorer (vendor reliability)

**Model:** none for the arithmetic — compute the score in SQL/Python. Use a model
only for the *explanation* line and for reading the free-text signal column.
**Runs:** nightly per vendor, plus on every promise status change
**Output:** strict JSON snapshot, written to `vendor_score_snapshots`

---

## Formula (deterministic, versioned — never let a model invent the number)

```
kept_rate      = kept / (kept + late + missed)
on_time_rate   = on_time / (kept + late + missed)        # only counted promises
response_rate  = follow_ups_answered / follow_ups_sent
proof_rate     = kept_with_proof / kept
slip_penalty   = min(avg_slip_days, 14) / 14             # 0..1, saturating
reschedule_penalty = min(reschedules / 3, 1) * 0.10

reliability = 100 * (
      0.45 * on_time_rate
    + 0.25 * kept_rate
    + 0.15 * response_rate
    + 0.15 * proof_rate
)  * (1 - 0.20 * slip_penalty - reschedule_penalty)

SMALL SAMPLES — never let one promise define a person:
    when (kept + late + missed) < 5, apply Laplace smoothing with a
    one-kept / one-missed prior and label the band `provisional`:
        kept_rate    = (kept    + 1) / (counted + 2)
        on_time_rate = (on_time + 1) / (counted + 2)

RENORMALISE WHAT YOU COULD NOT MEASURE:
    if we have never sent this vendor a follow-up, response_rate is not
    information — drop that term and rescale the remaining weights to 1
    instead of silently scoring them 0. Same rule for proof_rate before any
    proof channel exists. Never put a number on a dimension with no data.

bands:  >=80 strong | 60-79 ok | 40-59 watch | <40 risk | <5 promises provisional
```

Weights are a starting point, not a truth: `on_time_rate` carries the most
weight because a vendor who delivers late but does deliver still costs the owner
client trust. Raise `proof_rate`'s weight for trades where the artefact matters
(fabrication, finishes) and lower it for advisory work (a quote, an approval).

## System prompt (for the explanation only)

```
You turn a vendor's reliability numbers into two short sentences the owner will
actually act on. You are blunt but not cruel, and you never invent facts.

Input: the numbers, the promise history, and the raw flags/notes.
Say: how reliable they are, what specifically drives the score up or down, and
one concrete thing to change next time (ask for proof, add a buffer day, use
them only for small jobs, keep them for finish work).

Do not mention the score formula, percentages to one decimal place, or the word
"algorithm". Write in the owner's language. Max 40 words.
```

## Output schema

```json
{
  "vendor_id": "vd_01H...",
  "as_of": "2026-03-20",
  "promises_total": 11,
  "promises_kept": 8,
  "promises_late": 2,
  "promises_missed": 1,
  "on_time_rate": 0.727,
  "kept_rate": 0.909,
  "avg_delay_days": 1.6,
  "response_rate": 0.8,
  "proof_rate": 0.75,
  "reschedules": 2,
  "measured": ["on_time", "kept", "response", "proof"],
  "reliability": 71.4,
  "band": "ok",
  "breakdown": {
    "on_time": 32.7, "kept": 22.7, "response": 12.0, "proof": 11.3,
    "slip_penalty": 0.023, "reschedule_penalty": 0.067
  },
  "explanation": "Kaam accha karte hain par 3 me se 1 baar late. Har delivery pe photo maang lijiye, tab score upar jayega.",
  "flags_seen": ["insufficient"]
}
```

## Notes worth keeping

* **Never show a score built on fewer than 5 promises** without saying so. One
  missed delivery must not brand a good carpenter — Laplace smoothing plus the
  `provisional` band handles that.
* **Two-sided scoring.** Score vendors *and* the owner's own `we_owe` promises.
  Being told "you keep 60% of your own promises" is uncomfortable and is the
  single most compelling retention hook in the product.
* **Snapshot, don't overwrite.** The trend line ("watch → ok over two months")
  is what lets an owner trust the number, and it is the artefact you need when a
  vendor disputes it.
* **Score-sharing is opt-in.** Publishing a vendor's score to other businesses
  is the network effect the plan wants, but it needs explicit
  `consents(scope='share_score')` rows first, and vendors should be able to see
  and contest their own number.
* **Hallucination guard:** the explanation prompt gets only the computed JSON.
  If the model's sentence contains a number not present in the input, discard
  it and re-render from the template — the score must stay auditable.
