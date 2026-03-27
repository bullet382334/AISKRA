"""
Проверяет: есть ли .md файлы новее .notion_state.json?
Если да → шлёт уведомление в Telegram. Если нет → тишина.
Запускать по расписанию (Task Scheduler) или вручную.
"""

import json
import os
import urllib.request
import urllib.parse
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
STATE_FILE = PROJECT_DIR / "notion" / ".notion_state.json"

# Директории для отслеживания
WATCH_DIRS = [
    PROJECT_DIR / "karta-idej",
    PROJECT_DIR / "realizaciya",
    PROJECT_DIR / "project",
    PROJECT_DIR / "_sostoyaniye.md",
]


def load_telegram_config():
    """Читает BOT_TOKEN и ADMIN_CHAT_ID из bot/.env."""
    env_path = PROJECT_DIR / "bot" / ".env"
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values.get("BOT_TOKEN"), values.get("ADMIN_CHAT_ID")


def get_changed_files():
    """Возвращает список .md файлов новее state.json."""
    if not STATE_FILE.exists():
        return ["(state.json не найден — нужен первый sync)"]

    state_mtime = STATE_FILE.stat().st_mtime
    changed = []

    for item in WATCH_DIRS:
        if item.is_file() and item.suffix == ".md":
            if item.stat().st_mtime > state_mtime:
                changed.append(item.name)
        elif item.is_dir():
            for md in item.rglob("*.md"):
                if md.stat().st_mtime > state_mtime:
                    changed.append(str(md.relative_to(PROJECT_DIR)))

    return changed


def send_telegram(bot_token, chat_id, text):
    """Отправляет сообщение через Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
    }).encode("utf-8")
    urllib.request.urlopen(url, data, timeout=10)


def main():
    changed = get_changed_files()
    if not changed:
        return  # тишина

    bot_token, admin_id = load_telegram_config()
    if not bot_token or not admin_id:
        print("Нет BOT_TOKEN или ADMIN_CHAT_ID в bot/.env")
        return

    files_list = "\n".join(f"  - {f}" for f in changed[:10])
    if len(changed) > 10:
        files_list += f"\n  ...и ещё {len(changed) - 10}"

    text = (
        f"Изменились {len(changed)} файлов с последнего sync:\n"
        f"{files_list}\n\n"
        "Напиши sync боту для синхронизации."
    )
    send_telegram(bot_token, admin_id, text)
    print(f"Уведомление отправлено ({len(changed)} файлов)")


if __name__ == "__main__":
    main()
