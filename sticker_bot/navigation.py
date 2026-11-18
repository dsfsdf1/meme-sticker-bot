from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup

from .images import COLORS


def home_button() -> Button:
    return Button("Главное меню", callback_data="home")


def back_keyboard(callback: str, label: str = "Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Button(label, callback_data=callback), home_button()]])


def color_keyboard(prefix: str, selected: str, back: str) -> InlineKeyboardMarkup:
    buttons = [
        Button(("✓ " if key == selected else "") + name, callback_data=f"{prefix}color:{key}")
        for key, (name, _) in COLORS.items()
    ]
    return InlineKeyboardMarkup([
        buttons[:1], buttons[1:3], buttons[3:],
        [Button("Назад к стикеру", callback_data=back), home_button()],
    ])


def position_keyboard(prefix: str, draft, back: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Button(("✓ " if draft.caption_position == place else "") + label, callback_data=f"{prefix}position:{place}")
         for place, label in (("top", "Сверху"), ("bottom", "Снизу"))],
        [Button(("✓ " if draft.caption_gap == gap else "") + label, callback_data=f"{prefix}gap:{gap}")
         for gap, label in ((8, "Ближе к фото"), (24, "Чуть дальше"))],
        [Button("Назад к стикеру", callback_data=back), home_button()],
    ])


def history_buttons(prefix: str, draft) -> list[Button]:
    buttons = []
    if draft.undo:
        buttons.append(Button("↶ Отменить правку", callback_data=f"{prefix}undo"))
    if draft.redo:
        buttons.append(Button("↷ Повторить правку", callback_data=f"{prefix}redo"))
    return buttons


async def refresh_options(query, draft, prefix: str, action: str, back: str) -> None:
    from telegram.error import TelegramError

    if action.startswith("color:"):
        keyboard = color_keyboard(prefix, draft.color, back)
    elif action.startswith(("position:", "gap:")):
        keyboard = position_keyboard(prefix, draft, back)
    else:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except TelegramError:
        # The new sticker has already been sent. A cosmetic refresh can be retried.
        pass
