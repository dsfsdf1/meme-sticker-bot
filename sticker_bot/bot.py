import asyncio
from dataclasses import replace
from pathlib import Path
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
from .persistence import SavedDrafts
from .state import Draft, Preferences, record_edit, restore_edit
from .navigation import back_keyboard, color_keyboard, history_buttons, home_button, position_keyboard, refresh_options

LOG = logging.getLogger(__name__)
TEMPLATES = ("Я всё понял", "Это база", "Когда уже пятница?")


class StickerBot:
    def __init__(self, preferences: Preferences):
        self.preferences = preferences
        self.drafts = SavedDrafts(preferences.path, "single")
        self.library = Library(preferences.path)
        self.albums = Albums(preferences, self.library, self.show_pack)
        self.proposals = Proposals(self.library, preferences)
        self.renaming = SavedDrafts(preferences.path, "renaming")
        self.naming = SavedDrafts(preferences.path, "naming")

    def cancel_input(self, user_id: int) -> None:
        self.renaming.discard(user_id)
        self.naming.discard(user_id)
        draft = self.drafts.get(user_id)
        if draft:
            draft.waiting_text = False
        album = self.albums.sessions.get(user_id)
        if album:
            album.waiting = None

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        args = getattr(context, "args", None)
        if args and args[0].startswith("invite_"):
            await self.proposals.start(update.effective_message, user_id, args[0][7:])
            return
        await self.home(update, context)

    async def home(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        self.cancel_input(user_id)
        self.proposals.cancel(user_id)
        draft = self.drafts.get(user_id)
        album = self.albums.sessions.get(user_id)
        keys = [
            [Button("Собрать стикерпак", callback_data="album")],
            [Button("Мои наборы", callback_data="pack"), Button("Настройки", callback_data="settings")],
            [Button("Предложения друзей", callback_data="inbox")],
        ]
        if album:
            keys.insert(0, [Button("Вернуться к альбому", callback_data="album")])
        elif draft:
            keys.insert(0, [Button("Вернуться к стикеру", callback_data="resume")])
        await update.effective_message.reply_text(
            "Пришлите фото. Я удалю фон и пришлю стикер с обводкой. "
            "Чтобы собрать несколько стикеров сразу, пришлите альбом или нажмите «Собрать стикерпак».",
            reply_markup=InlineKeyboardMarkup(keys),
        )

    async def back(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        message = update.effective_message
        if self.proposals.targets.get(user_id) is not None:
            await self.home(update, context)
            return
        rename_id = self.renaming.get(user_id)
        self.renaming.discard(user_id)
        self.naming.discard(user_id)
        album = self.albums.sessions.get(user_id)
        if album:
            if album.collecting:
                await self.albums.collection_prompt(message, album)
            elif album.title is not None:
                await self.albums.retry_prompt(message, album)
            elif album.waiting == "caption":
                await self.albums.edit(message, album, album.index)
            else:
                await self.albums.overview(message, album)
        elif rename_id:
            pack = self.library.pack(rename_id)
            await self.show_pack(message, pack_url(pack["name"]), context.bot.username, pack["title"])
        elif self.drafts.get(user_id):
            await self.show_preview(message, user_id, self.drafts.get(user_id), remember=False)
        else:
            await self.home(update, context)

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.cancel_input(update.effective_user.id)
        selected = self.preferences.color(update.effective_user.id)
        keys = [
            Button(("✓ " if color == selected else "") + label, callback_data=f"default:{color}")
            for color, (label, _) in COLORS.items()
        ]
        await update.effective_message.reply_text(
            "Обводка для новых фото", reply_markup=InlineKeyboardMarkup([keys[:1], keys[1:3], keys[3:], [home_button()]])
        )

    async def show_preview(self, message, user_id: int, draft: Draft, remember: bool = True) -> None:
        current = self.drafts.get(user_id)
        if remember and current:
            draft = record_edit(current, draft)
        preview = await asyncio.to_thread(
            render, draft.subject, draft.color, draft.caption, draft.caption_position, draft.caption_gap,
        )
        draft = replace(draft, preview=preview, uploaded_id=None, waiting_text=False)

        def button(label: str, action: str) -> Button:
            return Button(label, callback_data=f"d:{draft.revision}:{action}")

        keys = [
            [button("Обводка", "colors"), button("Подпись", "captions")],
            [button("Расположение текста", "layout")],
            [button("Выбрать набор", "save")],
        ]
        history = history_buttons(f"d:{draft.revision}:", draft)
        if history:
            keys.append(history)
        keys.append([home_button()])
        keyboard = InlineKeyboardMarkup(keys)
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
            await self.show_preview(message, user_id, Draft(subject, self.preferences.color(user_id)), remember=False)
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
            [home_button()],
            [Button("Мои наборы", callback_data="pack"), Button("Как удалить стикер", callback_data="remove_help")],
            [Button("Поделиться ботом", url=share_url(f"https://t.me/{bot_username}", "Сделай стикер из фото"))],
        ])
        await message.reply_text(f"Ваш набор «{title}»", reply_markup=InlineKeyboardMarkup(keys))

    async def my_pack(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.cancel_input(update.effective_user.id)
        packs = await owned_packs(context.bot, update.effective_user.id, self.library)
        choices = [[Button(pack["title"], callback_data=f"p:open:{pack['id']}")] for pack in packs]
        choices.append([Button("Собрать стикерпак", callback_data="album"), home_button()])
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
            self.cancel_input(user_id)
            await self.show_pack(message, pack_url(name), context.bot.username, title)
        elif action == "invite":
            token = self.library.invite(pack["id"], user_id)
            link = f"https://t.me/{context.bot.username}?start=invite_{token}"
            await message.reply_text(
                f"По этой ссылке друзья смогут предложить стикер в «{title}». Вы решаете, что попадёт в набор.\n{link}",
                reply_markup=InlineKeyboardMarkup([[Button("Отправить приглашение", url=share_url(link, f"Предложи стикер в «{title}»"))], [Button("Назад к набору", callback_data=f"p:open:{selected}"), home_button()]]),
            )
        elif self.albums.sessions.get(user_id):
            await message.reply_text("Сначала завершите или закройте альбом командой /cancel.")
        else:
            self.naming.discard(user_id)
            self.renaming.put(user_id, pack["id"])
            await message.reply_text("Введите новое название набора, до 64 символов.", reply_markup=back_keyboard(f"p:open:{selected}", "Отменить ввод"))

    async def inbox(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.cancel_input(update.effective_user.id)
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
        await update.effective_message.reply_text("Редактирование закрыто. Можете прислать фото.", reply_markup=back_keyboard("home", "В главное меню"))

    async def remove_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        replied = message.reply_to_message
        chosen = replied.sticker if replied else None
        if chosen is None:
            await message.reply_text(
                    "Отправьте стикер из своего набора в этот чат и ответьте на него командой /remove.",
                    reply_markup=back_keyboard("pack", "Назад к наборам"),
                )
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
        if query.data in {"home", "resume", "inbox"}:
            await query.answer()
            if query.data == "home":
                await self.home(update, context)
            elif query.data == "inbox":
                await self.inbox(update, context)
            elif self.albums.sessions.get(user_id):
                await self.albums.start(message, user_id)
            else:
                draft = self.drafts.get(user_id)
                if draft:
                    self.naming.discard(user_id)
                    await self.show_preview(message, user_id, draft, remember=False)
                else:
                    await self.home(update, context)
            return
        if query.data in {"album", "pack", "rename", "remove_help"}:
            await query.answer()
            if query.data == "album":
                await self.album(update, context)
            elif query.data == "pack":
                await self.my_pack(update, context)
            elif query.data == "remove_help":
                await message.reply_text(
                    "Отправьте стикер из своего набора в этот чат и ответьте на него командой /remove.",
                    reply_markup=back_keyboard("pack", "Назад к наборам"),
                )
            else:
                await self.my_pack(update, context)
            return
        if query.data == "settings":
            await query.answer()
            self.renaming.discard(user_id)
            self.naming.discard(user_id)
            await self.settings(update, context)
            return
        if query.data.startswith("default:"):
            color = query.data.split(":")[1]
            if color in COLORS:
                self.preferences.set_color(user_id, color)
                await query.answer("Настройка сохранена")
                await self.settings(update, context)
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
            draft.waiting_text = False
            await message.reply_text(
                "Выберите обводку. Последнюю правку можно отменить под стикером.",
                reply_markup=color_keyboard(f"d:{revision}:", draft.color, f"d:{revision}:back"),
            )
        elif action == "captions":
            draft.waiting_text = False
            keys = [[option(label, f"template:{index}")] for index, label in enumerate(TEMPLATES)]
            keys.append([option("Своя подпись", "custom"), option("Без подписи", "clear")])
            keys.append([option("Расположение текста", "layout")])
            keys.append([option("Назад к стикеру", "back"), home_button()])
            await message.reply_text("Подпись на стикере", reply_markup=InlineKeyboardMarkup(keys))
        elif action == "custom":
            await message.reply_text(f"Отправьте подпись одним сообщением, до {CAPTION_LIMIT} символов.", reply_markup=back_keyboard(f"d:{revision}:captions", "Отменить ввод"))
            draft.waiting_text = True
        elif action in {"undo", "redo", "back"}:
            restored = restore_edit(draft, action)
            await self.show_preview(message, user_id, restored, remember=False)
        elif action == "layout":
            draft.waiting_text = False
            await message.reply_text(
                "Где разместить подпись?",
                reply_markup=position_keyboard(f"d:{revision}:", draft, f"d:{revision}:back"),
            )
        elif action.startswith("position:"):
            await self.show_preview(message, user_id, replace(draft, caption_position=action.split(":")[1]))
        elif action.startswith("gap:"):
            await self.show_preview(message, user_id, replace(draft, caption_gap=int(action.split(":")[1])))
        elif action == "save":
            draft.waiting_text = False
            packs = await owned_packs(context.bot, user_id, self.library)
            keys = [[option("Создать новый", "new")]]
            keys.extend([option(pack["title"], f"put:{pack['id']}")] for pack in packs)
            keys.append([option("Назад к стикеру", "back"), home_button()])
            await message.reply_text("Куда сохранить стикер?", reply_markup=InlineKeyboardMarkup(keys))
        elif action == "new":
            self.naming.put(user_id, (revision, new_pack_name(user_id, context.bot.username)))
            await message.reply_text("Напишите название нового набора.", reply_markup=back_keyboard(f"d:{revision}:save", "Отменить ввод"))
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

        current = self.drafts.get(user_id)
        if current:
            await refresh_options(query, current, f"d:{revision}:", action, f"d:{revision}:back")


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
        BotCommand("start", "Главное меню"),
        BotCommand("settings", "Цвет обводки по умолчанию"),
        BotCommand("album", "Собрать набор из нескольких фото"),
        BotCommand("pack", "Мои наборы"),
        BotCommand("inbox", "Предложения от друзей"),
        BotCommand("remove", "Удалить стикер ответом на него"),
        BotCommand("back", "Назад к редактированию"),
        BotCommand("cancel", "Закрыть черновик"),
    ])


