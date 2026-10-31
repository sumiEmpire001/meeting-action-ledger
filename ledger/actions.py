import csv
import hashlib
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date


class Conflict(ValueError):
    pass


PATTERN = re.compile(r"ACTION \[([^\]\r\n]{1,80})\]: (.{1,500}?)(?: \| due=(\d{4}-\d{2}-\d{2}))?\Z")


def extract(transcript):
    if not isinstance(transcript, str) or len(transcript.encode()) > 1024*1024:
        raise ValueError("Transcript must be text up to 1 MiB")
    result, offset = [], 0
    for number, raw in enumerate(transcript.splitlines(keepends=True), 1):
        line = raw.rstrip("\r\n")
        match = PATTERN.fullmatch(line)
        if match:
            owner, action, due = match.groups()
            if owner.strip() and action.strip() and " | due=" not in action:
                try:
                    if due:
                        date.fromisoformat(due)
                except ValueError:
                    pass
                else:
                    result.append({"owner": owner.strip(), "action": action.strip(), "due": due,
                                   "start": offset, "end": offset+len(line), "line": number, "quote": line})
        offset += len(raw)
    return result


def text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field} must be nonempty text up to {maximum} characters")
    return value.strip()


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS meetings(
                id INTEGER PRIMARY KEY,title TEXT NOT NULL,transcript TEXT NOT NULL,digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions(
                id INTEGER PRIMARY KEY,meeting INTEGER NOT NULL REFERENCES meetings(id),
                original TEXT NOT NULL,owner TEXT NOT NULL,action TEXT NOT NULL,due TEXT,
                state TEXT NOT NULL,revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews(
                id INTEGER PRIMARY KEY,action_id INTEGER NOT NULL REFERENCES actions(id),
                revision INTEGER NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL);""")

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ingest(self, title, transcript):
        title = text(title, "Title", 200)
        candidates = extract(transcript)
        with self.db(True) as db:
            meeting = db.execute("INSERT INTO meetings(title,transcript,digest) VALUES(?,?,?)", (title, transcript, hashlib.sha256(transcript.encode()).hexdigest())).lastrowid
            for item in candidates:
                db.execute("INSERT INTO actions(meeting,original,owner,action,due,state,revision) VALUES(?,?,?,?,?,'proposed',0)", (meeting, json.dumps(item), item["owner"], item["action"], item["due"]))
            return meeting

    def get(self, db, action):
        row = db.execute("SELECT * FROM actions WHERE id=?", (action,)).fetchone()
        if row is None:
            raise KeyError(action)
        return dict(row)

    def verify(self, db, row):
        meeting = db.execute("SELECT * FROM meetings WHERE id=?", (row["meeting"],)).fetchone()
        if hashlib.sha256(meeting["transcript"].encode()).hexdigest() != meeting["digest"]:
            raise ValueError("Transcript integrity check failed")
        original = json.loads(row["original"])
        if original not in extract(meeting["transcript"]):
            raise ValueError("Action has unsupported extraction evidence")
        row["evidence"] = original
        row["human_edited"] = any(row[key] != original[key] for key in ("owner", "action", "due"))
        del row["original"]
        return row

    def actions(self, meeting, *, state=None):
        if state not in (None, "proposed", "accepted", "rejected"):
            raise ValueError("Unknown review state")
        with self.db() as db:
            if not db.execute("SELECT id FROM meetings WHERE id=?", (meeting,)).fetchone():
                raise KeyError(meeting)
            rows = db.execute("SELECT * FROM actions WHERE meeting=? ORDER BY id", (meeting,)).fetchall()
            return [self.verify(db, dict(row)) for row in rows if state is None or row["state"] == state]

    def review(self, action, *, expected_revision, actor, reason, state, edits=None):
        actor, reason = text(actor, "Actor", 80), text(reason, "Reason", 500)
        if type(expected_revision) is not int or expected_revision < 0 or state not in ("proposed", "accepted", "rejected"):
            raise ValueError("Invalid revision or state")
        if edits is None:
            edits = {}
        if not isinstance(edits, dict) or set(edits) - {"owner", "action", "due"}:
            raise ValueError("Only owner, action and due may be edited")
        edits = dict(edits)
        for key, maximum in (("owner", 80), ("action", 500)):
            if key in edits:
                edits[key] = text(edits[key], key, maximum)
        if "due" in edits and edits["due"] is not None:
            due = edits["due"]
            if not isinstance(due, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
                raise ValueError("Due must be YYYY-MM-DD or null")
            date.fromisoformat(due)
        with self.db(True) as db:
            before = self.verify(db, self.get(db, action))
            if before["revision"] != expected_revision:
                raise Conflict("Review revision changed")
            after = {**before, **edits, "state": state, "revision": expected_revision+1}
            original = before["evidence"]
            after["human_edited"] = any(after[k] != original[k] for k in ("owner", "action", "due"))
            db.execute("UPDATE actions SET owner=?,action=?,due=?,state=?,revision=? WHERE id=?", (after["owner"], after["action"], after["due"], state, after["revision"], action))
            db.execute("INSERT INTO reviews(action_id,revision,actor,reason,before_json,after_json) VALUES(?,?,?,?,?,?)", (action, after["revision"], actor, reason, json.dumps(before), json.dumps(after)))
            return after

    def history(self, action):
        with self.db() as db:
            self.verify(db, self.get(db, action))
            return [{**dict(row), "before": json.loads(row["before_json"]), "after": json.loads(row["after_json"])} for row in db.execute("SELECT * FROM reviews WHERE action_id=? ORDER BY revision", (action,))]

    def export(self, meeting, *, format="json", accepted_only=True):
        if type(accepted_only) is not bool:
            raise ValueError("accepted_only must be boolean")
        rows = self.actions(meeting, state="accepted" if accepted_only else None)
        if format == "json":
            return json.dumps(rows, ensure_ascii=False, indent=2)
        if format != "csv":
            raise ValueError("Export format must be json or csv")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["id", "owner", "action", "due", "state", "human_edited", "source_line", "source_quote"])
        def safe(value):
            value = str(value if value is not None else "")
            return "'"+value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value
        for row in rows:
            writer.writerow([safe(v) for v in [row["id"], row["owner"], row["action"], row["due"], row["state"], row["human_edited"], row["evidence"]["line"], row["evidence"]["quote"]]])
        return output.getvalue()
