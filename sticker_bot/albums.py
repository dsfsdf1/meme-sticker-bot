import asyncio
from dataclasses import dataclass, field, replace
from io import BytesIO
from secrets import token_hex

from PIL import Image, ImageDraw, UnidentifiedImageError
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, InputFile, PhotoSize
from telegram.error import BadRequest, TelegramError

from .images import CAPTION_LIMIT, COLORS, ImageError, caption_font, cutout_photo, open_photo, photo_in_frame, render
from .library import Library
from .packs import MAX_TITLE, new_pack_name, owned_packs, pack_url, save_many
from .photos import MAX_PHOTO_BYTES, photo_bytes
from .state import Draft, Drafts, Preferences, record_edit, restore_edit
from .navigation import back_keyboard, color_keyboard, history_buttons, home_button, position_keyboard

MAX_PHOTOS = 8
MIN_PHOTOS = 3


@dataclass
class Frame:
    photo: Image.Image
    draft: Draft


@dataclass
class Album:
    revision: str = field(default_factory=lambda: token_hex(5))
    photos: list[PhotoSize] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    collecting: bool = True
    waiting: str | None = None
    index: int = 0
    title: str | None = None
    target: str | None = None
    removed: list[tuple[int, Frame]] = field(default_factory=list)
    progress_task: asyncio.Task | None = field(default=None, repr=False)
    limit_warned: bool = False


def frame_subject(photo: Image.Image, mode: str) -> Image.Image:
    if mode == "frame":
        return photo_in_frame(photo)
    if mode == "closer":
        x, y = photo.width // 8, photo.height // 8
        photo = photo.crop((x, y, photo.width - x, photo.height - y))
    return cutout_photo(photo)


def prepare_frame(data: bytes, color: str, caption: str = "") -> Frame:
    photo = open_photo(data)
    photo.thumbnail((900, 900), Image.Resampling.LANCZOS)
    try:
        subject = frame_subject(photo, "cutout")
    except ImageError:
        subject = frame_subject(photo, "frame")
    return Frame(photo, Draft(subject, color, caption, preview=render(subject, color, caption)))