def build_app(token: str, database: Path) -> Application:
    bot = StickerBot(Preferences(database))
    app = Application.builder().token(token).concurrent_updates(False).post_init(register_commands).build()
    stores = (
        bot.drafts, bot.albums.sessions, bot.naming, bot.renaming,
        bot.proposals.targets, bot.proposals.previews,
    )
    for store in stores:
        store.bot = app.bot

    def saved(callback):
        async def handle(update, context):
            try:
                return await callback(update, context)
            finally:
                if update.effective_user:
                    for store in stores:
                        store.save_cached(update.effective_user.id)
        return handle

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", saved(bot.start), filters=private))
    app.add_handler(CommandHandler("settings", saved(bot.settings), filters=private))
    app.add_handler(CommandHandler("pack", saved(bot.my_pack), filters=private))
    app.add_handler(CommandHandler("inbox", saved(bot.inbox), filters=private))
    app.add_handler(CommandHandler("album", saved(bot.album), filters=private))
    app.add_handler(CommandHandler("remove", saved(bot.remove_sticker), filters=private))
    app.add_handler(CommandHandler("cancel", saved(bot.cancel), filters=private))
    app.add_handler(CommandHandler("back", saved(bot.back), filters=private))
    app.add_handler(MessageHandler(private & filters.PHOTO, saved(bot.photo)))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, saved(bot.text)))
    app.add_handler(CallbackQueryHandler(
        saved(bot.callback),
        pattern=r"^(?:home|resume|inbox|album|pack|rename|remove_help|settings|default:(?:none|white|pink|yellow|blue)|d:[0-9a-f]{12}:(?:back|undo|redo|layout|position:(?:top|bottom)|gap:(?:8|24)|colors|captions|custom|clear|save|new|put:[0-9]+|color:(?:none|white|pink|yellow|blue)|template:[0-2]))$",
    ))
    app.add_handler(CallbackQueryHandler(
        saved(bot.albums.callback),
        pattern=r"^a:[0-9a-f]{10}:(?:review|edit:[0-7]|overview|back|undo|redo|restore|colors|color:(?:none|white|pink|yellow|blue)|layout|position:(?:top|bottom)|gap:(?:8|24)|caption|clear|drop|closer|cutout|frame|save|new|put:[0-9]+|retry|cancel|resume|close)$",
    ))
    app.add_handler(CallbackQueryHandler(saved(bot.manage_pack), pattern=r"^p:(?:open|rename|invite):[0-9]+$"))
    app.add_handler(CallbackQueryHandler(
        saved(bot.proposals.callback),
        pattern=r"^g:(?:(?:send|cancel|back):[0-9a-f]{10}|(?:approve|reject):[0-9]+)$",
    ))
    app.add_error_handler(report_error)
    return app
