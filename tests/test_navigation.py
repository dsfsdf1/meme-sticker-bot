import asyncio
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from sticker_bot.albums import Album, Frame
from sticker_bot.bot import StickerBot
from sticker_bot.images import render
from sticker_bot.state import Draft, Preferences


def rows_alpha(data):
    with Image.open(BytesIO(data)) as image:
        alpha = image.getchannel("A")
        rows = []
        for y in range(512):
            box = alpha.crop((0, y, 512, y + 1)).getbbox()
            if box:
                rows.append((y, box[2] - box[0]))
        return rows


def test_none_outline_preserves_transparency_without_white_pixels():
    subject = Image.new("RGBA", (80, 80))
    subject.paste((128, 10, 20, 255), (20, 20, 60, 60))
    data = render(subject, "none")
    with Image.open(BytesIO(data)) as result:
        assert result.size == (512, 512)
        visible = [(r, g, b) for r, g, b, a in result.get_flattened_data() if a > 240]
        assert visible
        assert all(g < 50 and b < 60 for r, g, b in visible)
        assert result.getpixel((0, 0))[3] == 0
    assert data != render(subject, "white")


@pytest.mark.parametrize("position", ["top", "bottom"])
@pytest.mark.parametrize("gap", [8, 24])
def test_caption_position_and_gap_are_visible_in_output(position, gap):
    data = render(Image.new("RGBA", (240, 100), "#800020"), "none", "Это база", position, gap)
    rows = rows_alpha(data)
    breaks = [index for index in range(1, len(rows)) if rows[index][0] > rows[index - 1][0] + 1]
    assert len(breaks) == 1
    boundary = breaks[0]
    assert rows[boundary][0] - rows[boundary - 1][0] - 1 == gap
    first, second = rows[:boundary], rows[boundary:]
    # The wide red rectangle is the photo, the narrower group is the label.
    assert (max(width for _, width in first) < max(width for _, width in second)) == (position == "top")


def test_single_sticker_back_undo_redo_and_cancel_name(tmp_path):
    app = StickerBot(Preferences(tmp_path / "state.sqlite3"))
    message = NS(chat=NS(type="private"), reply_text=AsyncMock(), reply_sticker=AsyncMock())
    update = NS(effective_message=message, effective_user=NS(id=42))
    context = NS(bot=NS(username="ExampleBot"))

    async def press(action):
        draft = app.drafts.get(42)
        update.callback_query = NS(data=f"d:{draft.revision}:{action}", answer=AsyncMock())
        await app.callback(update, context)

    async def scenario():
        original = Draft(Image.new("RGBA", (70, 100), "#900000"), "white", "Это база")
        await app.show_preview(message, 42, original)
        await press("color:none")
        assert app.drafts.get(42).color == "none"
        await press("undo")
        assert app.drafts.get(42).color == "white"
        await press("redo")
        assert app.drafts.get(42).color == "none"
        await press("position:top")
        await press("gap:24")
        await press("undo")
        assert app.drafts.get(42).caption_gap == 8
        await press("custom")
        assert app.drafts.get(42).waiting_text
        await press("captions")
        assert not app.drafts.get(42).waiting_text
        await press("back")
        await press("new")
        assert app.naming.get(42)
        await press("back")
        assert app.naming.get(42) is None
        assert app.drafts.get(42).caption == "Это база"
        await press("color:pink")
        assert not app.drafts.get(42).redo
        await app.home(update, context)
        assert app.drafts.get(42).color == "pink"
        callbacks = [b.callback_data for row in message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        assert "resume" in callbacks

    asyncio.run(scenario())


def test_album_navigation_history_restore_and_cancel_input(tmp_path):
    app = StickerBot(Preferences(tmp_path / "state.sqlite3"))
    message = NS(chat=NS(type="private"), reply_text=AsyncMock(), reply_sticker=AsyncMock(), reply_photo=AsyncMock())
    update = NS(effective_message=message, effective_user=NS(id=42))
    subject = Image.new("RGBA", (90, 50), "#900000")
    first = Draft(subject, "white", "Это база", preview=render(subject, "white", "Это база"))
    album = Album(collecting=False, frames=[Frame(subject, first), Frame(subject, replace(first))])
    app.albums.sessions.put(42, album)

    async def press(action):
        update.callback_query = NS(data=f"a:{album.revision}:{action}", answer=AsyncMock())
        await app.albums.callback(update, NS(bot=NS(username="ExampleBot")))

    async def scenario():
        await press("edit:0")
        await press("color:none")
        await press("position:top")
        await press("undo")
        assert album.frames[0].draft.caption_position == "bottom"
        await press("redo")
        assert album.frames[0].draft.caption_position == "top"
        await press("caption")
        assert album.waiting == "caption"
        await press("back")
        assert album.waiting is None
        await press("edit:1")
        assert album.frames[1].draft.color == "white"
        await press("drop")
        assert len(album.frames) == 1
        await press("restore")
        assert len(album.frames) == 2
        await press("new")
        assert album.waiting == "title"
        await press("overview")
        assert album.waiting is None
        await press("cancel")
        assert app.albums.sessions.get(42) is album
        await press("resume")
        assert len(album.frames) == 2

    asyncio.run(scenario())
