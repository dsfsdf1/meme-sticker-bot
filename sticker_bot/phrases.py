from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup

from .images import CAPTION_LIMIT
from .navigation import home_button

MAX_PHRASES = 10


def normalize(text: str) -> str:
    return " ".join(text.split())


class Phrases:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS phrases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope INTEGER NOT NULL, text TEXT NOT NULL,
                    UNIQUE(scope, text)
                )
            """)

    def list(self, scope: int):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return db.execute("SELECT * FROM phrases WHERE scope = ? ORDER BY id", (scope,)).fetchall()

    def add(self, scope: int, texts: list[str]) -> int:
        texts = list(dict.fromkeys(normalize(text) for text in texts if normalize(text)))
        if not texts or any(len(text) > CAPTION_LIMIT for text in texts):
            raise ValueError(f"Каждая фраза должна содержать от 1 до {CAPTION_LIMIT} символов.")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = {row[0] for row in db.execute("SELECT text FROM phrases WHERE scope = ?", (scope,))}
            additions = [text for text in texts if text not in existing]
            if len(existing) + len(additions) > MAX_PHRASES:
                raise ValueError(f"В словаре помещается {MAX_PHRASES} фраз. Удалите лишние и повторите.")
            db.executemany("INSERT INTO phrases(scope, text) VALUES (?, ?)", [(scope, text) for text in additions])
            return len(additions)

    def phrase(self, scope: int, phrase_id: int):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT text FROM phrases WHERE scope = ? AND id = ?", (scope, phrase_id)).fetchone()
            return row[0] if row else None

    def delete(self, scope: int, phrase_id: int) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM phrases WHERE scope = ? AND id = ?", (scope, phrase_id))


def phrase_picker(phrases: Phrases, user_id: int, scope: int | None, prefix: str, back: str) -> InlineKeyboardMarkup:
    rows = phrases.list(user_id)
    if scope is not None and scope != user_id:
        rows = [*rows, *phrases.list(scope)]
    keys = [[Button(row["text"], callback_data=f"{prefix}phrase:{row['id']}")] for row in rows]
    keys.append([Button("Мои фразы", callback_data="phrases")])
    keys.append([Button("Назад к стикеру", callback_data=back), home_button()])
    return InlineKeyboardMarkup(keys)


def chosen_phrase(phrases: Phrases, user_id: int, scope: int | None, phrase_id: int):
    text = phrases.phrase(user_id, phrase_id)
    if text is None and scope is not None:
        text = phrases.phrase(scope, phrase_id)
    return text


async def show_variants(message, draft, phrases: Phrases, user_id: int, prefix: str, back: str, page: int = 0) -> None:
    import asyncio
    from dataclasses import replace
    from telegram import InputFile

    from .albums import Frame, contact_sheet
    from .images import render

    rows = list(phrases.list(user_id))
    if draft.phrase_scope is not None and draft.phrase_scope != user_id:
        rows.extend(phrases.list(draft.phrase_scope))
    if not rows:
        await message.reply_text(
            "Сохраните свои фразы — по одной в строке. Затем их можно выбирать под любым стикером.",
            reply_markup=phrase_picker(phrases, user_id, draft.phrase_scope, prefix, back),
        )
        return
    page = min(max(0, page), (len(rows) - 1) // 3)
    choices = rows[page * 3:page * 3 + 3]

    def build():
        frames = []
        for row in choices:
            variant = replace(draft, caption=row["text"])
            variant.preview = render(
                variant.subject, variant.color, variant.caption, variant.caption_position, variant.caption_gap,
            )
            frames.append(Frame(variant.subject, variant))
        return contact_sheet(frames)

    keys = [[Button(f"{index + 1}. {row['text']}", callback_data=f"{prefix}phrase:{row['id']}")]
            for index, row in enumerate(choices)]
    paging = []
    if page:
        paging.append(Button("← Ещё варианты", callback_data=f"{prefix}phrases:{page - 1}"))
    if (page + 1) * 3 < len(rows):
        paging.append(Button("Ещё варианты →", callback_data=f"{prefix}phrases:{page + 1}"))
    if paging:
        keys.append(paging)
    keys.extend([[Button("Мои фразы", callback_data="phrases")],
                 [Button("Назад к стикеру", callback_data=back), home_button()]])
    await message.reply_photo(
        InputFile(await asyncio.to_thread(build), filename="phrases.jpg"),
        caption="Выберите подпись. Номер соответствует варианту на превью.",
        reply_markup=InlineKeyboardMarkup(keys),
    )
