import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image
from telegram import Bot, PhotoSize
from telegram.ext import CallbackQueryHandler

from sticker_bot.albums import Album, Frame
from sticker_bot.bot import build_app
from sticker_bot.images import render
from sticker_bot.persistence import SavedDrafts
from sticker_bot.state import Draft, Preferences, record_edit, restore_edit


def test_sticker_survives_half_hour_eviction_and_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    Preferences(path)
    now = [0]
    store = SavedDrafts(path, "single", capacity=1, clock=lambda: now[0])
    photo = Image.new("RGBA", (30, 40), "red")
    original = Draft(photo, "white", "Это база", preview=render(photo, "white", "Это база"))
    edited = record_edit(original, replace(original, color="none", caption_position="top", caption_gap=24))
    store.put(42, edited)
    now[0] = 1801
    after_timeout = store.get(42)
    assert after_timeout.revision == original.revision
    assert after_timeout.subject.tobytes() == photo.tobytes()
    assert (after_timeout.color, after_timeout.caption_position, after_timeout.caption_gap) == ("none", "top", 24)
    store.put(99, original)
    assert 42 not in store.entries
    reopened = SavedDrafts(path, "single")
    restored = reopened.get(42)
    assert restored.revision == original.revision
    assert restore_edit(restored, "undo").color == "white"
    assert restored.subject is restored.undo[0].subject
    assert reopened.get(77) is None
    reopened.discard(42)
    assert SavedDrafts(path, "single").get(42) is None


def test_album_persists_originals_collected_files_and_edit_state(tmp_path):
    path = tmp_path / "state.sqlite3"
    Preferences(path)
    image = Image.new("RGBA", (20, 30), "blue")
    draft = Draft(image, "pink", caption="Привет", uploaded_id="uploaded")
    photo = PhotoSize("telegram-file", "unique", 20, 30, file_size=100)
    album = Album(
        frames=[Frame(image, draft)], photos=[photo], collecting=False,
        waiting="caption", active_frame=0, target="my_pack_by_examplebot",
        removed=[(0, Frame(image, replace(draft, color="none")))],
    )
    store = SavedDrafts(path, "albums")
    store.put(42, album)
    reopened = SavedDrafts(path, "albums")
    reopened.bot = Bot("123:TEST_ONLY")
    restored = reopened.get(42)
    assert restored.revision == album.revision
    assert restored.waiting == "caption"
    assert restored.progress_task is None
    assert restored.frames[0].draft.uploaded_id == "uploaded"
    assert restored.removed[0][1].draft.color == "none"
    assert restored.photos[0].get_bot() is reopened.bot
    assert restored.frames[0].photo.tobytes() == image.tobytes()


def test_real_handler_checkpoints_mutations_and_accepts_old_button_after_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    Preferences(path)
    original = Draft(Image.new("RGBA", (20, 30), "red"), "white", "Привет")
    SavedDrafts(path, "single").put(42, original)
    message = NS(chat=NS(type="private"), reply_text=AsyncMock(), reply_sticker=AsyncMock())
    user = NS(id=42)

    async def press(app, action):
        handler = next(
            handler for handler in app.handlers[0]
            if isinstance(handler, CallbackQueryHandler) and handler.pattern.match(f"d:{original.revision}:{action}")
        )
        query = NS(data=f"d:{original.revision}:{action}", answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        await handler.callback(
            NS(callback_query=query, effective_user=user, effective_message=message),
            NS(bot=NS(username="ExampleBot")),
        )
        assert not query.answer.call_args.kwargs.get("show_alert")

    async def scenario():
        first = build_app("123:TEST_ONLY", path)
        await press(first, "custom")
        assert SavedDrafts(path, "single").get(42).waiting_text
        restarted = build_app("123:TEST_ONLY", path)
        await press(restarted, "position:top")
        restored = SavedDrafts(path, "single").get(42)
        assert restored.caption_position == "top"
        assert not restored.waiting_text
        await press(build_app("123:TEST_ONLY", path), "undo")
        assert SavedDrafts(path, "single").get(42).caption_position == "bottom"

    asyncio.run(scenario())
