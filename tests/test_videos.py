import asyncio
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw, ImageFilter
import pytest

from sticker_bot.bot import StickerBot
from sticker_bot.images import ImageError
from sticker_bot.state import Preferences
from sticker_bot.videos import MAX_VIDEO_BYTES, Moment, extract_moments, frame_quality, select_moments, video_bytes


@pytest.fixture
def movie(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("Для интеграционного теста нужен FFmpeg")
    colors = ("#960c24", "#0a925a", "#135ac6", "#9a1dc4")
    for index, color in enumerate(colors):
        frame = Image.new("RGB", (160, 120), color)
        draw = ImageDraw.Draw(frame)
        draw.rectangle((15 + index * 10, 20, 100, 100), outline="white", width=5)
        draw.text((40, 50), str(index), fill="white")
        frame.save(tmp_path / f"frame-{index}.png")
    output = tmp_path / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-framerate", "1", "-i", str(tmp_path / "frame-%d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)],
        check=True, capture_output=True, timeout=20,
    )
    return output


def test_real_video_produces_distinct_ordered_moments(movie):
    moments = extract_moments(movie.read_bytes())
    assert len(moments) == 4
    assert [moment.seconds for moment in moments] == sorted(moment.seconds for moment in moments)
    assert all(0 <= moment.seconds <= 4 for moment in moments)
    assert len({moment.photo.getpixel((0, 0)) for moment in moments}) == 4


def test_duplicates_blur_and_black_frames_are_ranked():
    sharp = Image.new("RGB", (160, 120), "white")
    draw = ImageDraw.Draw(sharp)
    for x in range(0, 160, 8):
        draw.rectangle((x, 0, x + 3, 120), fill="black")
    blurred = sharp.filter(ImageFilter.GaussianBlur(5))
    assert frame_quality(sharp) > frame_quality(blurred) > frame_quality(Image.new("RGB", sharp.size))
    assert len(select_moments([Moment(sharp, 1, 10), Moment(sharp.copy(), 2, 20)])) == 1


def test_corrupt_video_is_rejected():
    with pytest.raises(ImageError, match="Не удалось прочитать"):
        extract_moments(b"not a video")


def test_video_limits_are_checked_before_download():
    media = NS(file_size=MAX_VIDEO_BYTES + 1, duration=2, get_file=AsyncMock())
    message = NS(video=media, video_note=None, animation=None, document=None)
    with pytest.raises(ImageError, match="20 МБ"):
        asyncio.run(video_bytes(message))
    media.get_file.assert_not_awaited()
    media.file_size = 100
    media.duration = 61
    with pytest.raises(ImageError, match="60 секунд"):
        asyncio.run(video_bytes(message))
    media.get_file.assert_not_awaited()


def test_video_selection_opens_existing_editor_and_saved_phrases(movie, tmp_path, monkeypatch):
    monkeypatch.setattr("sticker_bot.albums.cutout_photo", lambda photo: photo.convert("RGBA"))
    app = StickerBot(Preferences(tmp_path / "state.sqlite3"))
    app.phrases.add(42, ["Ну поехали"])
    file = NS(download_as_bytearray=AsyncMock(return_value=movie.read_bytes()))
    video = NS(file_size=movie.stat().st_size, duration=4, get_file=AsyncMock(return_value=file))
    status = NS(edit_text=AsyncMock())
    message = NS(
        chat=NS(type="private"), video=video, video_note=None, animation=None, document=None,
        reply_text=AsyncMock(return_value=status), reply_photo=AsyncMock(), reply_sticker=AsyncMock(),
    )
    user = NS(id=42)
    update = NS(effective_message=message, effective_user=user)
    context = NS(bot=NS(username="ExampleBot"))
    async def scenario():
        await app.video(update, context)
        album = app.albums.sessions.get(42)
        assert album.from_video and len(album.frames) == 4
        assert album.frames[0].seconds is not None
        async def press(action):
            update.callback_query = NS(data=f"a:{album.revision}:{action}", answer=AsyncMock())
            await app.albums.callback(update, context)
        await press("edit:1")
        await press("use")
        assert app.albums.sessions.get(42) is None
        assert app.drafts.get(42).subject.size == album.frames[1].draft.subject.size
        assert message.reply_photo.await_count == 2
    asyncio.run(scenario())
