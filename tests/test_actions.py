import csv
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from ledger import Ledger, Conflict, extract


SOURCE = "Synthetic notes\nACTION [Ada]: Review fixtures | due=2026-11-05\nWe might deploy later.\nACTION [Bo]: Prepare demo\n"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "meeting.db"
        self.ledger = Ledger(self.path)
        self.meeting = self.ledger.ingest("Synthetic meeting", SOURCE)

    def first(self):
        return self.ledger.actions(self.meeting)[0]

    def review(self, **kwargs):
        args = dict(expected_revision=0, actor="reviewer", reason="Confirmed synthetic action", state="accepted")
        args.update(kwargs)
        return self.ledger.review(self.first()["id"], **args)

    def test_exact_spans_and_line_numbers(self):
        rows = extract(SOURCE)
        self.assertEqual([r["line"] for r in rows], [2, 4])
        for row in rows:
            self.assertEqual(SOURCE[row["start"]:row["end"]], row["quote"])
        self.assertEqual(rows[0]["due"], "2026-11-05")
        self.assertIsNone(rows[1]["due"])

    def test_labelled_extraction_evaluation(self):
        fixtures = [("ACTION [A]: Ship sample", True), ("ACTION [A]: Ship sample | due=2028-02-29", True),
                    ("Please maybe ship", False), ("A: I might ship", False), ("ACTION []: Ship", False),
                    ("ACTION [A]: Ship | due=2026-02-29", False), ("ACTION [A]: Ship | due=tomorrow", False),
                    ("ACTION [ ]: Ship", False), ("ACTION [A]:   ", False), ("Suggestion: ACTION [A]: Ship", False)]
        tp = fp = fn = 0
        for source, expected in fixtures:
            actual = bool(extract(source))
            tp += actual and expected
            fp += actual and not expected
            fn += not actual and expected
        self.assertEqual((tp, fp, fn), (2, 0, 0))

    def test_unicode_and_crlf_offsets(self):
        source = "Résumé 🎯\r\nACTION [Éva]: Review café\r\n"
        row = extract(source)[0]
        self.assertEqual(source[row["start"]:row["end"]], "ACTION [Éva]: Review café")
        self.assertEqual(row["line"], 2)

    def test_unsupported_prose_has_no_exported_actions(self):
        meeting = self.ledger.ingest("Discussion", "Maybe someone will do it.\nWe discussed a launch.")
        self.assertEqual(self.ledger.actions(meeting), [])
        self.assertEqual(json.loads(self.ledger.export(meeting)), [])

    def test_human_acceptance_required_for_default_export(self):
        self.assertEqual(json.loads(self.ledger.export(self.meeting)), [])
        self.review()
        rows = json.loads(self.ledger.export(self.meeting))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "accepted")
        self.assertFalse(rows[0]["human_edited"])

    def test_edits_preserve_source_and_audit(self):
        before = self.first()
        after = self.review(edits={"owner": "New Owner", "action": "Human revised task", "due": None})
        self.assertTrue(after["human_edited"])
        self.assertEqual(after["evidence"], before["evidence"])
        history = self.ledger.history(before["id"])
        self.assertEqual(history[0]["before"]["action"], "Review fixtures")
        self.assertEqual(history[0]["after"]["action"], "Human revised task")
        self.assertEqual(history[0]["actor"], "reviewer")

    def test_stale_and_concurrent_reviews(self):
        def attempt(_):
            try:
                self.review()
                return "accepted"
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=6) as pool:
            result = list(pool.map(attempt, range(12)))
        self.assertEqual(result.count("accepted"), 1)
        self.assertEqual(len(self.ledger.history(self.first()["id"])), 1)

    def test_reject_and_reopen_preserve_history(self):
        self.review(state="rejected")
        self.assertEqual(len(self.ledger.actions(self.meeting, state="rejected")), 1)
        self.review(expected_revision=1, state="proposed", reason="Needs another review")
        self.assertEqual(self.first()["revision"], 2)
        self.assertEqual(json.loads(self.ledger.export(self.meeting)), [])

    def test_invalid_edits_leave_state_unchanged(self):
        for edits in [{"action": ""}, {"due": "2026-13-01"}, {"due": "20260101"}, {"evidence": "fake"}, {"owner": "\x00"}]:
            with self.assertRaises(ValueError):
                self.review(edits=edits)
        with self.assertRaises(ValueError):
            self.review(actor="")
        self.assertEqual(self.first()["revision"], 0)
        self.assertEqual(self.ledger.history(self.first()["id"]), [])

    def test_restart_retains_reviews(self):
        self.review(edits={"action": "Keep this edit"})
        self.ledger = Ledger(self.path)
        self.assertEqual(self.first()["action"], "Keep this edit")
        self.assertEqual(len(self.ledger.history(self.first()["id"])), 1)

    def test_tampered_source_and_extraction_rejected(self):
        with self.ledger.db(True) as db:
            db.execute("UPDATE meetings SET transcript=transcript || 'tampered' WHERE id=?", (self.meeting,))
        with self.assertRaises(ValueError):
            self.ledger.export(self.meeting, accepted_only=False)
        with self.ledger.db(True) as db:
            db.execute("UPDATE meetings SET transcript=? WHERE id=?", (SOURCE, self.meeting))
            original = json.loads(db.execute("SELECT original FROM actions LIMIT 1").fetchone()[0])
            original["action"] = "Unsupported invented action"
            db.execute("UPDATE actions SET original=? WHERE id=?", (json.dumps(original), self.first_id(db)))
        with self.assertRaises(ValueError):
            self.ledger.actions(self.meeting)

    def first_id(self, db):
        return db.execute("SELECT id FROM actions ORDER BY id LIMIT 1").fetchone()[0]

    def test_csv_export_quotes_and_formula_safety(self):
        self.review(edits={"owner": "=DEMO()", "action": 'Review, then say "yes"'})
        rows = list(csv.DictReader(io.StringIO(self.ledger.export(self.meeting, format="csv"))))
        self.assertEqual(rows[0]["owner"], "'=DEMO()")
        self.assertEqual(rows[0]["action"], 'Review, then say "yes"')
        self.assertEqual(rows[0]["source_quote"], self.first()["evidence"]["quote"])

    def test_missing_ids_and_input_limits(self):
        with self.assertRaises(KeyError):
            self.ledger.actions(999)
        with self.assertRaises(KeyError):
            self.ledger.review(999, expected_revision=0, actor="a", reason="r", state="accepted")
        with self.assertRaises(ValueError):
            extract("x"*(1024*1024+1))
        with self.assertRaises(ValueError):
            self.ledger.export(self.meeting, format="xml")


if __name__ == "__main__":
    unittest.main()
