import asyncio
from dataclasses import replace
from pathlib import Path
from secrets import token_hex
import logging

from PIL import Image, UnidentifiedImageError
from telegram import BotCommand, InlineKeyboardButton as Button, InlineKeyboardMarkup, InputFile, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .albums import Albums
from .images import CAPTION_LIMIT, COLORS, ImageError, cutout, render
from .library import Library
from .packs import DEFAULT_TITLE, MAX_TITLE, find_pack, new_pack_name, owned_packs, pack_name, pack_url, save_many, share_url
from .proposals import Proposals
from .photos import photo_bytes
from .state import Draft, Drafts, Preferences

LOG = logging.getLogger(__name__)
TEMPLATES = ("Я всё понял", "Это база", "Когда уже пятница?")


class StickerBot:
    def __init__(self, preferences: Preferences):
        self.preferences = preferences
        self.drafts: Drafts[Draft] = Drafts()
        self.library = Library(preferences.path)
        self.albums = Albums(preferences, self.library, self.show_pack)
        self.proposals = Proposals(self.library, preferences)
        self.renaming: Drafts[int] = Drafts()
        self.naming: Drafts[tuple[str, str]] = Drafts()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        args = getattr(context, "args", None)
        if args and args[0].startswith("invite_"):
            await self.proposals.start(update.effective_message, user_id, args[0][7:])
            return
        self.proposals.cancel(user_id)
        await update.effective_message.reply_text(
            "Пришлите фото. Я удалю фон и пришлю стикер с обводкой. "
            "Чтобы собрать несколько стикеров сразу, пришлите альбом или нажмите «Собрать стикерпак».",
            reply_markup=InlineKeyboardMarkup([
                [Button("Собрать стикерпак", callback_data="album")],
                [Button("Мои наборы", callback_data="pack"), Button("Настройки", callback_data="settings")],
            ]),
        )

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        selected = self.preferences.color(update.effective_user.id)
        keys = [
            Button(("✓ " if color == selected else "") + label, callback_data=f"default:{color}")
            for color, (label, _) in COLORS.items()
        ]
        await update.effective_message.reply_text(
            "Обводка для новых фото", reply_markup=InlineKeyboardMarkup([keys[:2], keys[2:]])
        )

    async def show_preview(self, message, user_id: int, draft: Draft) -> None:
        preview = await asyncio.to_thread(render, draft.subject, draft.color, draft.caption)
        draft = replace(draft, preview=preview, revision=token_hex(6), uploaded_id=None, waiting_text=False)

        def button(label: str, action: str) -> Button:
            return Button(label, callback_data=f"d:{draft.revision}:{action}")

        keyboard = InlineKeyboardMarkup([
            [button("Обводка", "colors"), button("Подпись", "captions")],
            [button("Выбрать набор", "save")],
        ])
        await message.reply_sticker(InputFile(preview, filename="sticker.webp"), reply_markup=keyboard)
        self.drafts.put(user_id, draft)

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user_id = update.effective_user.id
        if await self.proposals.photo(message, user_id):
            return
        self.naming.discard(user_id)
        if await self.albums.collect(message, user_id):
            self.renaming.discard(user_id)
            return
        self.renaming.discard(user_id)
        status = await message.reply_text("Вырезаю объект… Первый запуск может занять дольше.")
        try:
            data = await photo_bytes(message.photo[-1])
            subject = await asyncio.to_thread(cutout, data)
            await self.show_preview(message, user_id, Draft(subject, self.preferences.color(user_id)))
        except ImageError as error:
            await status.edit_text(str(error))
        except (UnidentifiedImageError, Image.DecompressionBombError):
            await status.edit_text("Не удалось открыть фото. Попробуйте другое.")
        else:
            await status.edit_text("Готово. Настройки стикера — под ним.")

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        message = update.effective_message
        if self.proposals.targets.get(user_id) is not None:
            await message.reply_text("Пришлите фото с подписью к нему или выйдите командой /cancel.")
            return
        rename_id = self.renaming.get(user_id)
        naming = self.naming.get(user_id)
        if rename_id or naming:
            title = " ".join(message.text.split())
            if not 1 <= len(title) <= MAX_TITLE:
                await message.reply_text(f"Введите название длиной от 1 до {MAX_TITLE} символов.")
                return
            if rename_id:
                pack = self.library.pack(rename_id)
                await context.bot.set_sticker_set_title(name=pack["name"], title=title)
                self.library.remember(user_id, pack["name"], title)
                self.renaming.discard(user_id)
                await self.show_pack(message, pack_url(pack["name"]), context.bot.username, title)
            else:
                revision, name = naming
                draft = self.drafts.get(user_id)
                if draft is None or draft.revision != revision:
                    self.naming.discard(user_id)
                    await message.reply_text("Черновик устарел. Пришлите фото снова.")
                    return
                if await self.save_one(message, context.bot, user_id, draft, name, title):
                    self.naming.discard(user_id)
            return
        if await self.albums.text(message, user_id, context.bot if context else None):
            return
        draft = self.drafts.get(user_id)
        if draft is None or not draft.waiting_text:
            await message.reply_text("Пришлите фото или выберите «Своя подпись» под стикером.")
            return
        caption = " ".join(message.text.split())
        if not 1 <= len(caption) <= CAPTION_LIMIT:
            await message.reply_text(f"Введите подпись длиной от 1 до {CAPTION_LIMIT} символов.")
            return
        await self.show_preview(message, user_id, replace(draft, caption=caption))

    async def show_pack(self, message, url: str, bot_username: str, title: str = DEFAULT_TITLE) -> None:
        name = url.rsplit("/", 1)[-1]
        pack = self.library.by_name(name)
        keys = [[Button("Открыть набор", url=url), Button("Поделиться набором", url=share_url(url, title))]]
        if pack is not None:
            pack_id = pack["id"]
            keys.extend([
                [Button("Добавить фото", callback_data="album"), Button("Переименовать", callback_data=f"p:rename:{pack_id}")],
                [Button("Пригласить друзей", callback_data=f"p:invite:{pack_id}")],
            ])
        keys.extend([
            [Button("Мои наборы", callback_data="pack"), Button("Как удалить стикер", callback_data="remove_help")],
            [Button("Поделиться ботом", url=share_url(f"https://t.me/{bot_username}", "Сделай стикер из фото"))],
        ])
        await message.reply_text(f"Ваш набор «{title}»", reply_markup=InlineKeyboardMarkup(keys))

    async def my_pack(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        packs = await owned_packs(context.bot, update.effective_user.id, self.library)
        choices = [[Button(pack["title"], callback_data=f"p:open:{pack['id']}")] for pack in packs]
        choices.append([Button("Собрать стикерпак", callback_data="album")])
        await update.effective_message.reply_text(
            "Мои наборы" if packs else "Пришлите фото, чтобы создать первый набор.",
            reply_markup=InlineKeyboardMarkup(choices),
        )

    async def manage_pack(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        message = update.effective_message
        user_id = update.effective_user.id
        _, action, selected = query.data.split(":")
        pack = self.library.pack(int(selected))
        if message is None or message.chat.type != "private" or pack is None or pack["owner"] != user_id:
            await query.answer("Набор недоступен.", show_alert=True)
            return
        await query.answer()
        name, title = pack["name"], pack["title"]
        if action == "open":
            await self.show_pack(message, pack_url(name), context.bot.username, title)
        elif action == "invite":
            token = self.library.invite(pack["id"], user_id)
            link = f"https://t.me/{context.bot.username}?start=invite_{token}"
            await message.reply_text(
                f"По этой ссылке друзья смогут предложить стикер в «{title}». Вы решаете, что попадёт в набор.\n{link}",
                reply_markup=InlineKeyboardMarkup([[Button("Отправить приглашение", url=share_url(link, f"Предложи стикер в «{title}»"))]]),
            )
        elif self.albums.sessions.get(user_id):
            await message.reply_text("Сначала завершите или закройте альбом командой /cancel.")
        else:
            self.naming.discard(user_id)
            self.renaming.put(user_id, pack["id"])
            await message.reply_text("Введите новое название набора, до 64 символов. /cancel — отменить.")

    async def inbox(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.proposals.inbox(update.effective_message, update.effective_user.id, context.bot)

    async def album(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        self.renaming.discard(user_id)
        self.naming.discard(user_id)
        self.proposals.cancel(user_id)
        await self.albums.start(update.effective_message, user_id)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        self.albums.sessions.discard(user_id)
        self.renaming.discard(user_id)
        self.drafts.discard(user_id)
        self.naming.discard(user_id)
        self.proposals.cancel(user_id)
        await update.effective_message.reply_text("Редактирование закрыто. Можете прислать фото.")

    async def remove_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        replied = message.reply_to_message
        chosen = replied.sticker if replied else None
        if chosen is None:
            await message.reply_text("Отправьте стикер из своего набора в этот чат и ответьте на него командой /remove.")
            return
        user_id = update.effective_user.id
        name = chosen.set_name
        saved = self.library.by_name(name) if name else None
        if not name or (
            name != pack_name(user_id, context.bot.username) and (saved is None or saved["owner"] != user_id)
        ):
            await message.reply_text("Выберите стикер из своего набора командой /pack.")
            return
        pack = await find_pack(context.bot, name)
        if pack is None:
            await message.reply_text("Выберите стикер из своего набора командой /pack.")
            return
        match = next((sticker for sticker in pack.stickers if sticker.file_unique_id == chosen.file_unique_id), None)
        if match is None:
            await message.reply_text("Этого стикера уже нет в наборе.")
            return
        if len(pack.stickers) == 1:
            await message.reply_text("В наборе один стикер. Добавьте другой, затем повторите удаление.")
            return
        await context.bot.delete_sticker_from_set(sticker=match.file_id)
        await message.reply_text("Стикер удалён из набора.")

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        message = update.effective_message
        if message is None or message.chat.type != "private":
            await query.answer()
            return
        user_id = update.effective_user.id
        if query.data in {"album", "pack", "rename", "remove_help"}:
            await query.answer()
            if query.data == "album":
                await self.album(update, context)
            elif query.data == "pack":
                await self.my_pack(update, context)
            elif query.data == "remove_help":
                await message.reply_text("Отправьте стикер из своего набора в этот чат и ответьте на него командой /remove.")
            else:
                await self.my_pack(update, context)
            return
        if query.data == "settings":
            await query.answer()
            await self.settings(update, context)
            return
        if query.data.startswith("default:"):
            color = query.data.split(":")[1]
            if color in COLORS:
                self.preferences.set_color(user_id, color)
                await query.answer(f"Цвет для новых фото: {COLORS[color][0].lower()}")
            else:
                await query.answer()
            return

        if self.albums.sessions.get(user_id):
            await query.answer("Сначала завершите редактирование альбома.", show_alert=True)
            return
        _, revision, action = query.data.split(":", 2)
        draft = self.drafts.get(user_id)
        if draft is None or draft.revision != revision:
            await query.answer("Этот черновик устарел. Откройте последний стикер или пришлите фото.", show_alert=True)
            return
        await query.answer()
        self.renaming.discard(user_id)
        self.naming.discard(user_id)

        def option(label: str, value: str) -> Button:
            return Button(label, callback_data=f"d:{revision}:{value}")

        if action == "colors":
            keys = [option(label, f"color:{key}") for key, (label, _) in COLORS.items()]
            await message.reply_text("Цвет обводки", reply_markup=InlineKeyboardMarkup([keys[:2], keys[2:]]))
        elif action == "captions":
            keys = [[option(label, f"template:{index}")] for index, label in enumerate(TEMPLATES)]
            keys.append([option("Своя подпись", "custom"), option("Без подписи", "clear")])
            await message.reply_text("Подпись на стикере", reply_markup=InlineKeyboardMarkup(keys))
        elif action == "custom":
            await message.reply_text(f"Отправьте подпись одним сообщением, до {CAPTION_LIMIT} символов.")
            draft.waiting_text = True
        elif action == "save":
            packs = await owned_packs(context.bot, user_id, self.library)
            keys = [[option("Создать новый", "new")]]
            keys.extend([option(pack["title"], f"put:{pack['id']}")] for pack in packs)
            await message.reply_text("Куда сохранить стикер?", reply_markup=InlineKeyboardMarkup(keys))
        elif action == "new":
            self.naming.put(user_id, (revision, new_pack_name(user_id, context.bot.username)))
            await message.reply_text("Напишите название нового набора. /cancel — отменить.")
        elif action.startswith("put:"):
            pack = self.library.pack(int(action.split(":")[1]))
            if pack is None or pack["owner"] != user_id:
                await message.reply_text("Этот набор недоступен.")
                return
            await self.save_one(message, context.bot, user_id, draft, pack["name"], pack["title"])
        elif action == "clear":
            await self.show_preview(message, user_id, replace(draft, caption=""))
        elif action.startswith("color:"):
            await self.show_preview(message, user_id, replace(draft, color=action.split(":")[1]))
        elif action.startswith("template:"):
            await self.show_preview(message, user_id, replace(draft, caption=TEMPLATES[int(action.split(":")[1])]))


    async def save_one(self, message, bot, user_id: int, draft: Draft, name: str, title: str) -> bool:
        try:
            title = await save_many(bot, user_id, [draft], title, name=name)
        except BadRequest as error:
            normalized = str(error).lower().replace("_", " ")
            if "stickers too much" in normalized or "sticker set is full" in normalized:
                await message.reply_text("Набор заполнен. Удалите один стикер из набора и повторите.")
                return False
            raise
        self.library.remember(user_id, name, title)
        await self.show_pack(message, pack_url(name), bot.username, title)
        return True


async def report_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # URLs in transport exceptions can contain the bot token.
    LOG.error("Ошибка обработки (%s)", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Не удалось завершить операцию. Попробуйте ещё раз.")
        except TelegramError:
            pass


async def register_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Создать стикер"),
        BotCommand("settings", "Цвет обводки по умолчанию"),
        BotCommand("album", "Собрать набор из нескольких фото"),
        BotCommand("pack", "Мои наборы"),
        BotCommand("inbox", "Предложения от друзей"),
        BotCommand("remove", "Удалить стикер ответом на него"),
        BotCommand("cancel", "Закрыть черновик"),
    ])


def build_app(token: str, database: Path) -> Application:
    bot = StickerBot(Preferences(database))
    app = Application.builder().token(token).concurrent_updates(False).post_init(register_commands).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", bot.start, filters=private))
    app.add_handler(CommandHandler("settings", bot.settings, filters=private))
    app.add_handler(CommandHandler("pack", bot.my_pack, filters=private))
    app.add_handler(CommandHandler("inbox", bot.inbox, filters=private))
    app.add_handler(CommandHandler("album", bot.album, filters=private))
    app.add_handler(CommandHandler("remove", bot.remove_sticker, filters=private))
    app.add_handler(CommandHandler("cancel", bot.cancel, filters=private))
    app.add_handler(MessageHandler(private & filters.PHOTO, bot.photo))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, bot.text))
    app.add_handler(CallbackQueryHandler(
        bot.callback,
        pattern=r"^(?:album|pack|rename|remove_help|settings|default:(?:white|pink|yellow|blue)|d:[0-9a-f]{12}:(?:colors|captions|custom|clear|save|new|put:[0-9]+|color:(?:white|pink|yellow|blue)|template:[0-2]))$",
    ))
    app.add_handler(CallbackQueryHandler(
        bot.albums.callback,
        pattern=r"^a:[0-9a-f]{10}:(?:review|edit:[0-7]|overview|caption|clear|drop|closer|cutout|frame|save|new|put:[0-9]+|retry|cancel)$",
    ))
    app.add_handler(CallbackQueryHandler(bot.manage_pack, pattern=r"^p:(?:open|rename|invite):[0-9]+$"))
    app.add_handler(CallbackQueryHandler(
        bot.proposals.callback,
        pattern=r"^g:(?:(?:send|cancel):[0-9a-f]{10}|(?:approve|reject):[0-9]+)$",
    ))
    app.add_error_handler(report_error)
    return app
