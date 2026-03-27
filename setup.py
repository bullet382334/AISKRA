#!/usr/bin/env python3
"""
Автоматическая настройка системы.
Читает secrets.txt → создаёт .env → проверяет токены → настраивает автозапуск.
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

OS = platform.system()  # 'Darwin', 'Windows', 'Linux'
PROJECT_DIR = Path(__file__).parent
BOT_DIR = PROJECT_DIR / "bot"
NOTION_DIR = PROJECT_DIR / "notion"


def check_python():
    v = sys.version_info
    if v < (3, 10):
        print(f"Нужен Python >= 3.10, сейчас {v.major}.{v.minor}")
        sys.exit(1)
    print(f"Python {v.major}.{v.minor}.{v.micro} — OK")


def install_deps():
    print("\nУстановка зависимостей...")
    deps = ["python-telegram-bot", "telethon", "httpx", "notion-client", "python-dotenv"]
    if OS == "Darwin":
        deps.append("rumps")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet"] + deps, check=True)
    print("Зависимости установлены.")


def read_secrets():
    secrets_file = PROJECT_DIR / "secrets.txt"
    if not secrets_file.exists():
        print("\nФайл secrets.txt не найден!")
        print("Создайте его по шаблону secrets.txt.example и запустите setup.py снова.")
        sys.exit(1)

    secrets = {}
    for line in secrets_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, val = line.split("=", 1)
            secrets[key.strip()] = val.strip()
    return secrets


def get_chat_ids(token):
    import httpx

    print("\nПолучаю chat ID из Telegram...")
    resp = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
    data = resp.json()
    if not data.get("ok"):
        print(f"Ошибка Telegram API: {data}")
        return None, None

    updates = data.get("result", [])
    group_id = None
    admin_id = None

    for u in updates:
        msg = u.get("message", {})
        chat = msg.get("chat", {})
        chat_type = chat.get("type", "")
        if chat_type in ("group", "supergroup") and not group_id:
            group_id = chat["id"]
        elif chat_type == "private" and not admin_id:
            admin_id = chat["id"]

    if not group_id:
        print("GROUP_CHAT_ID не найден. Напишите сообщение в группу и запустите setup.py снова.")
    else:
        print(f"GROUP_CHAT_ID: {group_id}")

    if not admin_id:
        print("ADMIN_CHAT_ID не найден. Напишите боту лично и запустите setup.py снова.")
    else:
        print(f"ADMIN_CHAT_ID: {admin_id}")

    return group_id, admin_id


def verify_telegram(token):
    import httpx
    resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    data = resp.json()
    if data.get("ok"):
        name = data["result"].get("first_name", "?")
        username = data["result"].get("username", "?")
        print(f"Telegram бот: {name} (@{username}) — OK")
        return True
    print("BOT_TOKEN невалидный!")
    return False


def verify_notion(token):
    import httpx
    if not token:
        print("NOTION_API_TOKEN не указан — пропускаю.")
        return False
    resp = httpx.get(
        "https://api.notion.com/v1/users/me",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
        timeout=10,
    )
    if resp.status_code == 200:
        print("Notion API — OK")
        return True
    print("NOTION_API_TOKEN невалидный!")
    return False


def verify_unsplash(key):
    import httpx
    if not key:
        print("UNSPLASH_ACCESS_KEY не указан — обложки не будут генерироваться.")
        return False
    resp = httpx.get(
        "https://api.unsplash.com/photos/random",
        headers={"Authorization": f"Client-ID {key}"},
        timeout=10,
    )
    if resp.status_code == 200:
        print("Unsplash API — OK")
        return True
    print("UNSPLASH_ACCESS_KEY невалидный!")
    return False


def create_env_files(secrets, group_id, admin_id):
    print("\nСоздаю .env файлы...")

    bot_env = BOT_DIR / ".env"
    bot_env.write_text(
        f"BOT_TOKEN={secrets.get('BOT_TOKEN', '')}\n"
        f"TELETHON_API_ID={secrets.get('TELETHON_API_ID', '')}\n"
        f"TELETHON_API_HASH={secrets.get('TELETHON_API_HASH', '')}\n"
        f"GROUP_CHAT_ID={group_id or ''}\n"
        f"ADMIN_CHAT_ID={admin_id or ''}\n"
        f"GROUP_ALLOWED_IDS=\n",
        encoding="utf-8",
    )
    print(f"  {bot_env}")

    notion_env = NOTION_DIR / ".env"
    notion_env.write_text(
        f"NOTION_API_TOKEN={secrets.get('NOTION_API_TOKEN', '')}\n"
        f"NOTION_ROOT_PAGE={secrets.get('NOTION_ROOT_PAGE', '')}\n"
        f"UNSPLASH_ACCESS_KEY={secrets.get('UNSPLASH_ACCESS_KEY', '')}\n"
        f"PERSON_TG_USERNAME={secrets.get('PERSON_TG_USERNAME', '')}\n"
        f"MAIN_PROJECT_KEYWORD={secrets.get('MAIN_PROJECT_KEYWORD', '')}\n",
        encoding="utf-8",
    )
    print(f"  {notion_env}")


def setup_macos_launchagent():
    project_name = PROJECT_DIR.name.lower().replace(" ", "-")
    label = f"com.{project_name}.bot"
    bot_py = str(BOT_DIR / "bot.py")
    work_dir = str(BOT_DIR)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key><array>
        <string>{sys.executable}</string>
        <string>{bot_py}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>WorkingDirectory</key><string>{work_dir}</string>
    <key>StandardOutPath</key><string>{work_dir}/bot.log</string>
    <key>StandardErrorPath</key><string>{work_dir}/bot.log</string>
</dict></plist>"""

    plist_path = Path.home() / f"Library/LaunchAgents/{label}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    print(f"\nLaunchAgent создан: {plist_path}")
    print(f"  Запуск: launchctl load {plist_path}")
    print(f"  Стоп:   launchctl unload {plist_path}")


