import tempfile
from pathlib import Path
from ledger import Ledger

with tempfile.TemporaryDirectory() as folder:
    ledger = Ledger(Path(folder) / "meeting.db")
    meeting = ledger.ingest("Synthetic planning", "We discussed a demo.\nACTION [Demo Owner]: Review the synthetic fixture | due=2026-11-05\nMaybe build more later.\n")
    action = ledger.actions(meeting)[0]
    ledger.review(action["id"], expected_revision=0, actor="demo-reviewer", reason="Confirmed in the synthetic meeting", state="accepted")
    print(ledger.export(meeting))
