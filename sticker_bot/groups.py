import asyncio
from dataclasses import replace
from secrets import token_urlsafe

from PIL import UnidentifiedImageError
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, InputFile

from .albums import prepare_frame
from .images import CAPTION_LIMIT, ImageError, render
from .persistence import encode, decode
from .phrases import Phrases, normalize
from .videos import extract_moments, photo_bytes as encode_photo, video_bytes
from .photos import photo_bytes


class Groups:
    def __init__(self, library, preferences, phrases: Phrases):
        self.library = library
        self.preferences = preferences
        self.phrases = phrases
        with library.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS group_settings (
                    chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS group_captures (
                    token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,
                    creator INTEGER NOT NULL, payload TEXT NOT NULL
                );
            """)

    def enabled(self, chat_id: int) -> bool:
        with self.library.connect() as db:
            row = db.execute("SELECT enabled FROM group_settings WHERE chat_id = ?", (chat_id,)).fetchone()
            return bool(row and row["enabled"])

    def capture(self, token: str):
        with self.library.connect() as db:
            return db.execute("SELECT * FROM group_captures WHERE token = ?", (token,)).fetchone()

    @staticmethod
    async def help(message, username: str) -> None:
        await message.reply_text(
            f"Мемы этой беседы\n\n"
            f"Администратор включает участие командой /group@{username}.\n"
            f"Ответьте /phrase@{username} на смешную реплику — она попадёт в общий словарь.\n"
            f"Ответьте /meme@{username} на фото или короткое видео — получите стикер и выбор ваших фраз.\n"
            f"/phrases@{username} — словарь. Для добавления подписи сразу напишите /meme@{username} ваша фраза.\n"
            "Фото и реплики выбирают сами участники. Автор может продолжить работу со стикером в личке."
        )

    async def joined(self, update, context) -> None:
        if any(member.id == context.bot.id for member in update.effective_message.new_chat_members):
            await self.help(update.effective_message, context.bot.username)

    async def admin(self, update, bot) -> bool:
        member = await bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return member.status in {"administrator", "creator"}

    async def configure(self, update, context) -> None:
        message = update.effective_message
        if not await self.admin(update, context.bot):
            await message.reply_text("Включить или выключить бота для беседы может администратор.")
            return
        enabled = not (context.args and context.args[0].lower() == "off")
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO group_settings VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET enabled = excluded.enabled",
                (update.effective_chat.id, int(enabled)),
            )
        if enabled:
            await self.help(message, context.bot.username)
        else:
            await message.reply_text("Создание мемов в этой беседе выключено. /group — включить снова.")

    async def list_phrases(self, update, context) -> None:
        rows = self.phrases.list(update.effective_chat.id)
        text = "\n".join(f"{row['id']}. {row['text']}" for row in rows)
        await update.effective_message.reply_text(
            "Фразы беседы\n" + (text or "Пока пусто.") +
            f"\n\nДобавить — /phrase@{context.bot.username} ответом на сообщение. "
            f"Администратор может удалить фразу командой /unphrase@{context.bot.username} номер."
        )

    async def add_phrase(self, update, context) -> None:
        message = update.effective_message
        if not self.enabled(update.effective_chat.id):
            await message.reply_text("Попросите администратора включить бота командой /group.")
            return
        replied = message.reply_to_message
        text = " ".join(context.args) if context.args else (replied.text if replied else "")
        try:
            count = self.phrases.add(update.effective_chat.id, [text or ""])
        except ValueError as error:
            await message.reply_text(str(error))
            return
        await message.reply_text(
            "Фраза добавлена в словарь беседы. Теперь её можно выбрать под стикером."
            if count else "Эта фраза уже есть в словаре."
        )

    async def delete_phrase(self, update, context) -> None:
        if not await self.admin(update, context.bot):
            await update.effective_message.reply_text("Удалить общую фразу может администратор.")
            return
        if not context.args or not context.args[0].isdigit():
            await update.effective_message.reply_text("Укажите номер фразы из /phrases.")
            return
        self.phrases.delete(update.effective_chat.id, int(context.args[0]))
        await self.list_phrases(update, context)

    def keyboard(self, token: str, chat_id: int, username: str) -> InlineKeyboardMarkup:
        keys = [
            [Button(row["text"], callback_data=f"c:{token}:{row['id']}")]
            for row in self.phrases.list(chat_id)
        ]
        keys.append([Button("Редактировать и сохранить", url=f"https://t.me/{username}?start=chat_{token}")])
        return InlineKeyboardMarkup(keys)

    async def meme(self, update, context) -> None:
        message = update.effective_message
        chat_id = update.effective_chat.id
        if not self.enabled(chat_id):
            await message.reply_text("Попросите администратора включить бота командой /group.")
            return
        source = message.reply_to_message
        if source is None:
            await message.reply_text("Ответьте командой /meme на фото или короткое видео.")
            return
        caption = normalize(" ".join(context.args))
        if len(caption) > CAPTION_LIMIT:
            await message.reply_text(f"Подпись должна быть не длиннее {CAPTION_LIMIT} символов.")
            return
        await message.reply_text("Выбираю кадр и готовлю стикер…")
        try:
            if source.photo:
                raw = await photo_bytes(source.photo[-1])
            elif source.video or source.video_note or source.animation:
                moments = await asyncio.to_thread(extract_moments, await video_bytes(source))
                raw = encode_photo(max(moments, key=lambda moment: moment.quality).photo)
            else:
                await message.reply_text("Для /meme выберите сообщение с фото, видео или GIF.")
                return
            frame = await asyncio.to_thread(prepare_frame, raw, self.preferences.color(update.effective_user.id), caption)
        except ImageError as error:
            await message.reply_text(str(error))
            return
        except (UnidentifiedImageError, OSError):
            await message.reply_text("Не удалось открыть выбранное фото. Попробуйте другое.")
            return
        draft = replace(frame.draft, phrase_scope=chat_id)
        token = token_urlsafe(12)
        with self.library.connect() as db:
            db.execute("INSERT INTO group_captures VALUES (?, ?, ?, ?)", (token, chat_id, update.effective_user.id, encode(draft)))
        await message.reply_sticker(
            InputFile(draft.preview, filename="sticker.webp"),
            reply_markup=self.keyboard(token, chat_id, context.bot.username),
        )
        await message.reply_text(
            "Автор может выбрать общую фразу кнопкой под стикером или открыть редактор. "
            "Чтобы сделать свой вариант, ответьте /meme на исходное фото."
        )

    async def callback(self, update, context) -> None:
        query = update.callback_query
        _, token, phrase_id = query.data.split(":")
        capture = self.capture(token)
        if (capture is None or capture["creator"] != update.effective_user.id
                or capture["chat_id"] != update.effective_chat.id or not self.enabled(capture["chat_id"])):
            await query.answer("Этот вариант редактирует его автор. Создайте свой командой /meme.", show_alert=True)
            return
        text = self.phrases.phrase(capture["chat_id"], int(phrase_id))
        if text is None:
            await query.answer("Фраза уже удалена из словаря.", show_alert=True)
            return
        await query.answer()
        draft = decode(capture["payload"])
        draft.caption = text
        draft.preview = await asyncio.to_thread(
            render, draft.subject, draft.color, text, draft.caption_position, draft.caption_gap,
        )
        with self.library.connect() as db:
            db.execute("UPDATE group_captures SET payload = ? WHERE token = ?", (encode(draft), token))
        await update.effective_message.reply_sticker(
            InputFile(draft.preview, filename="sticker.webp"),
            reply_markup=self.keyboard(token, capture["chat_id"], context.bot.username),
        )

    async def enter(self, message, user_id: int, token: str):
        capture = self.capture(token)
        if capture is None or capture["creator"] != user_id or not self.enabled(capture["chat_id"]):
            await message.reply_text("Ссылка доступна автору стикера. Создайте свой вариант командой /meme в беседе.")
            return None
        return decode(capture["payload"])
