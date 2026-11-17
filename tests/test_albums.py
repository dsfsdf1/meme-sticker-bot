import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from telegram import Bot, File
from telegram.error import BadRequest, NetworkError

from sticker_bot.albums import MAX_PHOTOS
from sticker_bot.bot import StickerBot
from sticker_bot.state import Preferences


def test_album_edit_remove_and_create(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("sticker_bot.albums.cutout_photo", lambda photo: photo.convert("RGBA"))
    raw = BytesIO()
    Image.new("RGB", (90, 60), "#bd7048").save(raw, "PNG")
    photo = SimpleNamespace(file_size=100, get_file=AsyncMock(return_value=SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=raw.getvalue()),
    )))
    message = SimpleNamespace(
        photo=[photo], media_group_id="album", chat=SimpleNamespace(type="private"),
        reply_text=AsyncMock(), reply_photo=AsyncMock(), reply_sticker=AsyncMock(),
    )
    user = SimpleNamespace(id=42)
    update = SimpleNamespace(effective_message=message, effective_user=user, callback_query=None)
    bot = AsyncMock(spec=Bot)
    bot.username = "ExampleBot"
    bot.get_sticker_set.side_effect = BadRequest("STICKERSET_INVALID")
    bot.upload_sticker_file.side_effect = [File("file1", "unique1"), File("file2", "unique2")]
    context = SimpleNamespace(bot=bot)
    app = StickerBot(Preferences(tmp_path / "bot.sqlite3"))

    async def button(action, revision=None):
        album = app.albums.sessions.get(user.id)
        update.callback_query = SimpleNamespace(
            data=f"a:{revision or album.revision}:{action}", answer=AsyncMock(),
        )
        await app.albums.callback(update, context)

    async def scenario():
        for _ in range(3):
            await app.photo(update, context)
        album = app.albums.sessions.get(user.id)
        key = album.revision
        assert len(album.photos) == 3
        await button("review")
        assert len(album.frames) == 3
        sheet = message.reply_photo.call_args.args[0]
        with Image.open(BytesIO(sheet.input_file_content)) as image:
            assert image.size == (624, 208)
        await button("edit:0")
        await button("frame")
        await button("caption")
        message.text = "Устроим собрание?"
        await app.text(update, context)
        assert album.frames[0].draft.caption == "Устроим собрание?"
        assert album.frames[0].draft.subject.getpixel((0, 0))[3] == 0
        await button("edit:1", revision=key)
        assert update.callback_query.answer.call_args.kwargs["show_alert"]
        await button("overview")
        await button("edit:1")
        await button("drop")
        assert len(album.frames) == 2
        await button("save")
        await button("new")
        message.text = "Наши выходные"
        await app.text(update, context)
        assert app.albums.sessions.get(user.id) is None
        sent = message.reply_text.call_args
        assert sent.args[0] == "Ваш набор «Наши выходные»"
        url = sent.kwargs["reply_markup"].inline_keyboard[0][0].url
        assert url.startswith("https://t.me/addstickers/m2a_") and url.endswith("_by_examplebot")
        assert len(app.library.owned(42)) == 1

    asyncio.run(scenario())
    assert bot.create_new_sticker_set.call_args.kwargs["title"] == "Наши выходные"
    assert len(bot.create_new_sticker_set.call_args.kwargs["stickers"]) == 2


def test_batch_limit_and_retry_reuses_ids(tmp_path):
    from sticker_bot.albums import Album, Frame
    from sticker_bot.state import Draft

    app = StickerBot(Preferences(tmp_path / "state.sqlite3"))
    message = SimpleNamespace(photo=[SimpleNamespace(file_size=100)], reply_text=AsyncMock())
    bot = AsyncMock(spec=Bot)
    bot.username = "ExampleBot"
    bot.get_sticker_set.return_value = SimpleNamespace(title="Старый набор")
    bot.upload_sticker_file.side_effect = [File("file1", "unique1"), File("file2", "unique2")]
    bot.add_sticker_to_set.side_effect = [True, NetworkError("temporary"), True, True]
    user_id = 42

    async def scenario():
        album = await app.albums.start(message, user_id)
        for _ in range(MAX_PHOTOS + 2):
            assert await app.albums.collect(message, user_id)
        assert len(album.photos) == MAX_PHOTOS
        image = Image.new("RGBA", (10, 10), "blue")
        album = Album(collecting=False, frames=[Frame(image, Draft(image, "white", preview=b"webp")) for _ in range(2)])
        app.albums.sessions.put(user_id, album)
        await app.albums.save(message, user_id, bot, album, "Старый набор")
        assert app.albums.sessions.get(user_id) is album
        await app.albums.save(message, user_id, bot, album, "Старый набор")
        assert app.albums.sessions.get(user_id) is None

    asyncio.run(scenario())
    assert bot.upload_sticker_file.await_count == 2
    assert [call.kwargs["sticker"].sticker for call in bot.add_sticker_to_set.await_args_list] == [
        "file1", "file2", "file1", "file2",
    ]


def test_remove_checks_pack_ownership_and_uses_current_file_id(tmp_path):
    app = StickerBot(Preferences(tmp_path / "state.sqlite3"))
    bot = AsyncMock(spec=Bot)
    bot.username = "ExampleBot"
    bot.get_sticker_set.return_value = SimpleNamespace(stickers=[
        SimpleNamespace(file_unique_id="chosen", file_id="current-id"),
        SimpleNamespace(file_unique_id="other", file_id="other-id"),
    ])
    chosen = SimpleNamespace(set_name="another_pack", file_unique_id="chosen")
    message = SimpleNamespace(reply_to_message=SimpleNamespace(sticker=chosen), reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42))

    async def scenario():
        await app.remove_sticker(update, SimpleNamespace(bot=bot))
        bot.delete_sticker_from_set.assert_not_awaited()
        chosen.set_name = "m2a_by_examplebot"
        await app.remove_sticker(update, SimpleNamespace(bot=bot))
        bot.delete_sticker_from_set.assert_awaited_once_with(sticker="current-id")

    asyncio.run(scenario())
