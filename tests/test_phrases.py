import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from sticker_bot.bot import StickerBot
from sticker_bot.phrases import Phrases
from sticker_bot.state import Draft, Preferences


def test_phrase_limit_dedup_scope_and_atomic_batch(tmp_path):
    path = tmp_path / "bot.sqlite3"
    Preferences(path)
    phrases = Phrases(path)
    assert phrases.add(42, ["  Сейчас   подъеду ", "Сейчас подъеду", "Это база"]) == 2
    assert len(phrases.list(42)) == 2
    assert phrases.add(-100, ["Сейчас подъеду"]) == 1
    foreign = phrases.list(-100)[0]["id"]
    phrases.delete(42, foreign)
    assert phrases.phrase(-100, foreign) == "Сейчас подъеду"
    with pytest.raises(ValueError):
        phrases.add(42, [f"Фраза {index}" for index in range(9)])
    assert len(phrases.list(42)) == 2
    with pytest.raises(ValueError):
        phrases.add(42, ["Нормально", "Ш" * 81])
    assert len(Phrases(path).list(42)) == 2


def test_add_phrases_preview_apply_and_undo_normal_entrypoints(tmp_path):
    app = StickerBot(Preferences(tmp_path / "state.sqlite3"))
    message = NS(chat=NS(type="private"), text="", reply_text=AsyncMock(), reply_photo=AsyncMock(), reply_sticker=AsyncMock())
    user = NS(id=42)
    update = NS(effective_message=message, effective_user=user)
    context = NS(bot=NS(username="ExampleBot"))
    async def scenario():
        update.callback_query = NS(data="f:add", answer=AsyncMock())
        await app.phrase_callback(update, context)
        message.text = "Сейчас подъеду\nСаня всё решит\nЭто база"
        await app.text(update, context)
        assert len(app.phrases.list(42)) == 3
        assert not app.phrase_input.get(42)
        await app.show_preview(message, 42, Draft(Image.new("RGBA", (30, 50), "red"), "none"))
        draft = app.drafts.get(42)
        async def press(action):
            update.callback_query = NS(data=f"d:{draft.revision}:{action}", answer=AsyncMock())
            await app.callback(update, context)
        await press("phrases:0")
        assert message.reply_photo.await_count == 1
        phrase_id = app.phrases.list(42)[1]["id"]
        await press(f"phrase:{phrase_id}")
        assert app.drafts.get(42).caption == "Саня всё решит"
        await press("undo")
        assert app.drafts.get(42).caption == ""
        app.phrases.add(77, ["Чужая фраза"])
        await press(f"phrase:{app.phrases.list(77)[0]['id']}")
        assert app.drafts.get(42).caption == ""
    asyncio.run(scenario())
