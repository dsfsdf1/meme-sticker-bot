from contextlib import contextmanager, closing
from pathlib import Path
from secrets import token_urlsafe
import sqlite3


class Library:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS packs (
                    id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, invite TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS packs_owner ON packs(owner);
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, pack_id INTEGER NOT NULL, sender INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    FOREIGN KEY(pack_id) REFERENCES packs(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS pending_proposals ON proposals(pack_id, sender);
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def remember(self, owner: int, name: str, title: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO packs(owner, name, title) VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET title = excluded.title
                   WHERE owner = excluded.owner""", (owner, name, title),
            )

    def owned(self, owner: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM packs WHERE owner = ? ORDER BY id DESC", (owner,)).fetchall()

    def by_name(self, name: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM packs WHERE name = ?", (name,)).fetchone()

    def pack(self, pack_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM packs WHERE id = ?", (pack_id,)).fetchone()

    def invite(self, pack_id: int, owner: int):
        with self.connect() as db:
            db.execute(
                "UPDATE packs SET invite = ? WHERE id = ? AND owner = ? AND invite IS NULL",
                (token_urlsafe(18), pack_id, owner),
            )
            row = db.execute("SELECT invite FROM packs WHERE id = ? AND owner = ?", (pack_id, owner)).fetchone()
            return row["invite"] if row else None

    def invited(self, token: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM packs WHERE invite = ?", (token,)).fetchone()

    def can_propose(self, pack_id: int, sender: int) -> bool:
        with self.connect() as db:
            count, duplicate = db.execute(
                "SELECT count(*), max(sender = ?) FROM proposals WHERE pack_id = ?",
                (sender, pack_id),
            ).fetchone()
            return count < 20 and not duplicate

    def propose(self, pack_id: int, sender: int, file_id: str) -> int:
        with self.connect() as db:
            return db.execute(
                "INSERT INTO proposals(pack_id, sender, file_id) VALUES (?, ?, ?)", (pack_id, sender, file_id),
            ).lastrowid

    def pending(self, owner: int, proposal_id: int | None = None):
        with self.connect() as db:
            sql = """SELECT proposals.*, packs.name, packs.title FROM proposals JOIN packs
                     ON packs.id = proposals.pack_id WHERE packs.owner = ?"""
            if proposal_id is None:
                return db.execute(sql + " ORDER BY proposals.id LIMIT 10", (owner,)).fetchall()
            return db.execute(sql + " AND proposals.id = ?", (owner, proposal_id)).fetchone()

    def resolve(self, proposal_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
