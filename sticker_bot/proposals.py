import asyncio
from dataclasses import dataclass
from secrets import token_hex

from PIL import Image, UnidentifiedImageError
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, InputFile, InputSticker
from telegram.error import TelegramError

from .albums import prepare_frame
from .images import CAPTION_LIMIT, ImageError
from .library import Library
from .packs import pack_url
from .photos import photo_bytes
from .state import Drafts, Preferences
from .navigation import back_keyboard, home_button


@dataclass
class Suggestion:
    pack_id: int
    file_id: str
    revision: str


class Proposals:
    def __init__(self, library: Library, preferences: Preferences):
        self.library = library
        self.preferences = preferences
        self.targets: Drafts[int] = Drafts()
        self.previews: Drafts[Suggestion] = Drafts(capacity=8)

    def cancel(self, user_id: int) -> None:
        self.targets.discard(user_id)
        self.previews.discard(user_id)

    async def start(self, message, user_id: int, token: str) -> None:
        pack = self.library.invited(token)
        if pack is None:
            await message.reply_text("Приглашение недействительно. Попросите владельца прислать ссылку.")
            return
        self.cancel(user_id)
        self.targets.put(user_id, pack["id"])
        await message.reply_text(
            f"Предложите стикер в набор «{pack['title']}». Пришлите одно фото. "
            "Текст можно добавить прямо в подпись к фотографии.",
            reply_markup=back_keyboard("home", "Выйти из приглашения"),
        )

    async def photo(self, message, user_id: int) -> bool:
        pack_id = self.targets.get(user_id)
        if pack_id is None:
            return False
        if not self.library.can_propose(pack_id, user_id):
            await message.reply_text("В очереди пока нет места или ваше предложение уже ждёт решения.")
            return True
        caption = " ".join((message.caption or "").split())
        if len(caption) > CAPTION_LIMIT:
            await message.reply_text(f"Сократите подпись до {CAPTION_LIMIT} символов и отправьте фото ещё раз.")
            return True
        try:
            raw = await photo_bytes(message.photo[-1])
            frame = await asyncio.to_thread(prepare_frame, raw, self.preferences.color(user_id), caption)
        except (ImageError, UnidentifiedImageError, Image.DecompressionBombError, OSError):
            await message.reply_text("Не удалось открыть фото. Попробуйте прислать другое.")
            return True
        revision = token_hex(5)
        preview = await message.reply_sticker(
            InputFile(frame.draft.preview, filename="sticker.webp"),
            reply_markup=InlineKeyboardMarkup([
                [Button("Предложить владельцу", callback_data=f"g:send:{revision}")],
                [Button("Выбрать другое фото", callback_data=f"g:back:{revision}")],
                [Button("Отменить предложение", callback_data=f"g:cancel:{revision}"), home_button()],
            ]),
        )
        self.previews.put(user_id, Suggestion(pack_id, preview.sticker.file_id, revision))
        return True

    @staticmethod
    async def notify(bot, chat_id: int, proposal) -> None:
        await bot.send_message(chat_id, f"Предложение для «{proposal['title']}»")
        await bot.send_sticker(
            chat_id, proposal["file_id"],
            reply_markup=InlineKeyboardMarkup([[
                Button("Добавить", callback_data=f"g:approve:{proposal['id']}"),
                Button("Отклонить", callback_data=f"g:reject:{proposal['id']}"),
            ], [home_button()]]),
        )

    async def inbox(self, message, user_id: int, bot) -> None:
        rows = self.library.pending(user_id)
        if not rows:
            await message.reply_text("Новых предложений пока нет.", reply_markup=back_keyboard("home", "Главное меню"))
        for proposal in rows:
            await self.notify(bot, user_id, proposal)

    async def callback(self, update, context) -> None:
        query = update.callback_query
        message = update.effective_message
        if message is None or message.chat.type != "private":
            await query.answer()
            return
        user_id = update.effective_user.id
        _, action, value = query.data.split(":")
        if action in {"send", "cancel", "back"}:
            preview = self.previews.get(user_id)
            if preview is None or preview.revision != value:
                await query.answer("Предпросмотр устарел.", show_alert=True)
                return
            await query.answer()
            if action == "back":
                self.previews.discard(user_id)
                await message.reply_text(
                    "Пришлите другое фото с подписью.",
                    reply_markup=back_keyboard("home", "Выйти из приглашения"),
                )
                return
            if action == "cancel":
                self.cancel(user_id)
                await message.reply_text("Предложение отменено.", reply_markup=back_keyboard("home", "Главное меню"))
                return
            pack = self.library.pack(preview.pack_id)
            if pack is None or not self.library.can_propose(preview.pack_id, user_id):
                await message.reply_text("Уже есть предложение на модерации или очередь заполнена.")
                return
            proposal_id = self.library.propose(pack["id"], user_id, preview.file_id)
            self.cancel(user_id)
            proposal = self.library.pending(pack["owner"], proposal_id)
            try:
                await self.notify(context.bot, pack["owner"], proposal)
            except TelegramError:
                # The owner can still review a saved proposal from /inbox.
                pass
            await message.reply_text("Предложение отправлено владельцу набора.", reply_markup=back_keyboard("home", "Главное меню"))
            return

        proposal = self.library.pending(user_id, int(value))
        if proposal is None:
            await query.answer("Предложение уже обработано или недоступно.", show_alert=True)
            return
        await query.answer()
        if action == "approve":
            try:
                await context.bot.add_sticker_to_set(
                    user_id=user_id, name=proposal["name"],
                    sticker=InputSticker(sticker=proposal["file_id"], emoji_list=["😄"], format="static"),
                )
            except TelegramError:
                await message.reply_text(
                    "Telegram не добавил стикер. Проверьте, есть ли место в наборе, и повторите из /inbox."
                )
                return
        accepted = action == "approve"
        self.library.resolve(proposal["id"])
        await message.reply_text("Стикер добавлен." if accepted else "Предложение отклонено.", reply_markup=back_keyboard("inbox", "К предложениям"))
        if proposal["sender"] != user_id:
            text = (
                "Ваш стикер добавлен в набор. " + pack_url(proposal["name"])
                if accepted else f"Владелец набора «{proposal['title']}» отклонил предложение."
            )
            try:
                await context.bot.send_message(proposal["sender"], text)
            except TelegramError:
                pass
