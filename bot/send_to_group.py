"""
Отправка сообщений в групповой чат (для заказчика).
Без inline-кнопок — просто текст.

Формат файла: блоки, разделённые "---".
Каждый блок = одно сообщение (HTML).

Использование: python send_to_group.py msg-slug.txt
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])


async def main():
    from telegram import Bot
    from telegram.request import HTTPXRequest

    if len(sys.argv) < 2:
        print("Ispolzovanie: python send_to_group.py messages.txt")
        sys.exit(1)

    filepath = Path(sys.argv[1])
    if not filepath.exists():
        filepath = Path(__file__).parent / sys.argv[1]
    if not filepath.exists():
        print(f"Fajl ne najden: {filepath}")
        sys.exit(1)

    content = filepath.read_text(encoding="utf-8")
    blocks = [b.strip() for b in content.split("---") if b.strip()]
    if not blocks:
        print("Fajl pust")
        sys.exit(1)

    request = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0)
    bot = Bot(token=BOT_TOKEN, request=request)

    sent = 0
    for block in blocks:
        await bot.send_message(
            chat_id=GROUP_CHAT_ID, text=block,
            parse_mode="HTML",
        )
        sent += 1
        await asyncio.sleep(1.5)

    print(f"Otpravleno {sent} soobshchenij v gruppu.")


if __name__ == "__main__":
    asyncio.run(main())
