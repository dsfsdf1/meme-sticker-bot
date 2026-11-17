import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError

from .bot import build_app


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for logger in ("httpx", "httpcore", "telegram"):
        logging.getLogger(logger).setLevel(logging.CRITICAL)

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Укажите BOT_TOKEN в локальном .env. Токен выдаёт Telegram-бот @BotFather.")
    database = Path(os.getenv("BOT_DB", "data/bot.sqlite3"))
    cache = os.environ.setdefault("U2NET_HOME", "data/models")
    Path(cache).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    try:
        build_app(token, database).run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
    except TelegramError as error:
        raise SystemExit(f"Ошибка запуска Telegram ({type(error).__name__}). Проверьте токен, сеть и второй экземпляр бота.") from None


if __name__ == "__main__":
    main()
