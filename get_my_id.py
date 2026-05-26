import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message

from config import get_settings

logging.basicConfig(level=logging.INFO)

router = Router()


@router.message()
async def echo_handler(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(f"Your Telegram ID: {message.from_user.id}")


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
