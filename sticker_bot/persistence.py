"""Durable editor state. Cache eviction only releases memory."""
from base64 import b64decode, b64encode
from contextlib import closing
from dataclasses import fields, is_dataclass
from io import BytesIO
import json
from pathlib import Path
import sqlite3

from PIL import Image
from telegram import PhotoSize

from .state import Draft, Drafts


def encode(value) -> str:
    images = {}
    image_ids = {}

    def item(value):
        if isinstance(value, Image.Image):
            key = image_ids.get(id(value))
            if key is None:
                key = str(len(images))
                buffer = BytesIO()
                value.save(buffer, "PNG")
                images[key] = b64encode(buffer.getvalue()).decode("ascii")
                image_ids[id(value)] = key
            return {"image": key}
        if isinstance(value, bytes):
            return {"bytes": b64encode(value).decode("ascii")}
        if isinstance(value, PhotoSize):
            return {"photo": value.to_dict()}
        if is_dataclass(value):
            return {
                "type": type(value).__name__,
                "fields": {
                    field.name: item(getattr(value, field.name))
                    for field in fields(value) if field.name != "progress_task"
                },
            }
        if isinstance(value, tuple):
            return {"tuple": [item(entry) for entry in value]}
        if isinstance(value, list):
            return [item(entry) for entry in value]
        if value is None or isinstance(value, (str, int, bool, float)):
            return value
        raise TypeError(f"Unsupported editor state: {type(value).__name__}")

    payload = item(value)
    return json.dumps({"version": 1, "images": images, "value": payload}, ensure_ascii=False)


def decode(payload: str, bot=None):
    from .albums import Album, Frame
    from .proposals import Suggestion

    types = {"Draft": Draft, "Album": Album, "Frame": Frame, "Suggestion": Suggestion}
    document = json.loads(payload)
    if document["version"] != 1:
        raise ValueError("Unsupported editor state version")
    images = {}

    def item(value):
        if isinstance(value, list):
            return [item(entry) for entry in value]
        if not isinstance(value, dict):
            return value
        if "image" in value:
            key = value["image"]
            if key not in images:
                with Image.open(BytesIO(b64decode(document["images"][key]))) as image:
                    images[key] = image.copy()
            return images[key]
        if "bytes" in value:
            return b64decode(value["bytes"])
        if "photo" in value:
            return PhotoSize.de_json(value["photo"], bot)
        if "tuple" in value:
            return tuple(item(entry) for entry in value["tuple"])
        cls = types[value["type"]]
        return cls(**{key: item(entry) for key, entry in value["fields"].items()})

    return item(document["value"])


class SavedDrafts(Drafts):
    def __init__(self, path: Path, namespace: str, *, capacity: int = 32, ttl: float = 1800, **kwargs):
        super().__init__(capacity=capacity, ttl=ttl, **kwargs)
        self.path = path
        self.namespace = namespace
        self.bot = None
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS editor_state (
                    namespace TEXT NOT NULL, user_id INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(namespace, user_id)
                )
            """)

    def save_cached(self, user_id: int) -> None:
        entry = self.entries.get(user_id)
        if entry is None:
            return
        payload = encode(entry[1])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                """INSERT INTO editor_state VALUES (?, ?, ?)
                   ON CONFLICT(namespace, user_id) DO UPDATE SET payload = excluded.payload""",
                (self.namespace, user_id, payload),
            )

    def put(self, user_id: int, value) -> None:
        # Save before pruning, including when the memory cache has zero capacity.
        self.entries[user_id] = (self.clock(), value)
        self.save_cached(user_id)
        super().put(user_id, value)

    def get(self, user_id: int):
        self.prune()
        if user_id in self.entries:
            seen, value = self.entries[user_id]
            # Refresh the memory cache without rewriting images on every read.
            Drafts.put(self, user_id, value)
            return value
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT payload FROM editor_state WHERE namespace = ? AND user_id = ?",
                (self.namespace, user_id),
            ).fetchone()
        if row is None:
            return None
        value = decode(row[0], self.bot)
        Drafts.put(self, user_id, value)
        return value

    def discard(self, user_id: int) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "DELETE FROM editor_state WHERE namespace = ? AND user_id = ?",
                (self.namespace, user_id),
            )
        super().discard(user_id)
