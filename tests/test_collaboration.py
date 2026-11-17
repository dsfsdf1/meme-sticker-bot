import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image
from telegram import Bot, File
from telegram.error import BadRequest, NetworkError

from sticker_bot.bot import StickerBot
from sticker_bot.packs import new_pack_name, owned_packs, save_many
from sticker_bot.proposals import Suggestion
from sticker_bot.state import Draft, Preferences


def test_another_pack_keeps_legacy_and_is_listed_after_reopen(tmp_path):
    path = tmp_path / "bot.sqlite3"
    app = StickerBot(Preferences(path))
    bot = AsyncMock(spec=Bot)
    bot.username = "ExampleBot"
    bot.get_sticker_set.side_effect = [NS(title="Старый набор"), BadRequest("STICKERSET_INVALID")]
    bot.upload_sticker_file.return_value = File("file", "unique")
    draft = Draft(Image.new("RGBA", (1, 1)), "white", preview=b"preview")
    name = new_pack_name(42, bot.username)

    async def scenario():
        assert len(await owned_packs(bot, 42, app.library)) == 1
        title = await save_many(bot, 42, [draft], "Поездка", name=name)
        app.library.remember(42, name, title)
        other = StickerBot(Preferences(path))
        rows = await owned_packs(bot, 42, other.library)
        assert [(row["name"], row["title"]) for row in rows] == [(name, "Поездка"), ("m2a_by_examplebot", "Старый набор")]
        assert other.library.owned(99) == []

    asyncio.run(scenario())
    assert bot.create_new_sticker_set.call_args.kwargs["name"] == name


def test_invitation_approval_is_owner_only_and_safe_to_retry(tmp_path):
    app = StickerBot(Preferences(tmp_path / "bot.sqlite3"))
    library = app.library
    library.remember(42, "m2a_by_examplebot", "Наш набор")
    pack = library.owned(42)[0]
    assert library.invite(pack["id"], 77) is None
    token = library.invite(pack["id"], 42)
    assert library.invite(pack["id"], 42) == token

    bot = AsyncMock(spec=Bot)
    bot.add_sticker_to_set.side_effect = [NetworkError("temporary"), True]
    message = NS(chat=NS(type="private"), reply_text=AsyncMock())
    context = NS(bot=bot)

    async def press(user, data):
        query = NS(data=data, answer=AsyncMock())
        await app.proposals.callback(NS(effective_user=NS(id=user), effective_message=message, callback_query=query), context)
        return query

    async def scenario():
        await app.proposals.start(message, 77, token)
        assert app.proposals.targets.get(77) == pack["id"]
        app.proposals.previews.put(77, Suggestion(pack["id"], "uploaded", "0123456789"))
        await press(77, "g:send:0123456789")
        bot.send_sticker.assert_awaited_once()
        pending = library.pending(42)[0]
        action = f"g:approve:{pending['id']}"
        unauthorized = await press(77, action)
        assert unauthorized.answer.call_args.kwargs["show_alert"]
        bot.add_sticker_to_set.assert_not_awaited()
        await press(42, action)
        assert len(library.pending(42)) == 1
        await press(42, action)
        assert library.pending(42) == []
        later_id = library.propose(pack["id"], 77, "another")
        assert later_id != pending["id"]
        await press(42, action)
        assert library.pending(42, later_id)["file_id"] == "another"
        assert bot.add_sticker_to_set.await_count == 2
        assert all(call.kwargs["sticker"].sticker == "uploaded" for call in bot.add_sticker_to_set.await_args_list)

    asyncio.run(scenario())


def test_collection_posts_latest_count_below_photos(tmp_path):
    app = StickerBot(Preferences(tmp_path / "bot.sqlite3"))
    first = NS(photo=[NS(file_size=100)], reply_text=AsyncMock())
    last = NS(photo=[NS(file_size=100)], reply_text=AsyncMock())

    async def scenario():
        album = await app.albums.start(first, 42)
        await app.albums.collect(first, 42)
        old_task = album.progress_task
        await app.albums.collect(last, 42)
        await album.progress_task
        assert old_task.cancelled()
        args, kwargs = last.reply_text.call_args
        assert "2 из 8" in args[0]
        assert kwargs["reply_markup"].inline_keyboard[0][0].text == "Посмотреть превью"

    asyncio.run(scenario())
