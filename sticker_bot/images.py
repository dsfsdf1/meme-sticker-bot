from functools import lru_cache
from io import BytesIO
from pathlib import Path
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

SIZE = 512
MAX_BYTES = 512 * 1024
MAX_PIXELS = 20_000_000
CAPTION_LIMIT = 80
PADDING = 18
OUTLINE_RADIUS = 7
OUTLINE_PADDING = OUTLINE_RADIUS + 2
CAPTION_GAP = 8
ALPHA_CLEANUP = [
    max(0, min(255, round((value - 128) * 255 / (224 - 128))))
    for value in range(256)
]
COLORS = {
    "none": ("Без обводки", None),
    "white": ("Белая", "#ffffff"),
    "pink": ("Розовая", "#ff5ea8"),
    "yellow": ("Жёлтая", "#ffd84d"),
    "blue": ("Голубая", "#65dbff"),
}


class ImageError(ValueError):
    pass


@lru_cache(maxsize=1)
def model_session():
    from rembg import new_session

    return new_session("u2netp", providers=["CPUExecutionProvider"])


def open_photo(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
        if source.width * source.height > MAX_PIXELS:
            raise ImageError("Фото слишком большое. Попробуйте уменьшить его.")
        photo = ImageOps.exif_transpose(source).convert("RGB")
    photo.thumbnail((1500, 1500), Image.Resampling.LANCZOS)
    return photo


def photo_in_frame(photo: Image.Image) -> Image.Image:
    picture = ImageOps.contain(photo, (460, 460), Image.Resampling.LANCZOS)
    mask = Image.new("L", picture.size)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, picture.width - 1, picture.height - 1),
        radius=min(picture.size) // 9, fill=255,
    )
    picture.putalpha(mask)
    return picture


def cutout(data: bytes) -> Image.Image:
    return cutout_photo(open_photo(data))


def cutout_photo(photo: Image.Image) -> Image.Image:
    from rembg import remove

    session = model_session()
    mask = remove(photo, session=session, only_mask=True).point(ALPHA_CLEANUP)
    box = mask.getbbox()
    if box is None:
        raise ImageError("Не удалось выделить объект. Попробуйте фото с более чётким силуэтом.")

    # A closer view reduces the influence of distant background and decorative borders.
    left, top, right, bottom = box
    margin = round(max(right - left, bottom - top) * 0.16)
    view = (
        max(0, left - margin), max(0, top - margin),
        min(photo.width, right + margin), min(photo.height, bottom + margin),
    )
    view_area = (view[2] - view[0]) * (view[3] - view[1])
    mask = mask.crop(box)
    if view_area < photo.width * photo.height * 0.85:
        close_mask = remove(photo.crop(view), session=session, only_mask=True).point(ALPHA_CLEANUP)
        close_box = (left - view[0], top - view[1], right - view[0], bottom - view[1])
        refined = ImageChops.darker(mask, close_mask.crop(close_box))
        if refined.getbbox():
            mask = refined

    subject = photo.crop(box)
    subject.putalpha(mask)
    return subject.crop(mask.getbbox())


@lru_cache(maxsize=24)
def caption_font(size: int) -> ImageFont.FreeTypeFont:
    configured = os.getenv("STICKER_FONT")
    if configured:
        return ImageFont.truetype(configured, size)
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def caption_lines(text: str, font: ImageFont.FreeTypeFont) -> list[str]:
    lines = [""]
    for word in text.split():
        combined = f"{lines[-1]} {word}".strip()
        if font.getlength(combined) <= 456:
            lines[-1] = combined
            continue
        if lines[-1]:
            lines.append("")
        for character in word:
            if font.getlength(lines[-1] + character) > 456:
                lines.append("")
            lines[-1] += character
    return lines


def caption_layer(caption: str) -> Image.Image:
    for size in range(48, 13, -2):
        font = caption_font(size)
        text = "\n".join(caption_lines(caption, font))
        layer = Image.new("RGBA", (SIZE - 2 * PADDING, 115))
        draw = ImageDraw.Draw(layer)
        left, top, right, bottom = draw.multiline_textbbox(
            (0, 0), text, font=font, stroke_width=3, align="center", spacing=4,
        )
        if right - left <= layer.width and bottom - top <= layer.height:
            draw.multiline_text(
                (-left, -top), text, font=font, align="center", spacing=4,
                fill="white", stroke_width=3, stroke_fill="#16131a",
            )
            box = layer.getbbox()
            if box:
                return layer.crop(box)
    raise ImageError("Не удалось разместить подпись. Попробуйте сократить текст.")


def render(
    subject: Image.Image, color: str, caption: str = "",
    caption_position: str = "bottom", caption_gap: int = CAPTION_GAP,
) -> bytes:
    caption = " ".join(caption.split())
    label = caption_layer(caption) if caption else None
    label_height = label.height + caption_gap if label else 0
    available = SIZE - 2 * PADDING
    fitted = ImageOps.contain(
        subject, (available - 2 * OUTLINE_PADDING, available - 2 * OUTLINE_PADDING - label_height),
        Image.Resampling.LANCZOS,
    )
    outlined = Image.new(
        "RGBA", (fitted.width + 2 * OUTLINE_PADDING, fitted.height + 2 * OUTLINE_PADDING)
    )
    outlined.paste(fitted, (OUTLINE_PADDING, OUTLINE_PADDING))

    if color != "none":
        # Dilate a solid silhouette so faint pixels cannot turn into a wide outline.
        alpha = outlined.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
        outline = Image.new("RGBA", outlined.size, COLORS[color][1])
        expanded = alpha.filter(ImageFilter.MaxFilter(2 * OUTLINE_RADIUS + 1))
        outline.putalpha(expanded.filter(ImageFilter.GaussianBlur(0.55)))
        outlined = Image.alpha_composite(outline, outlined)
    box = outlined.getbbox()
    if box is None:
        raise ImageError("Не удалось выделить объект. Попробуйте другое фото.")
    outlined = outlined.crop(box)

    canvas = Image.new("RGBA", (SIZE, SIZE))
    top = (SIZE - outlined.height - label_height) // 2
    subject_top = top + label_height if label and caption_position == "top" else top
    canvas.paste(outlined, ((SIZE - outlined.width) // 2, subject_top))
    if label:
        label_top = top if caption_position == "top" else top + outlined.height + caption_gap
        canvas.paste(label, ((SIZE - label.width) // 2, label_top))

    for quality in (90, 75, 55, 35):
        output = BytesIO()
        canvas.save(output, "WEBP", quality=quality, method=4)
        if output.tell() <= MAX_BYTES:
            return output.getvalue()
    raise ImageError("Не удалось уложить стикер в лимит размера Telegram.")
