from secrets import token_hex
from urllib.parse import urlencode

from telegram import Bot, InputFile, InputSticker
from telegram.error import BadRequest

from .library import Library
from .state import Draft

DEFAULT_TITLE = "Мои мемные стикеры"
MAX_TITLE = 64


def pack_name(user_id: int, bot_username: str) -> str:
    return f"m{user_id:x}_by_{bot_username.lower()}"


def new_pack_name(user_id: int, bot_username: str) -> str:
    return f"m{user_id:x}_{token_hex(4)}_by_{bot_username.lower()}"


async def owned_packs(bot: Bot, owner: int, library: Library):
    legacy = pack_name(owner, bot.username)
    if library.by_name(legacy) is None:
        existing = await find_pack(bot, legacy)
        if existing is not None:
            library.remember(owner, legacy, existing.title)
    return library.owned(owner)


def pack_url(name: str) -> str:
    return f"https://t.me/addstickers/{name}"


def share_url(url: str, text: str) -> str:
    return "https://t.me/share/url?" + urlencode({"url": url, "text": text})


async def find_pack(bot: Bot, name: str):
    try:
        return await bot.get_sticker_set(name)
    except BadRequest as error:
        if str(error).lower().replace("_", " ") not in {"stickerset invalid", "sticker set not found"}:
            raise
        return None


async def upload(bot: Bot, user_id: int, draft: Draft) -> InputSticker:
    if draft.uploaded_id is None:
        file = await bot.upload_sticker_file(
            user_id=user_id,
            sticker=InputFile(draft.preview, filename="sticker.webp"),
            sticker_format="static",
        )
        draft.uploaded_id = file.file_id
    return InputSticker(sticker=draft.uploaded_id, emoji_list=["😄"], format="static")


async def save_to_pack(bot: Bot, user_id: int, draft: Draft) -> str:
    await save_many(bot, user_id, [draft])
    return pack_url(pack_name(user_id, bot.username))


async def save_many(
    bot: Bot, user_id: int, drafts: list[Draft], title: str = DEFAULT_TITLE, *, name: str | None = None
) -> str:
    name = name or pack_name(user_id, bot.username)
    stickers = [await upload(bot, user_id, draft) for draft in drafts]
    existing = await find_pack(bot, name)
    if existing is None:
        await bot.create_new_sticker_set(
            user_id=user_id, name=name, title=title, stickers=stickers, sticker_type="regular",
        )
        return title

    for sticker in stickers:
        await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker)
    return getattr(existing, "title", title)