def setup_platform():
    print("\nНастройка платформы...")
    if OS == "Darwin":
        setup_macos_launchagent()
    elif OS == "Windows":
        bat = BOT_DIR / "start-silent.bat"
        if bat.exists():
            print(f"start-silent.bat найден: {bat}")
        else:
            print("start-silent.bat не найден — фоновый запуск через трей недоступен.")
    else:
        print(f"Платформа: {OS}. Автозапуск нужно настроить вручную.")


def cleanup_secrets():
    secrets_file = PROJECT_DIR / "secrets.txt"
    if secrets_file.exists():
        secrets_file.unlink()
        print("\nsecrets.txt удалён. Токены распределены в bot/.env и notion/.env.")


def main():
    print("=" * 50)
    print("Настройка системы")
    print("=" * 50)

    check_python()
    install_deps()

    secrets = read_secrets()

    # Проверка токенов
    print("\nПроверка токенов...")
    token = secrets.get("BOT_TOKEN", "")
    tg_ok = verify_telegram(token) if token else False
    notion_ok = verify_notion(secrets.get("NOTION_API_TOKEN", ""))
    unsplash_ok = verify_unsplash(secrets.get("UNSPLASH_ACCESS_KEY", ""))

    # Получить chat ID
    group_id, admin_id = None, None
    if tg_ok:
        group_id, admin_id = get_chat_ids(token)

    # Создать .env
    create_env_files(secrets, group_id, admin_id)

    # Платформа
    setup_platform()

    # Очистка
    cleanup_secrets()

    # Итог
    print("\n" + "=" * 50)
    print("Итог:")
    print(f"  Telegram:  {'OK' if tg_ok else 'ОШИБКА'}")
    print(f"  Notion:    {'OK' if notion_ok else 'не настроен'}")
    print(f"  Unsplash:  {'OK' if unsplash_ok else 'не настроен'}")
    print(f"  Group ID:  {group_id or 'не получен'}")
    print(f"  Admin ID:  {admin_id or 'не получен'}")
    print()
    if not group_id or not admin_id:
        print("Напишите боту лично и в группу, затем запустите setup.py повторно.")
    else:
        print("Следующий шаг: python bot/bot.py (первый запуск для авторизации Telethon)")
    print("=" * 50)


if __name__ == "__main__":
    main()
