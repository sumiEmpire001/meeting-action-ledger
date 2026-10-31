# Meeting Action Ledger

Evidence-linked action extraction, human review, persistent edits and JSON/CSV exports. Standard-library Python 3.10+; deterministic and completely provider-free.

```sh
python -m unittest discover -s tests -v
python demo.py
```

The extraction protocol deliberately accepts only standalone, explicitly labelled lines:

```text
Synthetic discussion notes.
ACTION [Demo Owner]: Review the sample fixtures | due=2026-11-05
ACTION [Another Owner]: Prepare a demo
Maybe launch later.
```

Only the two ACTION lines become proposals. Dates must be valid YYYY-MM-DD; the due suffix is optional. Arbitrary prose, inferred promises and malformed dates produce no action. This baseline does not claim general natural-language understanding. Original line number, quote and Python Unicode character offsets are retained exactly, including when the source uses CRLF or non-ASCII text.

```python
from ledger import Ledger
ledger = Ledger('meetings.db')
meeting = ledger.ingest('Synthetic meeting', 'ACTION [Demo Owner]: Review sample\n')
proposal = ledger.actions(meeting)[0]
ledger.review(proposal['id'], expected_revision=0, actor='reviewer', reason='Confirmed', state='accepted', edits={'action': 'Review the revised sample'})
print(ledger.export(meeting))
print(ledger.history(proposal['id']))
```

Exports include accepted actions by default. `accepted_only=False` includes proposals and rejections. Human changes are explicitly marked `human_edited`; original extraction evidence remains separate and immutable through this API. Review revisions prevent stale/concurrent edits, and each change records before/after snapshots, actor and reason. Reopening a rejected action requires another revision. CSV protects leading formula-like cells and quotes multiline/comma content.

13 tests include exact evidence spans, unsupported-action rejection, extraction evaluation, Unicode/CRLF, required human acceptance, edits/history, concurrent reviews, restart, invalid inputs, source tampering and CSV round trips. The small labelled evaluation has two supported and eight unsupported cases with no errors; this measures the explicit format protocol only, not real-world transcript accuracy. GitHub Actions runs tests and the synthetic demo on Python 3.10/3.12/3.13. Actions → Tests and demo → Run workflow repeats them.

Limits: trusted local library, without authentication, meeting-level permissions, speaker recognition, a hosted UI or an LLM provider. Actor names are supplied by the caller and are not authenticated identities. Transcripts are stored in plaintext; use synthetic data for demos. Source hashes detect accidental changes, not an attacker who can rewrite both content and hashes. Up to 1 MiB transcript, 80-character owners and 500-character actions. Evidence offsets are characters, not UTF-8 byte offsets. Human-edited actions are reviewer assertions, not automatically verified claims. Back up the SQLite file under an appropriate transaction/snapshot procedure.
