#!/usr/bin/env python3
"""
macOS menu bar indicator for the bot.
Analog of tray-indicator.ps1 for Windows.
Requires: pip install rumps
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

try:
    import rumps
except ImportError:
    print("Установите rumps: pip install rumps")
    sys.exit(1)

BOT_DIR = Path(__file__).parent
PID_FILE = BOT_DIR / "bot.pid"
ENV_FILE = BOT_DIR / ".env"


def _read_env():
    """Read .env file into dict."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _get_bot_name():
    """Get bot name from Telegram API."""
    env = _read_env()
    token = env.get("BOT_TOKEN", "")
    if not token:
        return "Bot"
    try:
        import httpx
        resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        data = resp.json()
        if data.get("ok"):
            return data["result"].get("first_name", "Bot")
    except Exception:
        pass
    return "Bot"


def _is_running():
    """Check if bot process is running."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # check if process exists
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


class BotIndicator(rumps.App):
    def __init__(self):
        bot_name = _get_bot_name()
        super().__init__(bot_name, quit_button=None)
        self.bot_name = bot_name
        self.menu = [
            rumps.MenuItem("Start Bot", callback=self.start_bot),
            rumps.MenuItem("Stop Bot", callback=self.stop_bot),
            None,  # separator
            rumps.MenuItem("Sync Notion", callback=self.sync_notion),
            None,
            rumps.MenuItem("Exit", callback=self.quit_app),
        ]
        self._update_status()
        self.timer = rumps.Timer(self._tick, 10)
        self.timer.start()

    def _tick(self, _):
        self._update_status()

    def _update_status(self):
        if _is_running():
            self.title = f"\U0001f7e2 {self.bot_name}"
        else:
            self.title = f"\U0001f534 {self.bot_name}"

    def start_bot(self, _):
        if _is_running():
            rumps.notification(self.bot_name, "", "Бот уже запущен")
            return
        bot_py = str(BOT_DIR / "bot.py")
        log_file = str(BOT_DIR / "bot.log")
        with open(log_file, "a") as log:
            proc = subprocess.Popen(
                [sys.executable, bot_py],
                cwd=str(BOT_DIR),
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        rumps.notification(self.bot_name, "", f"Бот запущен (PID {proc.pid})")
        self._update_status()

    def stop_bot(self, _):
        if not _is_running():
            rumps.notification(self.bot_name, "", "Бот не запущен")
            return
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            rumps.notification(self.bot_name, "", "Бот остановлен")
        except Exception as e:
            rumps.notification(self.bot_name, "", f"Ошибка: {e}")
        self._update_status()

    def sync_notion(self, _):
        notion_py = str(BOT_DIR.parent / "notion" / "update_notion.py")
        if not Path(notion_py).exists():
            rumps.notification(self.bot_name, "", "update_notion.py не найден")
            return
        subprocess.Popen(
            [sys.executable, notion_py],
            cwd=str(BOT_DIR.parent / "notion"),
            start_new_session=True,
        )
        rumps.notification(self.bot_name, "", "Sync запущен")

    def quit_app(self, _):
        rumps.quit_application()


if __name__ == "__main__":
    BotIndicator().run()
