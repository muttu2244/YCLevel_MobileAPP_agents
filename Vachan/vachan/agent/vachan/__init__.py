"""Vachan — the promise ledger for small service businesses.

The agent team in one paragraph: the **extractor** reads a chat or a voice-note
transcript and writes down who promised what, by when. The **chaser** drafts
follow-ups on the owner's behalf when a date passes. The **closer** decides
whether a photo or a message actually proves the promise was kept. The
**scorer** turns that history into a vendor reliability number.

Nothing here needs a cloud account: `python -m vachan demo` runs the whole
pipeline against a local SQLite file with a rule-based extractor.
"""

__version__ = "0.1.0"