def contact_sheet(frames: list[Frame]) -> bytes:
    tile = 208
    columns = min(len(frames), 3)
    rows = (len(frames) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tile, rows * tile), "#20232b")
    draw = ImageDraw.Draw(sheet)
    for index, frame in enumerate(frames):
        x, y = (index % columns) * tile, (index // columns) * tile
        with Image.open(BytesIO(frame.draft.preview)) as sticker:
            thumb = sticker.resize((176, 176), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (x + 16, y + 27), thumb)
        draw.text((x + 13, y + 7), str(index + 1), font=caption_font(20), fill="white")
    output = BytesIO()
    sheet.save(output, "JPEG", quality=91)
    return output.getvalue()


class Albums:
    def __init__(self, preferences: Preferences, library: Library, show_pack):
        self.preferences = preferences
        self.show_pack = show_pack
        self.library = library
        self.sessions: Drafts[Album] = Drafts(capacity=8)

    async def start(self, message, user_id: int) -> Album:
        existing = self.sessions.get(user_id)
        if existing:
            if existing.collecting:
                await self.collection_prompt(message, existing)
            elif existing.title is not None:
                await self.retry_prompt(message, existing)
            else:
                await self.overview(message, existing)
            return existing
        album = Album()
        self.sessions.put(user_id, album)
        await self.collection_prompt(message, album)
        return album

    @staticmethod
    def button(album: Album, label: str, action: str) -> Button:
        return Button(label, callback_data=f"a:{album.revision}:{action}")

    async def collection_prompt(self, message, album: Album) -> None:
        text = (
            f"Получено фото: {len(album.photos)} из {MAX_PHOTOS}. Можно открыть превью после трёх."
            if album.photos else "Пришлите от 3 до 8 фото одним альбомом или по очереди."
        )
        await message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [self.button(album, "Посмотреть превью", "review")],
                [self.button(album, "Закрыть альбом", "cancel"), home_button()],
            ]),
        )

    async def collect(self, message, user_id: int) -> bool:
        album = self.sessions.get(user_id)
        if album is None and getattr(message, "media_group_id", None):
            album = await self.start(message, user_id)
        if album is None:
            return False
        if not album.collecting:
            await message.reply_text("Сначала сохраните или закройте текущий альбом. Команда /album вернёт к нему.")
            return True

        photo = message.photo[-1]
        if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
            await message.reply_text("Это фото больше 10 МБ. Пришлите уменьшенную копию.")
        elif len(album.photos) < MAX_PHOTOS:
            album.photos.append(photo)
        elif not album.limit_warned:
            album.limit_warned = True
            await message.reply_text("Взял первые 8 фото. Теперь нажмите «Посмотреть превью».")
        if album.progress_task and not album.progress_task.done():
            album.progress_task.cancel()
        album.progress_task = asyncio.create_task(self.progress(message, user_id, album))
        return True

    async def progress(self, message, user_id: int, album: Album) -> None:
        await asyncio.sleep(0.9)
        if self.sessions.get(user_id) is album and album.collecting:
            try:
                await self.collection_prompt(message, album)
            except TelegramError:
                pass

    async def review(self, message, user_id: int, album: Album) -> None:
        if len(album.photos) < MIN_PHOTOS:
            await message.reply_text(f"Получено фото: {len(album.photos)} из 3. Пришлите остальные.")
            return
        if album.progress_task:
            album.progress_task.cancel()
        color = self.preferences.color(user_id)
        await message.reply_text("Готовлю превью. Это может занять несколько секунд.")
        frames = []
        for photo in album.photos:
            try:
                data = await photo_bytes(photo)
                frames.append(await asyncio.to_thread(prepare_frame, data, color))
            except (ImageError, UnidentifiedImageError, Image.DecompressionBombError, OSError):
                continue
        skipped = len(album.photos) - len(frames)
        if skipped:
            await message.reply_text(f"Не удалось обработать фото: {skipped}.")
        if not frames:
            album.photos.clear()
            await self.collection_prompt(message, album)
            return
        album.collecting = False
        album.photos.clear()
        album.frames = frames
        await self.overview(message, album)

    async def overview(self, message, album: Album) -> None:
        album.waiting = None
        album.revision = token_hex(5)
        buttons = [self.button(album, str(index + 1), f"edit:{index}") for index in range(len(album.frames))]
        keys = [buttons[offset:offset + 4] for offset in range(0, len(buttons), 4)]
        if album.removed:
            keys.append([self.button(album, "↶ Вернуть удалённый кадр", "restore")])
        keys.extend([
            [self.button(album, "Сохранить стикеры", "save")],
            [self.button(album, "Закрыть альбом", "cancel"), home_button()],
        ])
        preview = await asyncio.to_thread(contact_sheet, album.frames)
        await message.reply_photo(
            InputFile(preview, filename="album.jpg"),
            caption=f"Будущий набор · {len(album.frames)} шт. Нажмите номер, чтобы изменить стикер.",
            reply_markup=InlineKeyboardMarkup(keys),
        )

    async def edit(self, message, album: Album, index: int) -> None:
        album.index = index
        album.waiting = None
        album.revision = token_hex(5)
        def button(text: str, action: str) -> Button:
            return self.button(album, text, action)

        draft = album.frames[index].draft
        keys = [
            [button("Обводка", "colors"), button("Подпись", "caption")],
            [button("Расположение текста", "layout"), button("Убрать подпись", "clear")],
            [button("Перекадрировать", "closer"), button("Фото в рамке", "frame")],
            [button("Автовырезание", "cutout"), button("Убрать из альбома", "drop")],
        ]
        history = history_buttons(f"a:{album.revision}:", draft)
        if history:
            keys.append(history)
        pages = []
        if index:
            pages.append(button("← Предыдущий кадр", f"edit:{index - 1}"))
        if index + 1 < len(album.frames):
            pages.append(button("Следующий кадр →", f"edit:{index + 1}"))
        if pages:
            keys.append(pages)
        keys.append([button("К обзору", "overview"), home_button()])
        await message.reply_sticker(
            InputFile(draft.preview, filename="sticker.webp"), reply_markup=InlineKeyboardMarkup(keys),
        )

    async def render_edit(self, message, album: Album, draft: Draft, remember: bool = True) -> None:
        if remember:
            draft = record_edit(album.frames[album.index].draft, draft)
        draft.preview = await asyncio.to_thread(
            render, draft.subject, draft.color, draft.caption, draft.caption_position, draft.caption_gap,
        )
        draft.uploaded_id = None
        album.frames[album.index].draft = draft
        await self.edit(message, album, album.index)

    async def text(self, message, user_id: int, bot) -> bool:
        album = self.sessions.get(user_id)
        if album is None:
            return False
        text = " ".join(message.text.split())
        if album.waiting == "caption":
            if not 1 <= len(text) <= CAPTION_LIMIT:
                await message.reply_text(f"Введите подпись длиной от 1 до {CAPTION_LIMIT} символов.")
            else:
                await self.render_edit(message, album, replace(album.frames[album.index].draft, caption=text))
        elif album.waiting == "title":
            if not 1 <= len(text) <= MAX_TITLE:
                await message.reply_text(f"Введите название длиной от 1 до {MAX_TITLE} символов.")
            else:
                await self.save(message, user_id, bot, album, text)
        else:
            await message.reply_text("Вернитесь к альбому командой /album или отмените его командой /cancel.")
        return True

    async def save(self, message, user_id: int, bot, album: Album, title: str) -> None:
        album.title = title
        album.target = album.target or new_pack_name(user_id, bot.username)
        album.waiting = None
        album.revision = token_hex(5)
        try:
            title = await save_many(bot, user_id, [frame.draft for frame in album.frames], title, name=album.target)
        except BadRequest as error:
            normalized = str(error).lower().replace("_", " ")
            if "stickers too much" in normalized or "sticker set is full" in normalized:
                await message.reply_text("Набор заполнен. Освободите место командой /remove и повторите сохранение.")
            await self.retry_prompt(message, album)
            return
        except TelegramError:
            await self.retry_prompt(message, album)
            return
        self.library.remember(user_id, album.target, title)
        self.sessions.discard(user_id)
        await self.show_pack(message, pack_url(album.target), bot.username, title)

    async def retry_prompt(self, message, album: Album) -> None:
        await message.reply_text(
            "Telegram не подтвердил сохранение всех стикеров. Можно повторить. "
            "Стикеры, которые уже появились в наборе, сохранятся.",
            reply_markup=InlineKeyboardMarkup([
                [self.button(album, "Повторить сохранение", "retry"), self.button(album, "Закрыть", "cancel")],
            ]),
        )

    async def callback(self, update, context) -> None:
        query = update.callback_query
        message = update.effective_message
        user_id = update.effective_user.id
        _, revision, action = query.data.split(":", 2)
        album = self.sessions.get(user_id)
        if message is None or message.chat.type != "private" or album is None or album.revision != revision:
            await query.answer("Эта кнопка устарела. Откройте /album.", show_alert=True)
            return
        await query.answer()
        if action == "cancel":
            await message.reply_text(
                "Закрыть альбом и удалить этот черновик?",
                reply_markup=InlineKeyboardMarkup([
                    [self.button(album, "Продолжить редактирование", "resume")],
                    [self.button(album, "Удалить черновик", "close")],
                ]),
            )
        elif action == "resume":
            if album.collecting:
                await self.collection_prompt(message, album)
            elif album.title is not None:
                await self.retry_prompt(message, album)
            else:
                await self.overview(message, album)
        elif action == "close":
            if album.progress_task:
                album.progress_task.cancel()
            self.sessions.discard(user_id)
            await message.reply_text("Альбом закрыт.", reply_markup=back_keyboard("home", "В главное меню"))
        elif action == "review" and album.collecting:
            await self.review(message, user_id, album)
        elif action == "retry" and album.title is not None:
            await self.save(message, user_id, context.bot, album, album.title)
        elif album.collecting or album.title is not None:
            return
        elif action.startswith("edit:"):
            index = int(action.split(":")[1])
            if index < len(album.frames):
                await self.edit(message, album, index)
        elif action == "back":
            await self.edit(message, album, album.index)
        elif action in {"undo", "redo"}:
            draft = restore_edit(album.frames[album.index].draft, action)
            await self.render_edit(message, album, draft, remember=False)
        elif action == "colors":
            album.waiting = None
            draft = album.frames[album.index].draft
            await message.reply_text(
                "Выберите обводку.",
                reply_markup=color_keyboard(f"a:{album.revision}:", draft.color, f"a:{album.revision}:back"),
            )
        elif action.startswith("color:"):
            await self.render_edit(message, album, replace(album.frames[album.index].draft, color=action.split(":")[1]))
        elif action == "layout":
            album.waiting = None
            await message.reply_text(
                "Где разместить подпись?",
                reply_markup=position_keyboard(f"a:{album.revision}:", album.frames[album.index].draft, f"a:{album.revision}:back"),
            )
        elif action.startswith("position:"):
            await self.render_edit(message, album, replace(album.frames[album.index].draft, caption_position=action.split(":")[1]))
        elif action.startswith("gap:"):
            await self.render_edit(message, album, replace(album.frames[album.index].draft, caption_gap=int(action.split(":")[1])))
        elif action == "restore" and album.removed:
            index, frame = album.removed.pop()
            album.frames.insert(index, frame)
            await self.overview(message, album)
        elif action == "overview":
            await self.overview(message, album)
        elif action == "save":
            album.waiting = None
            packs = await owned_packs(context.bot, user_id, self.library)
            keys = [[self.button(album, "Создать новый", "new")]]
            keys.extend([self.button(album, row["title"], f"put:{row['id']}")] for row in packs)
            keys.append([self.button(album, "К обзору", "overview"), home_button()])
            await message.reply_text("Куда сохранить стикеры?", reply_markup=InlineKeyboardMarkup(keys))
        elif action == "new":
            album.target = new_pack_name(user_id, context.bot.username)
            album.waiting = "title"
            album.revision = token_hex(5)
            await message.reply_text("Напишите название нового набора.", reply_markup=back_keyboard(f"a:{album.revision}:save", "Отменить ввод"))
        elif action.startswith("put:"):
            pack = self.library.pack(int(action.split(":")[1]))
            if pack is None or pack["owner"] != user_id:
                await message.reply_text("Этот набор недоступен.")
                return
            album.target = pack["name"]
            await self.save(message, user_id, context.bot, album, pack["title"])
        elif action == "caption":
            album.waiting = "caption"
            await message.reply_text(f"Введите подпись, до {CAPTION_LIMIT} символов.", reply_markup=back_keyboard(f"a:{album.revision}:back", "Отменить ввод"))
        elif action == "clear":
            await self.render_edit(message, album, replace(album.frames[album.index].draft, caption=""))
        elif action == "drop":
            if len(album.frames) == 1:
                await message.reply_text("В альбоме остался один стикер. Его можно сохранить или отменить альбом.")
            else:
                album.removed.append((album.index, album.frames.pop(album.index)))
                await self.overview(message, album)
        elif action in {"closer", "frame", "cutout"}:
            frame = album.frames[album.index]
            try:
                subject = await asyncio.to_thread(frame_subject, frame.photo, action)
            except ImageError:
                await message.reply_text("На этом кадре объект не выделился. Попробуйте «Фото в рамке».", reply_markup=back_keyboard(f"a:{album.revision}:back", "К редактору"))
                return
            await self.render_edit(message, album, replace(frame.draft, subject=subject))
