import asyncio
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image

from sticker_bot.bot import StickerBot
from sticker_bot.state import Preferences


def group_setup(tmp_path):
    app = StickerBot(Preferences(tmp_path / "state.sqlite3"))
    message = NS(chat=NS(type="supergroup"), reply_to_message=None, reply_text=AsyncMock(), reply_sticker=AsyncMock())
    update = NS(effective_message=message, effective_chat=NS(id=-100), effective_user=NS(id=42))
    bot = NS(username="ExampleBot", get_chat_member=AsyncMock(return_value=NS(status="member")))
    context = NS(bot=bot, args=[])
    return app, message, update, context


def test_group_setup_requires_admin_and_phrase_deletion_checks_scope(tmp_path):
    app, message, update, context = group_setup(tmp_path)
    async def scenario():
        await app.groups.configure(update, context)
        assert not app.groups.enabled(-100)
        context.bot.get_chat_member.return_value = NS(status="administrator")
        await app.groups.configure(update, context)
        assert app.groups.enabled(-100)
        context.args = ["Саня", "всё", "решит"]
        await app.groups.add_phrase(update, context)
        assert app.phrases.list(-100)[0]["text"] == "Саня всё решит"
        context.args = [str(app.phrases.list(-100)[0]["id"])]
        context.bot.get_chat_member.return_value = NS(status="member")
        await app.groups.delete_phrase(update, context)
        assert len(app.phrases.list(-100)) == 1
        context.bot.get_chat_member.return_value = NS(status="creator")
        await app.groups.delete_phrase(update, context)
        assert not app.phrases.list(-100)
        context.args = ["off"]
        await app.groups.configure(update, context)
        assert not app.groups.enabled(-100)
    asyncio.run(scenario())


def test_group_reply_photo_context_and_author_only_private_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr("sticker_bot.albums.cutout_photo", lambda photo: photo.convert("RGBA"))
    app, message, update, context = group_setup(tmp_path)
    photo = Image.new("RGB", (70, 100), "red")
    buffer = BytesIO()
    photo.save(buffer, "PNG")
    media = NS(file_size=100, get_file=AsyncMock(return_value=NS(
        download_as_bytearray=AsyncMock(return_value=buffer.getvalue()),
    )))
    message.reply_to_message = NS(photo=[media], video=None, video_note=None, animation=None)
    async def scenario():
        context.bot.get_chat_member.return_value = NS(status="administrator")
        await app.groups.configure(update, context)
        app.phrases.add(-100, ["Саня всё решит"])
        await app.groups.meme(update, context)
        keys = message.reply_sticker.call_args.kwargs["reply_markup"].inline_keyboard
        deep_link = keys[-1][0].url
        token = deep_link.split("chat_")[1]
        capture = app.groups.capture(token)
        assert capture["chat_id"] == -100 and capture["creator"] == 42
        private = NS(reply_text=AsyncMock())
        assert await app.groups.enter(private, 77, token) is None
        draft = await app.groups.enter(private, 42, token)
        assert draft.phrase_scope == -100
        update.callback_query = NS(data=keys[0][0].callback_data, answer=AsyncMock())
        update.effective_user = NS(id=77)
        await app.groups.callback(update, context)
        assert update.callback_query.answer.call_args.kwargs["show_alert"]
        assert message.reply_sticker.await_count == 1
        update.effective_user = NS(id=42)
        await app.groups.callback(update, context)
        assert message.reply_sticker.await_count == 2
        restored = await app.groups.enter(private, 42, token)
        assert restored.caption == "Саня всё решит"
    asyncio.run(scenario())
