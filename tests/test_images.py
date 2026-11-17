from io import BytesIO
import os
import sys
from types import SimpleNamespace

from PIL import Image, ImageColor, ImageDraw
import pytest

from sticker_bot.images import ALPHA_CLEANUP, CAPTION_GAP, COLORS, MAX_BYTES, ImageError, cutout, render


@pytest.fixture
def subject():
    image = Image.new("RGBA", (130, 100))
    ImageDraw.Draw(image).ellipse((5, 4, 124, 95), fill="#802020")
    return image


@pytest.mark.parametrize("color", ["white", "pink", "yellow", "blue"])
def test_static_webp_with_transparency_and_outline(subject, color):
    data = render(subject, color)
    assert len(data) <= MAX_BYTES
    with Image.open(BytesIO(data)) as sticker:
        assert (sticker.format, sticker.size, sticker.mode) == ("WEBP", (512, 512), "RGBA")
        assert sticker.getpixel((0, 0))[3] == 0
        assert sticker.getpixel((256, 256))[3] == 255
        expected = ImageColor.getrgb(COLORS[color][1])
        assert any(
            alpha == 255 and max(abs(channel - target) for channel, target in zip((r, g, b), expected)) < 24
            for r, g, b, alpha in sticker.get_flattened_data()
        )


@pytest.mark.parametrize("size", [(320, 80), (80, 320)])
@pytest.mark.parametrize("caption", ["Это база", "Ш" * 80])
def test_caption_is_centered_directly_below_subject(size, caption):
    subject = Image.new("RGBA", size, "#802020")
    with Image.open(BytesIO(render(subject, "white", caption))) as sticker:
        alpha = sticker.getchannel("A")
        _, top, _, bottom = alpha.getbbox()
        first_gap = next(y for y in range(top, bottom) if alpha.crop((0, y, 512, y + 1)).getbbox() is None)
        label_top = next(y for y in range(first_gap, bottom) if alpha.crop((0, y, 512, y + 1)).getbbox())
        assert abs(label_top - first_gap - CAPTION_GAP) <= 2
        left, _, right, _ = alpha.crop((0, label_top, 512, bottom)).getbbox()
        assert abs(left + right - 512) <= 2
        assert 0 < left < right < 512
        assert 0 < top < bottom < 512


def test_alpha_cleanup_removes_low_confidence_halo():
    assert all(value == 0 for value in ALPHA_CLEANUP[:129])
    assert 0 < ALPHA_CLEANUP[192] < 255
    assert ALPHA_CLEANUP[224:] == [255] * 32


def test_close_view_removes_background_retained_by_first_pass(monkeypatch):
    data = BytesIO()
    Image.new("RGB", (100, 100), "#802020").save(data, "PNG")
    sizes = []

    def remove(photo, *, session, only_mask):
        sizes.append(photo.size)
        mask = Image.new("L", photo.size)
        bounds = (30, 25, 69, 74) if len(sizes) == 1 else (17, 8, 38, 57)
        ImageDraw.Draw(mask).rectangle(bounds, fill=255)
        return mask

    monkeypatch.setitem(sys.modules, "rembg", SimpleNamespace(remove=remove))
    monkeypatch.setattr("sticker_bot.images.model_session", lambda: object())
    subject = cutout(data.getvalue())
    assert sizes == [(100, 100), (56, 66)]
    assert subject.size == (22, 50)
    assert subject.getchannel("A").getextrema() == (255, 255)


def test_pixel_limit_before_segmentation(monkeypatch):
    monkeypatch.setattr("sticker_bot.images.MAX_PIXELS", 10)
    data = BytesIO()
    Image.new("RGB", (5, 5)).save(data, "PNG")
    with pytest.raises(ImageError, match="слишком большое"):
        cutout(data.getvalue())


@pytest.mark.skipif(os.getenv("TEST_REAL_MODEL") != "1", reason="Нужен ONNX-файл модели u2netp")
def test_real_background_removal():
    from skimage.data import astronaut

    data = BytesIO()
    Image.fromarray(astronaut()).save(data, "JPEG")
    subject = cutout(data.getvalue())
    minimum, maximum = subject.getchannel("A").getextrema()
    assert minimum <= 20
    assert maximum >= 235
    with Image.open(BytesIO(render(subject, "pink", "Ну привет!"))) as sticker:
        assert sticker.size == (512, 512)
        assert sticker.getpixel((0, 0))[3] == 0
