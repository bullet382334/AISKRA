"""
Отправка сообщений в ЛС админу с inline-кнопками Notion sync.
Кнопки обрабатывает запущенный бот (bot.py → handle_sync_callback).

Формат файла сообщений: блоки, разделённые "---".
Первая строка блока = slug файла (для callback_data).
Остальное = HTML-текст сообщения.

Использование: python send_to_admin.py pending_research_msgs.txt
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])


async def main():
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.request import HTTPXRequest

    if len(sys.argv) < 2:
        print("Ispolzovanie: python send_to_admin.py messages.txt")
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
        lines = block.split("\n", 1)
        slug = lines[0].strip()
        text = lines[1].strip() if len(lines) > 1 else slug

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("В Notion", callback_data=f"notion_sync:{slug}"),
            InlineKeyboardButton("Пропустить", callback_data=f"notion_skip:{slug}"),
            InlineKeyboardButton("Доработать", callback_data=f"edit_research:{slug}"),
        ]])

        await bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=text,
            parse_mode="HTML", reply_markup=keyboard,
        )
        sent += 1
        await asyncio.sleep(1)

    print(f"Otpravleno {sent} soobshchenij s knopkami.")


if __name__ == "__main__":
    asyncio.run(main())
