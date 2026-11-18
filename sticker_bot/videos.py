from dataclasses import dataclass
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops, ImageFilter, ImageStat

from .images import ImageError

MAX_VIDEO_BYTES = 20 * 1024 * 1024
MAX_DURATION = 60
SAMPLE_COUNT = 16
MOMENT_COUNT = 4


@dataclass
class Moment:
    photo: Image.Image
    seconds: float
    quality: float


def video_media(message):
    return message.video or message.video_note or message.animation or message.document


def run_media(command: list[str], *, timeout: int):
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ImageError("Обработка видео заняла слишком долго. Попробуйте более короткий ролик.") from None
    except subprocess.CalledProcessError:
        raise ImageError("Не удалось прочитать видео. Попробуйте отправить MP4 обычным видео.") from None


def frame_quality(photo: Image.Image) -> float:
    small = photo.convert("L").resize((160, 120))
    # Black frames and motion blur rank below sharp, well-exposed frames.
    edges = small.filter(ImageFilter.FIND_EDGES).crop((2, 2, 158, 118))
    contrast = ImageStat.Stat(small).stddev[0]
    brightness = ImageStat.Stat(small).mean[0]
    exposure = min(brightness, 255 - brightness) / 127.5
    return ImageStat.Stat(edges).var[0] * max(0.05, exposure) + contrast


def distance(left: Image.Image, right: Image.Image) -> float:
    difference = ImageChops.difference(
        left.convert("RGB").resize((32, 24)), right.convert("RGB").resize((32, 24)),
    )
    return sum(ImageStat.Stat(difference).mean) / 3


def select_moments(candidates: list[Moment], count: int = MOMENT_COUNT) -> list[Moment]:
    selected = []
    for candidate in sorted(candidates, key=lambda moment: moment.quality, reverse=True):
        if all(distance(candidate.photo, prior.photo) >= 8 for prior in selected):
            selected.append(candidate)
            if len(selected) == count:
                break
    return sorted(selected, key=lambda moment: moment.seconds)


def container_format(data: bytes) -> str:
    if data[4:8] == b"ftyp":
        return "mov"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return "avi"
    raise ImageError("Не удалось прочитать видео. Пришлите MP4, WebM, AVI или GIF.")


def extract_moments(data: bytes) -> list[Moment]:
    if len(data) > MAX_VIDEO_BYTES:
        raise ImageError("Пришлите видео размером до 20 МБ.")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ImageError("На сервере пока недоступна обработка видео.")
    container = container_format(data)
    with TemporaryDirectory(prefix="sticker-video-") as temporary:
        root = Path(temporary)
        source = root / "source.video"
        source.write_bytes(data)
        probe = run_media([
            ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
            "-show_entries", "format=duration", "-of", "json", "-f", container, str(source),
        ], timeout=15)
        try:
            duration = float(json.loads(probe.stdout)["format"]["duration"])
        except (ValueError, KeyError, TypeError):
            raise ImageError("Не удалось определить длительность видео.") from None
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
            raise ImageError("Пришлите короткое видео длительностью до 60 секунд.")
        run_media([
            ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-filter_threads", "1",
            "-protocol_whitelist", "file,pipe", "-f", container, "-i", str(source), "-an",
            "-vf", f"fps={SAMPLE_COUNT / duration},scale=640:640:force_original_aspect_ratio=decrease",
            "-frames:v", str(SAMPLE_COUNT), str(root / "frame-%02d.png"),
        ], timeout=60)
        candidates = []
        for index, path in enumerate(sorted(root.glob("frame-*.png"))):
            with Image.open(path) as photo:
                photo = photo.convert("RGB")
            candidates.append(Moment(photo, min(duration, (index + 0.5) * duration / SAMPLE_COUNT), frame_quality(photo)))
        if not candidates:
            raise ImageError("В видео не нашлось кадров. Попробуйте другой ролик.")
        return select_moments(candidates)


async def video_bytes(message) -> bytes:
    media = video_media(message)
    if media is None:
        raise ImageError("Отправьте видео, GIF или видеосообщение.")
    if media.file_size and media.file_size > MAX_VIDEO_BYTES:
        raise ImageError("Пришлите видео размером до 20 МБ.")
    duration = getattr(media, "duration", None)
    if duration is not None:
        seconds = duration.total_seconds() if hasattr(duration, "total_seconds") else duration
        if seconds > MAX_DURATION:
            raise ImageError("Пришлите короткое видео длительностью до 60 секунд.")
    file = await media.get_file()
    data = bytes(await file.download_as_bytearray())
    if len(data) > MAX_VIDEO_BYTES:
        raise ImageError("Пришлите видео размером до 20 МБ.")
    return data


def photo_bytes(photo: Image.Image) -> bytes:
    buffer = BytesIO()
    photo.save(buffer, "PNG")
    return buffer.getvalue()
