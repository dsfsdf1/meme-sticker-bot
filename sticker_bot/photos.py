from telegram import PhotoSize

from .images import ImageError

MAX_PHOTO_BYTES = 10 * 1024 * 1024


async def photo_bytes(photo: PhotoSize) -> bytes:
    if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
        raise ImageError("Пришлите фото размером до 10 МБ.")
    file = await photo.get_file()
    data = await file.download_as_bytearray()
    if len(data) > MAX_PHOTO_BYTES:
        raise ImageError("Пришлите фото размером до 10 МБ.")
    return bytes(data)
