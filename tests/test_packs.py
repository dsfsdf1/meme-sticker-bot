import asyncio
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

from PIL import Image
from telegram import Bot, File
from telegram.error import BadRequest
import pytest

from sticker_bot.packs import find_pack, pack_name, save_to_pack, share_url
from sticker_bot.state import Draft


def test_create_then_add_reuses_uploaded_file():
    bot = AsyncMock(spec=Bot)
    bot.username = "ExampleBot"
    bot.upload_sticker_file.return_value = File("uploaded", "unique")
    bot.get_sticker_set.side_effect = [BadRequest("STICKERSET_INVALID"), object()]
    draft = Draft(Image.new("RGBA", (1, 1)), "white", preview=b"preview")

    async def scenario():
        assert await save_to_pack(bot, 42, draft) == "https://t.me/addstickers/m2a_by_examplebot"
        await save_to_pack(bot, 42, draft)

    asyncio.run(scenario())
    bot.upload_sticker_file.assert_awaited_once()
    assert bot.upload_sticker_file.call_args.kwargs["sticker_format"] == "static"
    create = bot.create_new_sticker_set.call_args.kwargs
    assert (create["user_id"], create["name"], create["sticker_type"]) == (42, "m2a_by_examplebot", "regular")
    assert create["stickers"][0].to_dict() == {
        "sticker": "uploaded", "emoji_list": ["😄"], "format": "static",
    }
    assert bot.add_sticker_to_set.call_args.kwargs["sticker"].to_dict() == create["stickers"][0].to_dict()


def test_other_api_errors_do_not_trigger_creation():
    bot = AsyncMock(spec=Bot)
    bot.get_sticker_set.side_effect = BadRequest("unexpected error")
    with pytest.raises(BadRequest, match="unexpected"):
        asyncio.run(find_pack(bot, "bad"))
    bot.create_new_sticker_set.assert_not_awaited()


def test_pack_name_and_share_encoding():
    name = pack_name(2**52 - 1, "A" * 29 + "Bot")
    assert len(name) <= 64
    assert name.endswith("_by_" + "a" * 29 + "bot")
    url = f"https://t.me/addstickers/{name}"
    assert parse_qs(urlsplit(share_url(url, "Мемы & фото")).query) == {"url": [url], "text": ["Мемы & фото"]}


def test_retry_after_failed_add_reuses_that_drafts_file():
    bot = AsyncMock(spec=Bot)
    bot.username = "ExampleBot"
    bot.upload_sticker_file.return_value = File("uploaded", "unique")
    bot.get_sticker_set.return_value = object()
    bot.add_sticker_to_set.side_effect = [BadRequest("temporary error"), True]
    draft = Draft(Image.new("RGBA", (1, 1)), "white", preview=b"preview")

    async def scenario():
        with pytest.raises(BadRequest, match="temporary"):
            await save_to_pack(bot, 42, draft)
        await save_to_pack(bot, 42, draft)

    asyncio.run(scenario())
    bot.upload_sticker_file.assert_awaited_once()
    assert bot.add_sticker_to_set.await_count == 2
    bot.create_new_sticker_set.assert_not_awaited()
