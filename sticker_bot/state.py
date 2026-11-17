from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from time import monotonic
from typing import Generic, TypeVar
import sqlite3

from PIL import Image


class Preferences:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS preferences (user_id INTEGER PRIMARY KEY, color TEXT NOT NULL)")

    def color(self, user_id: int) -> str:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT color FROM preferences WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else "white"

    def set_color(self, user_id: int, color: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO preferences VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET color = excluded.color",
                (user_id, color),
            )


@dataclass
class Draft:
    subject: Image.Image
    color: str
    caption: str = ""
    waiting_text: bool = False
    revision: str = field(default_factory=lambda: token_hex(6))
    preview: bytes = b""

    uploaded_id: str | None = None


T = TypeVar("T")


class Drafts(Generic[T]):
    def __init__(self, capacity: int = 32, ttl: float = 1800, clock=monotonic):
        self.entries: OrderedDict[int, tuple[float, T]] = OrderedDict()
        self.capacity = capacity
        self.ttl = ttl
        self.clock = clock

    def prune(self) -> None:
        now = self.clock()
        while self.entries:
            user_id, (seen, _) = next(iter(self.entries.items()))
            if now - seen < self.ttl and len(self.entries) <= self.capacity:
                break
            del self.entries[user_id]

    def put(self, user_id: int, draft: T) -> None:
        self.entries[user_id] = (self.clock(), draft)
        self.entries.move_to_end(user_id)
        self.prune()

    def get(self, user_id: int) -> T | None:
        self.prune()
        if user_id not in self.entries:
            return None
        _, draft = self.entries[user_id]
        self.put(user_id, draft)
        return draft

    def discard(self, user_id: int) -> None:
        self.entries.pop(user_id, None)
