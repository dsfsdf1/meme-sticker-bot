import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image

from sticker_bot.bot import StickerBot, TEMPLATES
from sticker_bot.state import Draft, Drafts, Preferences


def test_preferences_persist_by_user(tmp_path):
    db = tmp_path / "sqlite" / "prefs.sqlite3"
    preferences = Preferences(db)
    assert preferences.color(42) == "white"
    preferences.set_color(42, "blue")
    preferences.set_color(42, "pink")
    reopened = Preferences(db)
    assert reopened.color(42) == "pink"
    assert reopened.color(43) == "white"


def test_drafts_expire_and_evict_oldest():
    now = [0]
    drafts = Drafts(capacity=1, ttl=10, clock=lambda: now[0])
    draft = Draft(Image.new("RGBA", (1, 1)), "white")
    drafts.put(1, draft)
    drafts.put(2, draft)
    assert drafts.get(1) is None
    assert drafts.get(2) is draft
    now[0] = 10
    assert drafts.get(2) is None


def test_photo_custom_caption_template_color_and_stale_buttons(tmp_path, monkeypatch):
    preferences = Preferences(tmp_path / "prefs.sqlite3")
    preferences.set_color(42, "blue")
    app = StickerBot(preferences)
    status = AsyncMock()
    file = NS(download_as_bytearray=AsyncMock(return_value=bytearray(b"photo")))
    photo = NS(file_size=5, get_file=AsyncMock(return_value=file))
    message = NS(chat=NS(type="private"), photo=[photo], text="", reply_text=AsyncMock(return_value=status), reply_sticker=AsyncMock())
    user = NS(id=42)
    monkeypatch.setattr("sticker_bot.bot.cutout", lambda data: Image.new("RGBA", (20, 30), "green"))

    async def press(action, draft):
        query = NS(data=f"d:{draft.revision}:{action}", answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        await app.callback(NS(callback_query=query, effective_user=user, effective_message=message), None)
        return query

    async def scenario():
        update = NS(effective_message=message, effective_user=user)
        await app.photo(update, None)
        first = app.drafts.get(42)
        assert first.color == "blue"
        assert first.preview.startswith(b"RIFF")
        await press("custom", first)
        message.text = "Ш" * 81
        await app.text(update, None)
        assert app.drafts.get(42) is first
        message.text = "  Ну   привет! "
        await app.text(update, None)
        second = app.drafts.get(42)
        assert second.caption == "Ну привет!"
        # An earlier menu still edits the same photo.
        await press("template:0", first)
        third = app.drafts.get(42)
        assert third.caption == TEMPLATES[0]
        await press("color:pink", third)
        assert app.drafts.get(42).color == "pink"
        assert preferences.color(42) == "blue"
        await app.photo(update, None)
        stale = await press("template:0", first)
        assert stale.answer.call_args.kwargs["show_alert"]

    asyncio.run(scenario())
    assert message.reply_sticker.await_count == 5
