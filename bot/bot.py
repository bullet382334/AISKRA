"""
Бот — транскрипция голосовых через Буквицу.
Накопитель: транскрибирует → копит в буфер → Claude только по команде /push.
"""

import os
import sys
from pathlib import Path

# Диагностика: логируем какой Python и sys.path ДО импортов
_diag_file = Path(__file__).parent / "_tray_crash.log"
try:
    with open(_diag_file, "a", encoding="utf-8") as _f:
        _f.write(f"\n--- startup PID={os.getpid()} exe={sys.executable} ---\n")
        _f.write(f"sys.path[:5] = {sys.path[:5]}\n")
except Exception:
    pass

import asyncio
import json
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update, Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telethon import TelegramClient

# --- Настройки ---
load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["TELETHON_API_ID"])
API_HASH = os.environ["TELETHON_API_HASH"]
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])

BUKVITSA_BOT = "BukvitsaAI_bot"
PROJECT_DIR = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_DIR / "transkriptsii"
BUFFER_FILE = Path(__file__).parent / "buffer.md"
PENDING_TASKS = Path(__file__).parent / "pending_tasks.txt"
PID_FILE = Path(__file__).parent / "bot.pid"
NPX = shutil.which("npx.cmd") or shutil.which("npx") or r"C:\Program Files\nodejs\npx.cmd"

RESEARCH_TIMEOUT = 1800  # 30 минут на исследование
SYNC_TIMEOUT = 900       # 15 минут на Notion sync
ASK_TIMEOUT = 120        # 2 минуты на вопрос
AUTOPUSH_THRESHOLD = 15          # авто-push если после обработки в буфере >= N сообщений
# Фильтр участников группы: ID (надёжно) → username → first_name (ненадёжно)
# GROUP_ALLOWED_IDS из .env — через запятую, например: 123456,789012
_raw_ids = os.environ.get("GROUP_ALLOWED_IDS", "")
GROUP_ALLOWED_IDS: set[int] = {int(x.strip()) for x in _raw_ids.split(",") if x.strip()}
GROUP_ALLOWED_USERNAMES: set[str] = set()  # заполняется автоматически из участников группы
GROUP_ALLOWED_NAMES: set[str] = set()  # fallback — заполняется из участников группы
GROUP_MEMBERS_FILE = Path(__file__).parent / "group_members.txt"
PENDING_VOICES = Path(__file__).parent / "pending_voices.json"

telethon_client = TelegramClient(
    str(Path(__file__).parent / "bukvitsa_session"), API_ID, API_HASH,
)
bukvitsa_lock = asyncio.Lock()
sync_lock = asyncio.Lock()
sync_proc: subprocess.Popen | None = None  # ссылка на процесс sync для возможности остановки
claude_busy = False
ask_queue: list = []  # [(message, question), ...] — очередь вопросов пока Claude занят
bot_ref: Bot | None = None
BOT_USER_ID: int = 0  # заполняется в post_init — для фильтрации своих сообщений в catchup
pending_edit: dict | None = None  # {"type": "tasks"} or {"type": "research", "slug": "..."} or {"type": "ask", ...}
_last_ask: dict | None = None     # {"question": ..., "answer": ...} — контекст последнего ask для "Уточнить"
_last_kb_msg = None               # Message — последнее сообщение с inline-кнопками (чтобы снять при новом)
_user_display_names: dict[int, str] = {}  # user_id → каноническое имя (заполняется в post_init)
_autopush_scheduled = False  # защита от повторных авто-push
_push_start_count = 0  # сколько было в буфере при старте push (для корректного отображения)


def _resolve_name(first: str, last: str = "") -> str:
    """Определить короткое имя из first_name/last_name полей Telegram."""
    if first in GROUP_ALLOWED_NAMES:
        return first
    for name in GROUP_ALLOWED_NAMES:
        if first.startswith(name):
            return name
    if last in GROUP_ALLOWED_NAMES:
        return last
    return first or "?"


def _display_name(user) -> str:
    """Отображаемое имя: по ID (каноническое) → fallback по first/last name."""
    uid = getattr(user, "id", None)
    if uid and uid in _user_display_names:
        return _user_display_names[uid]
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    resolved = _resolve_name(first, last)
    if uid:
        _user_display_names[uid] = resolved
    return resolved


def is_group_member(user) -> bool:
    """Проверяет, разрешён ли пользователь: по ID → username → first_name."""
    if not user:
        return False
    if getattr(user, "is_bot", False):
        return False
    if GROUP_ALLOWED_IDS and user.id in GROUP_ALLOWED_IDS:
        return True
    username = getattr(user, "username", None) or ""
    if GROUP_ALLOWED_USERNAMES and username.lower() in GROUP_ALLOWED_USERNAMES:
        return True
    first_name = getattr(user, "first_name", "") or ""
    return first_name in GROUP_ALLOWED_NAMES


def is_group_member_telethon(sender) -> bool:
    """То же для Telethon sender."""
    if not sender:
        return False
    if getattr(sender, "bot", False):
        return False
    sender_id = getattr(sender, "id", None)
    if BOT_USER_ID and sender_id == BOT_USER_ID:
        return False
    if GROUP_ALLOWED_IDS and sender_id in GROUP_ALLOWED_IDS:
        return True
    username = getattr(sender, "username", None) or ""
    if GROUP_ALLOWED_USERNAMES and username.lower() in GROUP_ALLOWED_USERNAMES:
        return True
    first_name = getattr(sender, "first_name", "") or ""
    return first_name in GROUP_ALLOWED_NAMES


def is_admin(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.id == ADMIN_CHAT_ID and chat.type == "private")


async def _safe_reply(reply_fn, text, retries=3, delay=3, **kwargs):
    """reply_fn с retry при сетевых ошибках. Не дублиру��т: retry только если точно не доставлено."""
    for attempt in range(retries):
        try:
            return await reply_fn(text, **kwargs)
        except (NetworkError, TimedOut) as e:
            if attempt < retries - 1:
                print(f"  [retry {attempt+1}/{retries}] {type(e).__name__}, жду {delay}с...")
                await asyncio.sleep(delay)
                delay *= 2
            else:
                print(f"  [!] Не удалось отправить после {retries} попыток: {e}")
                raise


async def _remove_prev_kb():
    """Убрать inline-кнопки с предыдущего сообщения (если есть)."""
    global _last_kb_msg
    if _last_kb_msg:
        try:
            await _last_kb_msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        _last_kb_msg = None


# --- Буфер ---

def buffer_append(sender: str, duration_str: str, text: str, message_id: int = 0):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg_tag = f" | msg:{message_id}" if message_id else ""
    entry = f"### {now} | {sender} ({duration_str}){msg_tag}\n{text}\n\n---\n\n"
    with open(BUFFER_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
    _maybe_schedule_autopush()


def _maybe_schedule_autopush():
    """Проверяет порог буфера и планирует авто-push если нужно."""
    global _autopush_scheduled
    if _autopush_scheduled or claude_busy:
        return
    count = buffer_count()
    if count >= AUTOPUSH_THRESHOLD:
        _autopush_scheduled = True
        print(f"  [autopush] Буфер: {count} >= {AUTOPUSH_THRESHOLD}, планирую авто-push")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_autopush())
        except RuntimeError:
            _autopush_scheduled = False


def buffer_update(message_id: int, new_text: str) -> bool:
    """Обновить текст записи в буфере по msg:ID. Возвращает True если нашёл и обновил."""
    if not BUFFER_FILE.exists() or not message_id:
        return False
    content = BUFFER_FILE.read_text(encoding="utf-8")
    tag = f"msg:{message_id}"
    if tag not in content:
        return False
    # Разбить на блоки по "\n\n---\n\n" (точный разделитель из buffer_append)
    sep = "\n\n---\n\n"
    blocks = content.split(sep)
    updated = False
    for i, block in enumerate(blocks):
        if tag in block:
            # Найти заголовок (### ...) и заменить тело
            lines = block.split("\n", 1)
            if len(lines) == 2:
                blocks[i] = lines[0] + "\n" + new_text
                updated = True
            break
    if updated:
        BUFFER_FILE.write_text(sep.join(blocks), encoding="utf-8")
    return updated


def buffer_msg_ids() -> set[int]:
    """Извлечь все msg:ID из текущего буфера для дедупликации."""
    if not BUFFER_FILE.exists():
        return set()
    content = BUFFER_FILE.read_text(encoding="utf-8")
    return {int(m) for m in re.findall(r"msg:(\d+)", content)}


def buffer_read() -> str:
    if not BUFFER_FILE.exists():
        return ""
    return BUFFER_FILE.read_text(encoding="utf-8").strip()


def buffer_clear(original_raw: str = ""):
    """Архивирует обработанную часть буфера. Новые записи, пришедшие во время /push, сохраняются."""
    if not BUFFER_FILE.exists():
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    archive = BUFFER_FILE.with_name(f"buffer_{ts}.md")
    if original_raw:
        current = BUFFER_FILE.read_text(encoding="utf-8")
        if current.startswith(original_raw) and len(current) > len(original_raw):
            # Новые записи дописались во время обработки — сохраняем их
            new_part = current[len(original_raw):]
            archive.write_text(original_raw, encoding="utf-8")
            BUFFER_FILE.write_text(new_part, encoding="utf-8")
            return
    BUFFER_FILE.rename(archive)


def buffer_count() -> int:
    content = buffer_read()
    return content.count("### ") if content else 0


def buffer_count_display() -> str:
    """Счётчик буфера для отображения. Во время push показывает только новые."""
    total = buffer_count()
    if claude_busy and _push_start_count:
        new = total - _push_start_count
        return f"+{new}" if new >= 0 else str(total)
    return str(total)


# --- Буквица (транскрипция) ---

async def transcribe_via_bukvitsa(audio_path: str) -> str:
    """Отправляет аудио в Буквицу, ждёт транскрипцию (txt-файл или текст)."""
    sent_filename = Path(audio_path).name

    async with bukvitsa_lock:
        entity = await telethon_client.get_input_entity(BUKVITSA_BOT)
        sent = await telethon_client.send_file(entity, audio_path)
        sent_id = sent.id
        print(f"  Отправлено в Буквицу (msg_id={sent_id}), жду ответ...")

        def is_our_response(msg):
            if msg.out:
                return False
            if msg.text and sent_filename in msg.text:
                return True
            # Буквица отвечает «обработан» + «Расшифровка:» без имени файла
            if msg.text and "обработан" in msg.text.lower():
                return True
            stem = Path(sent_filename).stem
            if msg.document:
                for attr in msg.document.attributes:
                    if hasattr(attr, "file_name") and attr.file_name and stem in attr.file_name:
                        return True
            return False

        for attempt in range(120):  # до 10 минут
            await asyncio.sleep(5)
            messages = await telethon_client.get_messages(entity, min_id=sent_id, limit=10)
            if not messages:
                continue
            our_messages = [m for m in messages if is_our_response(m)]
            if not our_messages:
                continue

            # Приоритет: txt-файл
            for msg in our_messages:
                if msg.document:
                    for attr in msg.document.attributes:
                        if (
                            hasattr(attr, "file_name")
                            and attr.file_name
                            and attr.file_name.endswith(".txt")
                        ):
                            data = await telethon_client.download_media(msg, file=bytes)
                            return data.decode("utf-8").strip()

            # Запасной: текст из сообщения
            for msg in our_messages:
                if msg.text and "обработан" in msg.text:
                    for marker in ["Расшифровка:", "Транскрибация:"]:
                        if marker in msg.text:
                            extracted = msg.text.split(marker, 1)[1]
                            if "Создано в Буквица" in extracted:
                                extracted = extracted.split("Создано в Буквица")[0]
                            text = extracted.strip()
                            if text:
                                await asyncio.sleep(15)
                                msgs2 = await telethon_client.get_messages(
                                    entity, min_id=sent_id, limit=10
                                )
                                for m in msgs2:
                                    if is_our_response(m) and m.document:
                                        for a in m.document.attributes:
                                            if (
                                                hasattr(a, "file_name")
                                                and a.file_name
                                                and a.file_name.endswith(".txt")
                                            ):
                                                data = await telethon_client.download_media(
                                                    m, file=bytes
                                                )
                                                return data.decode("utf-8").strip()
                                return text

    raise TimeoutError("Буквица не ответила за 10 минут")


# --- Claude CLI ---

CREATE_NEW_PROCESS_GROUP = 0x00000200


def run_claude(prompt: str, timeout: int = 600, model: str = "sonnet",
               max_thinking: int | None = None) -> tuple[bool, str]:
    """Запускает Claude CLI. Возвращает (success, output)."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if max_thinking is not None:
        env["MAX_THINKING_TOKENS"] = str(max_thinking)
    try:
        proc = subprocess.Popen(
            [NPX, "claude", "-p", "-",
             "--model", model,
             "--permission-mode", "bypassPermissions"],
            cwd=str(PROJECT_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
            creationflags=CREATE_NEW_PROCESS_GROUP,
        )
        stdout, stderr = proc.communicate(input=prompt.encode("utf-8"), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            errors = stderr.decode("utf-8", errors="replace")
            output += f"\n[stderr]: {errors[-500:]}" if errors else ""
        return proc.returncode == 0, output
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return False, "Claude не уложился в таймаут"
    except Exception as e:
        return False, str(e)


def build_push_prompt(buffer_content: str) -> str:
    """Контекст проекта + буфер → промпт для Claude."""
    parts = []
    for name, path in [
        ("_sostoyaniye.md", PROJECT_DIR / "_sostoyaniye.md"),
        ("digest.md", PROJECT_DIR / "karta-idej" / "digest.md"),
        ("katalog.yaml", PROJECT_DIR / "katalog.yaml"),
        ("index.yaml", PROJECT_DIR / "index.yaml"),
        ("realizaciya/index.md", PROJECT_DIR / "realizaciya" / "index.md"),
    ]:
        if path.exists():
            parts.append(f"--- {name} ---\n{path.read_text(encoding='utf-8')}")

    context = "\n\n".join(parts)
    return (
        f"## Состояние проекта\n{context}\n\n"
        f"## Новые транскрипции\n{buffer_content}\n\n"
        "## Задача\n"
        "Несколько голосовых от одного человека подряд (2-3 мин) на одну тему = переформулировка. "
        "Используй ПОСЛЕДНЕЕ, не дублируй.\n"
        "Обработай транскрипции по workflow обработки (skill workflow-obrabotki).\n"
        "Создавай карточки в inbox/ и karta-idej/.\n"
        "НЕ ТРОГАЙ файлы в realizaciya/ — только по прямому запросу.\n"
        "НЕ запускай web search без прямого указания.\n"
        "Обнови digest.md и _sostoyaniye.md. Лимиты строк — из CLAUDE.md.\n"
        "\n"
        "## Размещение новой информации (ОБЯЗАТЕЛЬНО)\n"
        "Используй katalog.yaml для навигации:\n"
        "- Упоминается сущность из каталога → ДОПОЛНИ существующий файл\n"
        "- Новая сущность (человек, место, идея, решение) → СОЗДАЙ файл + ДОБАВЬ в katalog.yaml\n"
        "- Противоречит существующему факту → НЕ ЗАТИРАЙ, сохрани оба варианта\n"
        "- Непонятно куда → inbox/\n"
        "\n"
        "## Задачи и исследования\n"
        "Если заказчик в голосовых просит что-то сделать, узнать, проверить, посчитать, "
        "исследовать, подсказать, найти информацию — сохрани задачи в bot/pending_tasks.txt.\n"
        "ВАЖНО: Вопрос заказчика к AI (\"найди\", \"подскажи\", \"какие ещё\", \"что думаешь\") "
        "= задача [research] или [do]. НЕ отвечай на вопрос в карточке — создай задачу!\n"
        "Формат: одна задача — один блок, разделитель ---\n"
        "Каждый блок: первая строка — [do] или [research] + краткое название, "
        "вторая — что конкретно нужно сделать.\n"
        "[do] = быстрое действие (позвонить, спросить, отправить, написать, оформить).\n"
        "[research] = исследование AI (проверить, сравнить, найти, оценить, посчитать, подсказать).\n"
        "Пример:\n"
        "[research] Юридический анализ\n"
        "Проверить ограничения для строительства, правовой статус\n"
        "---\n"
        "[do] Уточнить условия\n"
        "Связаться с контактом, запросить недостающую информацию\n"
        "---\n"
        "ВАЖНО: Сверься с realizaciya/index.md — там список ГОТОВЫХ исследований.\n"
        "- Тема уже исследована и заказчик НЕ просит обновить → НЕ предлагай.\n"
        "- Тема исследована, но заказчик просит новые данные → предложи как ОБНОВЛЕНИЕ.\n"
        "- Тема новая → предложи как новое исследование.\n"
        "Если явных запросов нет — НЕ создавай pending_tasks.txt.\n"
        "\n"
        "## Управление списком задач в _sostoyaniye.md\n"
        "Секция «Ждём и делаем» — АКТИВНЫЙ список. При каждом push:\n"
        "1. ДОБАВЬ новые задачи из транскрипций (сверху списка — самые свежие)\n"
        "2. УДАЛИ выполненные (транскрипция содержит ответ/результат)\n"
        "3. УДАЛИ устаревшие (контекст изменился, задача потеряла смысл)\n"
        "Не просто копируй старый список — ПЕРЕСМОТРИ каждый пункт.\n"
        "\n"
        "Действуй автономно, без вопросов."
    )


def build_research_prompt(task: str) -> str:
    """Контекст проекта + задача → промпт для исследования."""
    parts = []
    for name, path in [
        ("_sostoyaniye.md", PROJECT_DIR / "_sostoyaniye.md"),
        ("digest.md", PROJECT_DIR / "karta-idej" / "digest.md"),
        ("katalog.yaml", PROJECT_DIR / "katalog.yaml"),
        ("index.yaml", PROJECT_DIR / "index.yaml"),
        ("realizaciya/index.md", PROJECT_DIR / "realizaciya" / "index.md"),
    ]:
        if path.exists():
            parts.append(f"--- {name} ---\n{path.read_text(encoding='utf-8')}")

    context = "\n\n".join(parts)
    return (
        f"## Контекст проекта\n{context}\n\n"
        f"## Задача на исследование\n{task}\n\n"
        "## Инструкции\n"
        "Проведи исследование по задаче выше.\n"
        "Следуй workflow реализации (skill workflow-realizaciya).\n"
        "ОБЯЗАТЕЛЬНО используй web search для актуальных данных.\n"
        "Создай файл в realizaciya/ по шаблону из workflow.\n"
        "Обнови realizaciya/index.md, index.yaml, katalog.yaml и _sostoyaniye.md.\n"
        "В _sostoyaniye.md: УДАЛИ задачи [do]/[research], которые исследование закрыло. "
        "Новые задачи добавляй с тегом [do] или [research]: '- [do] Спросить...' или '- [research] Проверить...'. "
        "Без тега = ожидание от других людей. Каждая строка — одно действие, коротко. БЕЗ ссылок, телефонов, подробностей.\n"
        "Добавь новое исследование в katalog.yaml (тип: исследование, связи, файлы).\n"
        "ОБЯЗАТЕЛЬНО создай файл bot/msg-{slug}.txt где {slug} — ТОЧНО совпадает с именем файла в realizaciya/{slug}.md.\n"
        "Пример: если файл realizaciya/kredit-analiz.md → msg-файл bot/msg-kredit-analiz.txt. Slug ОБЯЗАН совпадать!\n"
        "Формат msg-файла — по правилам из skill format-research-tg:\n"
        "  - Блоки разделены ---\n"
        "  - HTML parse_mode: <b>жирный</b> (НЕ Markdown **)\n"
        "  - Блок 1: эмодзи + <b>заголовок</b> + суть (1-2 строки)\n"
        "  - Блоки 2-N: чеклист из 'Что делать', сгруппированный по темам с эмодзи\n"
        "  - Последний блок: 🔑 <b>Самое срочное:</b> что делать прямо сейчас\n"
        "Действуй автономно, без вопросов."
    )


def parse_plan() -> dict:
    """Задачи [do] и [research] из _sostoyaniye.md (по тегам, без привязки к секции)."""
    result = {'do': [], 'research': [], 'wait': []}
    path = PROJECT_DIR / "_sostoyaniye.md"
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().lstrip("- ").replace("**", "")
        if stripped.startswith("[do] "):
            result['do'].append(stripped[5:])
        elif stripped.startswith("[research] "):
            result['research'].append(stripped[11:])
    return result


def remove_from_plan(task_text: str):
    """Удаляет выполненную задачу из _sostoyaniye.md."""
    path = PROJECT_DIR / "_sostoyaniye.md"
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    needle = task_text[:40]
    new_lines = [l for l in lines if needle not in l]
    if len(new_lines) < len(lines):
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def add_tasks_to_sostoyaniye(new_tasks: list[str]):
    """Добавляет задачи в _sostoyaniye.md (перед '## Следующий шаг AI')."""
    path = PROJECT_DIR / "_sostoyaniye.md"
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    marker = "## Следующий шаг AI"
    if marker in content:
        insert_lines = "\n".join(f"- **{t.splitlines()[0]}**" for t in new_tasks)
        content = content.replace(marker, f"{insert_lines}\n\n{marker}")
    else:
        insert_lines = "\n".join(f"- **{t.splitlines()[0]}**" for t in new_tasks)
        content = content.rstrip() + "\n" + insert_lines + "\n"
    path.write_text(content, encoding="utf-8")


def read_pending_tasks() -> list[str]:
    """Читает pending_tasks.txt, возвращает список задач."""
    if not PENDING_TASKS.exists():
        return []
    return [t.strip() for t in PENDING_TASKS.read_text(encoding="utf-8").split("---") if t.strip()]


def get_git_diff_stat() -> str:
    try:
        r = subprocess.run(
            ["git", "diff", "--stat"], cwd=str(PROJECT_DIR),
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() or "(нет незакоммиченных изменений)"
    except Exception:
        return "(не удалось прочитать git)"



# --- Исследования: отправка в группу заказчика ---

def _get_notion_url(slug: str) -> str:
    """Возвращает ссылку на страницу Реализации в Notion."""
    state_file = PROJECT_DIR / "notion" / ".notion_state.json"
    if not state_file.exists():
        return ""
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        db_id = state.get("realizaciya_gallery_db_id", "")
        if db_id:
            return f"https://notion.so/{db_id.replace('-', '')}"
    except Exception:
        pass
    return ""


def _extract_research_messages(filepath: str) -> list[str]:
    """Fallback: извлекает сообщения для группы из файла исследования (если msg-файл не создан)."""
    full_path = PROJECT_DIR / filepath
    if not full_path.exists():
        return []

    content = full_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    # Title
    title = Path(filepath).stem
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    # "Главное" section
    glavnoe = []
    in_section = False
    for line in lines:
        if in_section:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped:
                stripped = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', stripped)
                glavnoe.append(stripped)
        elif line.startswith("## ") and "Главное" in line:
            in_section = True

    # "Что делать" section
    chto_delat = []
    in_section = False
    for line in lines:
        if in_section:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped:
                stripped = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', stripped)
                chto_delat.append(stripped)
        elif line.startswith("## ") and "Что делать" in line:
            in_section = True

    messages = []

    # Message 1: Summary
    summary_body = "\n".join(glavnoe[:10]) if glavnoe else "Новое исследование готово."
    messages.append(f"\U0001f4ca <b>{title}</b>\n\n{summary_body}")

    # Message 2: Checklist
    if chto_delat:
        checklist = "\n".join(chto_delat)
        messages.append(f"\U0001f4cb <b>Что делать</b>\n\n{checklist}")

    return messages


async def _send_research_to_group(bot: Bot, new_research: list[dict]):
    """Отправляет уведомление + чеклист по новому исследованию в группу заказчика.

    new_research: [{"path": "realizaciya/slug.md", "title": "..."}]
    После Notion sync: ссылка на Notion + msg-файл или fallback.
    """
    for item in new_research:
        filepath = item["path"]
        slug = Path(filepath).stem
        msg_file = Path(__file__).parent / f"msg-{slug}.txt"

        # Notion URL (может быть пустой, если sync ещё не записал)
        notion_url = _get_notion_url(slug)
        notion_line = f'\n\n<a href="{notion_url}">Открыть в Notion</a>' if notion_url else ""

        if msg_file.exists():
            # Claude создал файл сообщений — отправляем как есть
            raw = msg_file.read_text(encoding="utf-8")
            blocks = [b.strip() for b in raw.split("---") if b.strip()]

            # Добавляем Notion-ссылку к первому блоку
            if blocks and notion_url:
                blocks[0] += notion_line

            for block in blocks:
                try:
                    await bot.send_message(
                        chat_id=GROUP_CHAT_ID, text=block,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    print(f"  Ошибка отправки в группу: {e}")
                await asyncio.sleep(1.5)
        else:
            # Fallback: извлекаем из файла исследования
            messages = _extract_research_messages(filepath)
            if not messages:
                continue

            # Добавляем Notion-ссылку к первому сообщению
            if notion_url:
                messages[0] += notion_line

            for msg in messages:
                try:
                    await bot.send_message(
                        chat_id=GROUP_CHAT_ID, text=msg,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    print(f"  Ошибка отправки в группу: {e}")
                await asyncio.sleep(1.5)

        print(f"  Отправлено в группу: {slug} ({'msg-файл' if msg_file.exists() else 'fallback'})")


async def _do_sync(reply_fn):
    """Единая точка Notion sync — для /sync и callback-кнопок."""
    global sync_proc
    if sync_lock.locked():
        await _safe_reply(reply_fn, "Sync уже идёт, подожди.")
        return
    async with sync_lock:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Остановить sync", callback_data="stop_sync")],
        ])
        status_msg = await _safe_reply(reply_fn, "Синхронизирую в Notion...", reply_markup=kb)
        try:
            sync_env = os.environ.copy()
            sync_env["PYTHONIOENCODING"] = "utf-8"
            sync_proc = subprocess.Popen(
                ["python", "-u", "update_notion.py"],
                cwd=str(PROJECT_DIR / "notion"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=sync_env,
            )
            loop = asyncio.get_event_loop()
            stdout, stderr = await loop.run_in_executor(
                None, lambda: sync_proc.communicate(timeout=SYNC_TIMEOUT),
            )
            try:
                if status_msg:
                    await status_msg.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            if sync_proc.returncode == 0:
                plan = parse_plan()
                plan_text = _format_plan(plan)
                if plan_text:
                    hints = []
                    if plan['do']:
                        hints.append("/do N — быстрая задача")
                    if plan['research']:
                        hints.append("/research N — исследование")
                    hint_text = "\n\n" + "\n".join(hints) if hints else ""
                    await _safe_reply(reply_fn, f"Sync OK.\n\n{plan_text}{hint_text}")
                else:
                    await _safe_reply(reply_fn, "Sync OK. План пуст.")
            else:
                err = ""
                progress_file = PROJECT_DIR / "notion" / ".sync_progress.json"
                if progress_file.exists():
                    try:
                        prog = json.loads(progress_file.read_text(encoding="utf-8"))
                        err = prog.get("error", "")
                    except Exception:
                        pass
                if not err:
                    err = stderr.decode("utf-8", errors="replace")[-500:] if stderr else "нет деталей"
                await _safe_reply(reply_fn, f"Sync ошибка: {err}")
        except subprocess.TimeoutExpired:
            if sync_proc:
                sync_proc.kill()
            await _safe_reply(reply_fn, f"Sync: таймаут ({SYNC_TIMEOUT // 60} мин), процесс остановлен.")
        except (NetworkError, TimedOut):
            raise  # пробросить наверх для обработки в cmd_push
        except Exception as e:
            await _safe_reply(reply_fn, f"Ошибка: {e}")
        finally:
            sync_proc = None



# --- Callback-обработчик (inline-кнопки) ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_edit, _last_ask
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    await query.answer()

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if data == "stop_sync":
        if sync_proc and sync_proc.poll() is None:
            sync_proc.kill()
            await query.message.reply_text("Sync остановлен.")
        else:
            await query.message.reply_text("Sync не запущен.")
        return
    elif data.startswith("edit_research:"):
        slug = data.split(":", 1)[1]
        if pending_edit:
            old = pending_edit.get("slug", pending_edit.get("type", "?"))
            await query.message.reply_text(f"(предыдущая правка '{old}' отменена)")
        pending_edit = {"type": "research", "slug": slug}
        await query.message.reply_text("Напиши, что доработать в исследовании (это займёт до 30 мин):")
    elif data == "edit_tasks":
        pending_edit = {"type": "tasks"}
        await query.message.reply_text("Напиши, что изменить в задачах:")
    elif data == "approve_tasks":
        pending = read_pending_tasks()
        if not pending:
            await query.message.reply_text("Задачи уже обработаны.")
            return
        add_tasks_to_sostoyaniye(pending)
        PENDING_TASKS.unlink()
        names = "\n".join(f"  {i+1}. {t.splitlines()[0]}" for i, t in enumerate(pending))
        await query.message.reply_text(f"Добавлено в план ({len(pending)}):\n{names}")
    elif data == "skip_tasks":
        if PENDING_TASKS.exists():
            PENDING_TASKS.unlink()
        await query.message.reply_text("Задачи пропущены.")
    elif data == "ask_send":
        # Отправить ответ Claude в группу
        answer_text = query.message.text_html or query.message.text or ""
        if answer_text:
            try:
                await context.bot.send_message(GROUP_CHAT_ID, answer_text, parse_mode="HTML")
                await query.message.reply_text("Отправлено в группу.")
            except Exception as e:
                await query.message.reply_text(f"Ошибка отправки: {e}")
        else:
            await query.message.reply_text("Нет текста для отправки.")
        _last_ask = None
    elif data == "ask_continue":
        if _last_ask:
            if pending_edit:
                await query.message.reply_text("(предыдущая правка отменена)")
            pending_edit = {"type": "ask", "ts": time.time(), **_last_ask}
            await query.message.reply_text("Что уточнить или сделать?")
        else:
            await query.message.reply_text("Контекст потерян. Задай вопрос заново.")
    elif data == "ask_done":
        _last_ask = None
        await query.message.reply_text("Ок.")


# --- Команды бота (/slash) ---

async def _push_core(reply_fn):
    """Ядро push-обработки. reply_fn — async callable(text, **kw) для отправки сообщений."""
    global claude_busy, _push_start_count

    # Очистить старые pending_tasks — новый push перезапишет если нужно
    if PENDING_TASKS.exists():
        PENDING_TASKS.unlink()

    content = buffer_read()
    if not content:
        await reply_fn("Буфер пуст.")
        return
    if claude_busy:
        await reply_fn("Claude занят, подожди.")
        return
    claude_busy = True

    try:
        count = buffer_count()
        _push_start_count = count
        # Снимок raw-содержимого ДО обработки (для сравнения после)
        raw_snapshot = BUFFER_FILE.read_text(encoding="utf-8") if BUFFER_FILE.exists() else ""
        status_msg = await reply_fn(f"Обработка {count} транскрипций...")

        # Снимок файлов до обработки
        _watch_dirs = [PROJECT_DIR / d for d in ("inbox", "karta-idej", "project", "kontekst")]
        _watch_files = [PROJECT_DIR / f for f in ("katalog.yaml", "karta-idej/digest.md", "_sostoyaniye.md")]
        _snap = {}
        for d in _watch_dirs:
            if d.exists():
                for f in d.rglob("*.md"):
                    _snap[str(f)] = f.stat().st_mtime
        for f in _watch_files:
            if f.exists():
                _snap[str(f)] = f.stat().st_mtime

        async def _heartbeat():
            start = asyncio.get_event_loop().time()
            while True:
                await asyncio.sleep(30)
                elapsed = int(asyncio.get_event_loop().time() - start)
                mins, secs = elapsed // 60, elapsed % 60
                # Новые/изменённые файлы
                changed = []
                for d in _watch_dirs:
                    if d.exists():
                        for f in d.rglob("*.md"):
                            key = str(f)
                            if key not in _snap or f.stat().st_mtime > _snap[key]:
                                changed.append(f.name)
                for f in _watch_files:
                    if f.exists():
                        key = str(f)
                        if key not in _snap or f.stat().st_mtime > _snap[key]:
                            changed.append(f.name)
                files_info = f" | +{len(changed)} файлов" if changed else ""
                try:
                    await status_msg.edit_text(f"Обработка {count} транскрипций...\n{mins}:{secs:02d}{files_info}")
                except Exception:
                    pass

        heartbeat_task = asyncio.create_task(_heartbeat())
        prompt = build_push_prompt(content)
        loop = asyncio.get_event_loop()
        success, output = await loop.run_in_executor(None, run_claude, prompt, 1800)
    finally:
        claude_busy = False
        _push_start_count = 0
        heartbeat_task.cancel()
        if ask_queue:
            asyncio.create_task(_process_ask_queue())

    if success:
        buffer_clear(raw_snapshot)
        diff = await asyncio.get_event_loop().run_in_executor(None, get_git_diff_stat)

        # Отправка результатов с retry (сеть может быть нестабильной)
        try:
            await _safe_reply(reply_fn, f"Обработано.\n\n{diff}")
        except (NetworkError, TimedOut):
            pass  # Логируется внутри _safe_reply, продолжаем

        pending = read_pending_tasks()
        if pending:
            add_tasks_to_sostoyaniye(pending)
            PENDING_TASKS.unlink(missing_ok=True)

        try:
            await _do_sync(reply_fn)
        except (NetworkError, TimedOut):
            print("  [!] Sync-сообщение не отправлено (сеть)")

        # Перепроверить буфер: во время обработки могли накопиться новые сообщения
        _maybe_schedule_autopush()
    else:
        summary = output[-500:] if output else "нет деталей"
        await _safe_reply(reply_fn, f"Ошибка Claude. Буфер сохранён, можно /push повторно.\n{summary}")


async def _autopush():
    """Авто-push: срабатывает когда буфер достигает AUTOPUSH_THRESHOLD.
    Планируется из _maybe_schedule_autopush (вызывается после buffer_append)."""
    global _autopush_scheduled
    try:
        if not bot_ref or not ADMIN_CHAT_ID:
            print("  [autopush] Нет bot_ref или ADMIN_CHAT_ID, пропуск")
            return

        # Перепроверяем условия (могли измениться между планированием и запуском)
        if claude_busy:
            print("  [autopush] Claude занят, пропуск")
            return
        count = buffer_count()
        if count < AUTOPUSH_THRESHOLD:
            print(f"  [autopush] Буфер: {count} < {AUTOPUSH_THRESHOLD}, пропуск")
            return

        # Свежий reply_fn через bot_ref — не зависит от старого message
        reply_fn = lambda text, **kw: bot_ref.send_message(chat_id=ADMIN_CHAT_ID, text=text, **kw)
        try:
            await _safe_reply(reply_fn, f"Авто-push: в буфере {count} сообщений, запускаю обработку...")
            await _push_core(reply_fn)
        except Exception as e:
            print(f"  [autopush] Ошибка: {e}")
            try:
                await bot_ref.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"Авто-push не удался: {e}\nБуфер сохранён, можно /push вручную.",
                )
            except Exception:
                pass
    finally:
        _autopush_scheduled = False


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/push — обработать буфер через Claude."""
    if not is_admin(update):
        return
    await _push_core(update.message.reply_text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status — показать состояние."""
    if not is_admin(update):
        return
    count = buffer_count_display()
    busy = "обрабатывает" if claude_busy else "свободен"
    plan = parse_plan()
    plan_tasks = len(plan['do']) + len(plan['research'])
    # Очистка протухшего pending_tasks.txt
    if PENDING_TASKS.exists():
        pending = read_pending_tasks()
        if pending:
            state = (PROJECT_DIR / "_sostoyaniye.md").read_text(encoding="utf-8") if (PROJECT_DIR / "_sostoyaniye.md").exists() else ""
            fresh = [t for t in pending if t.splitlines()[0][:30] not in state]
            if not fresh:
                PENDING_TASKS.unlink(missing_ok=True)
        else:
            PENDING_TASKS.unlink(missing_ok=True)

    # Notion: сколько исследований в очереди на sync
    notion_state_file = PROJECT_DIR / "notion" / ".notion_state.json"
    realizaciya_dir = PROJECT_DIR / "realizaciya"
    notion_line = ""
    if realizaciya_dir.exists():
        skip = {"index.md"}
        all_files = [f.stem for f in realizaciya_dir.glob("*.md") if f.name not in skip]
        synced = set()
        if notion_state_file.exists():
            import json as _json
            try:
                ns = _json.loads(notion_state_file.read_text(encoding="utf-8"))
                synced = set(ns.get("realizaciya_cards", {}).keys())
            except Exception:
                pass
        not_synced = [f for f in all_files if f not in synced]
        if not_synced:
            notion_line = f"\nNotion: {len(not_synced)} новых ({', '.join(not_synced[:3])}{'...' if len(not_synced) > 3 else ''})"
        else:
            notion_line = f"\nNotion: все {len(all_files)} исследований синхронизированы"

    await update.message.reply_text(
        f"Буфер: {count}\nClaude: {busy}\n"
        f"В плане: {plan_tasks}"
        f"{notion_line}"
    )


async def cmd_buffer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/buffer — показать буфер. /buffer <текст> — добавить текст в буфер."""
    if not is_admin(update):
        return
    # Если есть аргументы — добавить в буфер
    if context.args:
        text = " ".join(context.args).strip()
        if text:
            sender = _display_name(update.message.from_user) if update.message.from_user else "Админ"
            buffer_append(sender, "текст", text)
            count = buffer_count()
            await update.message.reply_text(
                f"Добавлено в буфер ({len(text)} симв). Всего: {count}\n"
                f"/push — обработать"
            )
            return
    content = buffer_read()
    if not content:
        await update.message.reply_text("Буфер пуст.")
    else:
        count = buffer_count()
        if len(content) > 3800:
            content = content[:3800] + "\n...(обрезано)"
        await update.message.reply_text(f"Буфер ({count}):\n\n{content}")


def _format_plan(plan: dict) -> str:
    """Форматирует план для вывода в Telegram."""
    parts = []
    if plan['do']:
        lines = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(plan['do']))
        parts.append(f"Задачи ({len(plan['do'])}):\n{lines}")
    if plan['research']:
        lines = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(plan['research']))
        parts.append(f"Исследования ({len(plan['research'])}):\n{lines}")
    if plan['wait']:
        items = ", ".join(plan['wait'])
        parts.append(f"Ждём: {items}")
    return "\n\n".join(parts)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/plan — задачи из плана."""
    if not is_admin(update):
        return
    plan = parse_plan()
    if not plan['do'] and not plan['research'] and not plan['wait']:
        await update.message.reply_text("План пуст.")
    else:
        text = _format_plan(plan)
        hints = []
        if plan['do']:
            hints.append("/do N — быстрая задача")
        if plan['research']:
            hints.append("/research N — исследование")
        if hints:
            text += "\n\n" + "\n".join(hints)
        await update.message.reply_text(text)


DO_TIMEOUT = 300  # 5 минут на быструю задачу


async def cmd_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/do тема — быстрая задача (письмо, текст, расчёт) без полного исследования."""
    global claude_busy
    if not is_admin(update):
        return
    message = update.message
    topic = " ".join(context.args) if context.args else ""
    if not topic:
        plan = parse_plan()
        n = len(plan['do'])
        lines = ["Что сделать?"]
        if n == 1:
            lines.append("/do 1 — задача из плана")
        elif n > 1:
            lines.append(f"/do 1–{n} — задача из плана")
        lines.append("/do составь письмо ...")
        await message.reply_text("\n".join(lines))
        return
    # Если цифра — взять задачу из плана (раздел do)
    from_plan = False
    if topic.strip().isdigit():
        plan = parse_plan()
        idx = int(topic.strip()) - 1
        if idx < 0 or idx >= len(plan['do']):
            await message.reply_text(f"Нет задачи #{topic}. Всего do: {len(plan['do'])}")
            return
        topic = plan['do'][idx]
        from_plan = True
    if claude_busy:
        await message.reply_text("Claude занят, подожди.")
        return
    claude_busy = True

    try:
        short = topic[:80] + ("..." if len(topic) > 80 else "")
        try:
            await _safe_reply(message.reply_text, f"Делаю: {short}")
        except Exception:
            pass  # статус — не критично

        # Контекст — минимальный
        state = ""
        state_path = PROJECT_DIR / "_sostoyaniye.md"
        if state_path.exists():
            state = state_path.read_text(encoding="utf-8")

        prompt = (
            f"## Контекст\n{state}\n\n"
            f"## Задача\n{topic}\n\n"
            "## Инструкции\n"
            "Выполни задачу быстро. Результат — текст для Telegram.\n"
            "НЕ создавай новые файлы в realizaciya/. НЕ трогай realizaciya/.\n"
            "НЕ проводи полное исследование. НЕ делай web search (если не просят явно).\n"
            "В _sostoyaniye.md: НЕ добавляй ссылки, телефоны, подробности. "
            "Только короткие действия с тегом [do] или [research]. НЕ используй другие теги.\n"
            "В КОНЦЕ выведи результат для Telegram (до 3500 символов, законченный текст).\n"
            "Формат: HTML (parse_mode). <b>жирный</b>, НЕ **markdown**. Без --- разделителей.\n"
            "Только полезный контент: текст письма, список вопросов, расчёт.\n"
            "НЕ пиши какие файлы обновил, что сделал внутри — только результат для человека.\n"
            "Действуй автономно."
        )
        loop = asyncio.get_event_loop()
        success, output = await loop.run_in_executor(
            None, run_claude, prompt, DO_TIMEOUT,
        )
    finally:
        claude_busy = False
        if ask_queue:
            asyncio.create_task(_process_ask_queue())
        _maybe_schedule_autopush()

    if success:
        if from_plan:
            remove_from_plan(topic)
        # Показать результат Claude
        summary = output.strip() if output else "(нет вывода)"
        text = f"Готово.\n\n{summary}"
        if len(text) > 3800:
            text = text[:3800] + "\n...(обрезано)"
        try:
            await _safe_reply(message.reply_text, text, parse_mode="HTML")
        except Exception:
            # Fallback без HTML если теги битые
            await _safe_reply(message.reply_text, text)
    else:
        summary = output[-500:] if output else "нет деталей"
        await _safe_reply(message.reply_text, f"Ошибка.\n{summary}")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clear — очистить буфер."""
    if not is_admin(update):
        return
    count = buffer_count()
    buffer_clear()
    await update.message.reply_text(f"Буфер очищен ({count} записей).")


async def cmd_catchup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/catchup — принудительно подхватить сообщения из группы."""
    if not is_admin(update):
        return
    if not bot_ref:
        await update.message.reply_text("Бот не инициализирован.")
        return
    before = buffer_count()
    await update.message.reply_text("Проверяю группу...")
    await _catchup_group_history(bot_ref, force=True)
    after = buffer_count()
    added = after - before
    if added > 0:
        await update.message.reply_text(f"Подхвачено {added} сообщений. Буфер: {after}.")
    else:
        await update.message.reply_text("Новых сообщений не найдено.")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sync — синхронизация с Notion."""
    if not is_admin(update):
        return
    await _do_sync(update.message.reply_text)


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/research N или /research тема — запустить исследование."""
    global claude_busy
    if not is_admin(update):
        return
    message = update.message
    topic = " ".join(context.args) if context.args else ""
    if not topic:
        plan = parse_plan()
        n = len(plan['research'])
        lines = ["Что исследовать?"]
        if n == 1:
            lines.append("/research 1 — исследование из плана")
        elif n > 1:
            lines.append(f"/research 1–{n} — исследование из плана")
        lines.append("/research тема — свободная тема")
        await message.reply_text("\n".join(lines))
        return
    from_plan = False
    if topic.isdigit():
        plan = parse_plan()
        idx = int(topic) - 1
        if idx < 0 or idx >= len(plan['research']):
            await message.reply_text(f"Нет исследования #{topic}. Всего: {len(plan['research'])}")
            return
        topic = plan['research'][idx]
        from_plan = True
    if claude_busy:
        await message.reply_text("Claude занят, подожди.")
        return
    claude_busy = True

    try:
        short = topic[:80] + ("..." if len(topic) > 80 else "")
        try:
            status_msg = await _safe_reply(message.reply_text, f"Исследование: {short}\n0:00")
        except Exception:
            status_msg = None
        # Снимок файлов ДО research — чтобы потом показать только новые
        existing_files = set()
        realizaciya_dir = PROJECT_DIR / "realizaciya"
        if realizaciya_dir.exists():
            existing_files = {f.name for f in realizaciya_dir.glob("*.md")}

        async def _research_heartbeat():
            start = asyncio.get_event_loop().time()
            while True:
                await asyncio.sleep(30)
                elapsed = int(asyncio.get_event_loop().time() - start)
                mins, secs = elapsed // 60, elapsed % 60
                # Новые файлы в realizaciya/
                new_files = []
                if realizaciya_dir.exists():
                    for f in realizaciya_dir.glob("*.md"):
                        if f.name not in existing_files and f.name != "index.md":
                            new_files.append(f.name)
                files_info = f" | +{len(new_files)} файлов" if new_files else ""
                try:
                    await status_msg.edit_text(f"Исследование: {short}\n{mins}:{secs:02d}{files_info}")
                except Exception:
                    pass

        heartbeat_task = asyncio.create_task(_research_heartbeat()) if status_msg else None
        prompt = build_research_prompt(topic)
        loop = asyncio.get_event_loop()
        success, output = await loop.run_in_executor(
            None, run_claude, prompt, RESEARCH_TIMEOUT,
        )
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
        claude_busy = False
        if ask_queue:
            asyncio.create_task(_process_ask_queue())
        _maybe_schedule_autopush()
    if success:
        if from_plan:
            remove_from_plan(topic)
        # Показать только НОВЫЕ файлы (не все грязные)
        new_research = []
        if realizaciya_dir.exists():
            for f in realizaciya_dir.glob("*.md"):
                if f.name not in existing_files and f.name != "index.md":
                    title = f.name
                    for line in f.read_text(encoding="utf-8").splitlines():
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break
                    new_research.append({"path": f"realizaciya/{f.name}", "title": title})
        if new_research:
            titles = "\n".join(f"  {r['title']}" for r in new_research)
            await message.reply_text(f"Исследование готово. Новое:\n{titles}")
        else:
            await message.reply_text("Исследование готово (файлы обновлены).")
        await _do_sync(message.reply_text)
        # Отправить в группу заказчика после sync
        if new_research:
            try:
                await _send_research_to_group(context.bot, new_research)
                # Кнопка «Доработать» для каждого нового исследования
                for r in new_research:
                    slug = Path(r["path"]).stem
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("Доработать", callback_data=f"edit_research:{slug}")],
                    ])
                    await message.reply_text(
                        f"Отправлено в группу: {r['title']}", reply_markup=kb,
                    )
            except Exception as e:
                await message.reply_text(f"Ошибка отправки в группу: {e}")
    else:
        summary = output[-500:] if output else "нет деталей"
        await message.reply_text(f"Ошибка исследования.\n{summary}")


# --- Telegram: обработчики ---

async def handle_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.effective_chat
    await update.message.reply_text(f"Chat ID: {c.id}\nType: {c.type}\nTitle: {c.title or '-'}")


def _save_pending_voice(file_id: str, sender: str, duration: int, timestamp: str):
    """Сохранить голосовое в очередь ДО транскрипции."""
    pending = []
    if PENDING_VOICES.exists():
        try:
            pending = json.loads(PENDING_VOICES.read_text(encoding="utf-8"))
        except Exception:
            pending = []
    pending.append({"file_id": file_id, "sender": sender, "duration": duration, "ts": timestamp})
    PENDING_VOICES.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")


def _remove_pending_voice(file_id: str):
    """Убрать голосовое из очереди ПОСЛЕ успешной транскрипции."""
    if not PENDING_VOICES.exists():
        return
    try:
        pending = json.loads(PENDING_VOICES.read_text(encoding="utf-8"))
        pending = [v for v in pending if v["file_id"] != file_id]
        if pending:
            PENDING_VOICES.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
        else:
            PENDING_VOICES.unlink(missing_ok=True)
    except Exception:
        pass


async def _process_voice(bot: Bot, file_id: str, sender: str, duration: int, ts: str,
                         reply_fn=None, tag: str = ""):
    """Единая обработка голосового: скачать → Буквица → md → буфер → уведомление."""
    dur_min, dur_sec = duration // 60, duration % 60
    dur_str = f"{dur_min}:{dur_sec:02d}"
    audio_path = OUTPUT_DIR / f"_temp_{ts}.oga"

    # Скачать если ещё нет
    if not audio_path.exists():
        file = await bot.get_file(file_id)
        await file.download_to_drive(str(audio_path))

    try:
        text = await transcribe_via_bukvitsa(str(audio_path))
    except Exception as e:
        if reply_fn:
            await reply_fn(f"Ошибка транскрипции: {e}")
        failed_dir = OUTPUT_DIR / "_failed"
        failed_dir.mkdir(exist_ok=True)
        try:
            shutil.move(str(audio_path), str(failed_dir / audio_path.name))
        except Exception:
            pass
        _remove_pending_voice(file_id)
        return

    md_name = f"{ts}_{sender}.md"
    md_path = OUTPUT_DIR / md_name
    md_path.write_text(
        f"---\nдата: {ts[:10]} {ts[11:13]}:{ts[14:16]}\n"
        f"от: {sender}\nдлительность: {dur_min} мин {dur_sec} сек\nразобрано: нет\n---\n\n{text}\n",
        encoding="utf-8",
    )
    audio_path.unlink(missing_ok=True)
    _remove_pending_voice(file_id)

    buffer_append(sender, dur_str, text)
    cnt = buffer_count_display()
    preview = text[:100] + ("..." if len(text) > 100 else "")
    tag_str = f" {tag}" if tag else ""

    if ADMIN_CHAT_ID and bot_ref:
        await bot_ref.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"\U0001f399 {sender} ({dur_str}): {preview} | Буфер: {cnt}{tag_str}",
        )
    print(f"  {md_name} | Буфер: {cnt}{tag_str}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    media = message.voice or message.audio or message.video_note
    # Пересланные аудио-документы (.m4a, .ogg) — Telegram отдаёт как document
    if not media and message.document and message.document.mime_type and message.document.mime_type.startswith("audio/"):
        media = message.document
    if not media:
        return

    sender = _display_name(message.from_user) if message.from_user else "?"
    is_private = message.chat.type == "private"
    if not is_private and not is_group_member(message.from_user):
        return
    duration = getattr(media, "duration", None) or 0
    msg_dt = message.date.astimezone() if message.date else datetime.now()
    timestamp = msg_dt.strftime("%Y-%m-%d_%H-%M-%S")

    # Сохранить в очередь ДО транскрипции — если бот крашнется, подхватим при рестарте
    _save_pending_voice(media.file_id, sender, duration, timestamp)

    reply_fn = message.reply_text if is_private else None
    await _process_voice(context.bot, media.file_id, sender, duration, timestamp, reply_fn)


async def _handle_edit_comment(message, comment: str):
    """Обработка комментария к вопросам / задачам / исследованию / уточнению ask."""
    global claude_busy, pending_edit, _last_ask, _last_kb_msg
    edit_ctx = pending_edit
    pending_edit = None

    if not edit_ctx:
        return

    edit_type = edit_ctx.get("type")

    if claude_busy:
        pending_edit = edit_ctx  # вернуть — пусть повторит когда Claude освободится
        await message.reply_text("Claude занят, подожди. Повтори комментарий позже.")
        return

    if edit_type == "tasks":
        claude_busy = True
        try:
            await message.reply_text("Передаю Claude...")
            prompt = (
                "В файле bot/pending_tasks.txt лежат задачи.\n"
                f"Комментарий: {comment}\n"
                "Исправь задачи по комментарию. "
                "Сохрани формат: одна задача — один блок, разделитель ---. "
                "Первая строка блока — [do] или [research] + краткое название, вторая — что сделать."
            )
            loop = asyncio.get_event_loop()
            success, _ = await loop.run_in_executor(
                None, lambda: run_claude(prompt, 180, model="haiku"))
        finally:
            claude_busy = False
            if ask_queue:
                asyncio.create_task(_process_ask_queue())
            _maybe_schedule_autopush()
        if success and PENDING_TASKS.exists():
            pending = read_pending_tasks()
            task_lines = "\n".join(f"  {i+1}. {t.splitlines()[0]}" for i, t in enumerate(pending))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("В план", callback_data="approve_tasks"),
                 InlineKeyboardButton("Пропустить", callback_data="skip_tasks"),
                 InlineKeyboardButton("Исправить", callback_data="edit_tasks")],
            ])
            await message.reply_text(f"Обновлены ({len(pending)}):\n{task_lines}", reply_markup=kb)
        else:
            await message.reply_text("Не удалось переделать.")

    elif edit_type == "research":
        slug = edit_ctx.get("slug", "")
        filepath = f"realizaciya/{slug}.md"
        full_path = PROJECT_DIR / filepath
        if not full_path.exists():
            await message.reply_text(f"Файл {filepath} не найден.")
            return
        claude_busy = True
        try:
            await message.reply_text(f"Дорабатываю исследование (до 30 мин)...")
            prompt = (
                f"Файл {filepath} содержит исследование.\n"
                f"Комментарий: {comment}\n"
                "Доработай исследование по комментарию. "
                "Следуй workflow реализации (skill workflow-realizaciya). "
                "ОБЯЗАТЕЛЬНО используй web search для актуальных данных. "
                "Обнови файл, сохрани структуру (Главное → Что делать → ...). "
                "Обнови realizaciya/index.md и _sostoyaniye.md если нужно. "
                f"Обнови файл bot/msg-{slug}.txt с сообщениями для Telegram-группы заказчика "
                "(формат — по правилам из skill format-research-tg). "
                "Действуй автономно, без вопросов."
            )
            loop = asyncio.get_event_loop()
            success, output = await loop.run_in_executor(
                None, run_claude, prompt, RESEARCH_TIMEOUT,
            )
        finally:
            claude_busy = False
            if ask_queue:
                asyncio.create_task(_process_ask_queue())
            _maybe_schedule_autopush()
        if success:
            await message.reply_text("Доработано.")
            await _do_sync(message.reply_text)
            # Отправить обновлённое исследование в группу заказчика
            if bot_ref:
                # Извлечь настоящий заголовок из файла
                real_title = slug
                if full_path.exists():
                    for fl in full_path.read_text(encoding="utf-8").splitlines():
                        if fl.startswith("# "):
                            real_title = fl[2:].strip()
                            break
                research_item = [{"path": filepath, "title": real_title}]
                try:
                    await _send_research_to_group(bot_ref, research_item)
                except Exception as e:
                    await message.reply_text(f"Ошибка отправки в группу: {e}")
            else:
                await message.reply_text("Бот не инициализирован, отправка в группу пропущена.")
        else:
            summary = output[-500:] if output else "нет деталей"
            await message.reply_text(f"Ошибка доработки.\n{summary}")

    elif edit_type == "ask":
        original_q = edit_ctx.get("question", "")
        original_a = edit_ctx.get("answer", "")
        claude_busy = True
        try:
            await _safe_reply(message.reply_text, "Уточняю...")

            state = ""
            state_path = PROJECT_DIR / "_sostoyaniye.md"
            if state_path.exists():
                state = state_path.read_text(encoding="utf-8")

            prompt = (
                f"## Контекст проекта\n{state}\n\n"
                f"## Исходный вопрос\n{original_q}\n\n"
                f"## Твой предыдущий ответ\n{original_a}\n\n"
                f"## Уточнение пользователя\n{comment}\n\n"
                "## Инструкции\n"
                "Пользователь уточняет или просит действие на основе предыдущего диалога.\n"
                "Можешь читать файлы проекта. Можешь изменять файлы если пользователь просит.\n"
                "НЕ делай web search (если не просят явно).\n"
                "Ответ — для Telegram, до 3500 символов.\n"
                "Формат: HTML (parse_mode). <b>жирный</b>, НЕ **markdown**. Без --- разделителей.\n"
                "Только полезный контент. НЕ пиши какие файлы читал — только ответ.\n"
            )

            loop = asyncio.get_event_loop()
            success, output = await loop.run_in_executor(
                None, run_claude, prompt, DO_TIMEOUT,
            )
        except Exception as e:
            await _safe_reply(message.reply_text, f"Ошибка: {e}")
            return
        finally:
            claude_busy = False
            if ask_queue:
                asyncio.create_task(_process_ask_queue())
            _maybe_schedule_autopush()

        if success:
            answer = output.strip() if output else "(нет ответа)"
            if len(answer) > 3800:
                answer = answer[:3800] + "\n...(обрезано)"
            _last_ask = {"question": original_q, "answer": answer}
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("В группу", callback_data="ask_send"),
                InlineKeyboardButton("Уточнить", callback_data="ask_continue"),
                InlineKeyboardButton("Готово", callback_data="ask_done"),
            ]])
            await _remove_prev_kb()
            try:
                _last_kb_msg = await _safe_reply(message.reply_text, answer, parse_mode="HTML", reply_markup=kb)
            except Exception:
                _last_kb_msg = await _safe_reply(message.reply_text, answer, reply_markup=kb)
        else:
            summary = output[-500:] if output else "нет деталей"
            await _safe_reply(message.reply_text, f"Ошибка.\n{summary}")


MIN_GROUP_TEXT = 20        # символов — короче игнорируем ("ок", "да", ссылки)
MIN_FORWARDED_TEXT = 10    # для пересланных — порог ниже, раз человек специально переслал


async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текстовые сообщения и подписи к фото/документам из группы → буфер."""
    message = update.message
    if not message:
        return
    # Игнорировать сообщения от ботов (включая самого себя)
    if message.from_user and message.from_user.is_bot:
        return
    # Debug: логируем ВСЕ входящие сообщения из группы
    sender_dbg = message.from_user.first_name if message.from_user else "?"
    text_dbg = (message.text or message.caption or "")[:60]
    print(f"  [группа-вход] {sender_dbg}: {text_dbg!r} (chat={message.chat_id})", flush=True)
    if not is_group_member(message.from_user):
        print(f"  [группа-вход] ОТКЛОНЕНО: не участник ({message.from_user.id})", flush=True)
        return
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return
    # Пересланные сообщения — порог ниже (человек специально переслал)
    is_forwarded = message.forward_origin is not None
    min_len = MIN_FORWARDED_TEXT if is_forwarded else MIN_GROUP_TEXT
    if text.startswith("http"):
        return
    if len(text) < min_len:
        print(f"  [группа-вход] ОТКЛОНЕНО: короткое ({len(text)}<{min_len})", flush=True)
        return

    sender = _display_name(message.from_user) if message.from_user else "?"
    # Дедупликация: catchup мог уже добавить это сообщение через Telethon
    if message.message_id in buffer_msg_ids():
        print(f"  [группа-вход] ПРОПУСК: msg:{message.message_id} уже в буфере (catchup)")
        return
    buffer_append(sender, "текст", text, message_id=message.message_id)
    cnt = buffer_count_display()
    preview = text[:100] + ("..." if len(text) > 100 else "")
    if ADMIN_CHAT_ID and bot_ref:
        await bot_ref.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"\U0001f4dd {sender} ({len(text)} симв): {preview} | Буфер: {cnt}",
        )
    print(f"  [группа] {sender} ({len(text)} симв) | Буфер: {cnt}")


async def handle_edited_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отредактированное сообщение из группы → обновить запись в буфере или добавить новую."""
    message = update.edited_message
    if not message:
        return
    if message.from_user and message.from_user.is_bot:
        return
    if not is_group_member(message.from_user):
        return
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/") or text.startswith("http"):
        return
    min_len = MIN_FORWARDED_TEXT if message.forward_origin else MIN_GROUP_TEXT
    if len(text) < min_len:
        return

    sender = _display_name(message.from_user) if message.from_user else "?"
    msg_id = message.message_id

    if buffer_update(msg_id, text):
        # Обновили существующую запись
        print(f"  [группа-edit] {sender} msg:{msg_id} обновлено в буфере")
        if ADMIN_CHAT_ID and bot_ref:
            preview = text[:80] + ("..." if len(text) > 80 else "")
            await bot_ref.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"\u270f {sender}: сообщение обновлено | {preview}",
            )
    else:
        # Записи нет (уже обработана или не попадала) — добавить как новую
        buffer_append(sender, "текст", text, message_id=msg_id)
        cnt = buffer_count_display()
        print(f"  [группа-edit] {sender} msg:{msg_id} добавлено (не было в буфере) | Буфер: {cnt}")
        if ADMIN_CHAT_ID and bot_ref:
            preview = text[:80] + ("..." if len(text) > 80 else "")
            await bot_ref.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"\u270f {sender} (изм.): {preview} | Буфер: {cnt}",
            )


async def _process_ask_queue():
    """Обработать следующий вопрос из очереди если Claude свободен."""
    if not ask_queue or claude_busy:
        return
    message, question = ask_queue.pop(0)
    await _handle_ask(message, question)


async def _handle_ask(message, question: str):
    """Свободный вопрос админа -> Claude read-only."""
    global claude_busy, _last_ask, _last_kb_msg

    # Атомарно: проверка + установка (без await между ними — нет race condition)
    if claude_busy:
        ask_queue.append((message, question))
        pos = len(ask_queue)
        await message.reply_text(f"Claude занят. Вопрос в очереди ({pos}).")
        return
    claude_busy = True

    try:
        try:
            await _safe_reply(message.reply_text, "Ищу ответ...")
        except Exception:
            pass  # статус — не критично, Claude запустится

        state = ""
        state_path = PROJECT_DIR / "_sostoyaniye.md"
        if state_path.exists():
            state = state_path.read_text(encoding="utf-8")

        prompt = (
            f"## Контекст проекта\n{state}\n\n"
            f"## Вопрос\n{question}\n\n"
            "## Инструкции\n"
            "Ответь на вопрос пользователя. Можешь читать файлы проекта для поиска информации.\n"
            "НЕ создавай и НЕ изменяй файлы. НЕ обновляй _sostoyaniye.md, index.yaml, katalog.yaml.\n"
            "НЕ делай web search (если не просят явно).\n"
            "Ответ — для Telegram, до 3500 символов.\n"
            "Формат: HTML (parse_mode). <b>жирный</b>, НЕ **markdown**. Без --- разделителей.\n"
            "Только полезный контент. НЕ пиши какие файлы читал — только ответ.\n"
        )

        loop = asyncio.get_event_loop()
        success, output = await loop.run_in_executor(
            None, run_claude, prompt, ASK_TIMEOUT,
        )
    except Exception as e:
        await _safe_reply(message.reply_text, f"Ошибка: {e}")
        return
    finally:
        claude_busy = False
        if ask_queue:
            asyncio.create_task(_process_ask_queue())
        _maybe_schedule_autopush()

    if success:
        answer = output.strip() if output else "(нет ответа)"
        if len(answer) > 3800:
            answer = answer[:3800] + "\n...(обрезано)"
        _last_ask = {"question": question, "answer": answer}
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("В группу", callback_data="ask_send"),
            InlineKeyboardButton("Уточнить", callback_data="ask_continue"),
            InlineKeyboardButton("Готово", callback_data="ask_done"),
        ]])
        await _remove_prev_kb()
        try:
            _last_kb_msg = await _safe_reply(message.reply_text, answer, parse_mode="HTML", reply_markup=kb)
        except Exception:
            _last_kb_msg = await _safe_reply(message.reply_text, answer, reply_markup=kb)
    else:
        summary = output[-500:] if output else "нет деталей"
        await _safe_reply(message.reply_text, f"Ошибка.\n{summary}")


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текст от админа: алиасы команд, комментарий к вопросам, вопрос Claude, транскрипция."""
    global claude_busy, pending_edit
    message = update.message
    if not message or not message.text:
        return
    if not is_admin(update):
        return

    text = message.text.strip()
    cmd = text.lower()

    # Текстовые алиасы (обратная совместимость)
    if cmd in ("push", "пуш"):
        return await cmd_push(update, context)
    if cmd in ("статус", "status", "?"):
        return await cmd_status(update, context)
    if cmd in ("покажи", "буфер", "buffer"):
        return await cmd_buffer(update, context)
    if cmd in ("план", "plan", "задачи", "tasks"):
        return await cmd_plan(update, context)
    if cmd in ("очисти", "clear"):
        return await cmd_clear(update, context)
    if cmd in ("sync", "синх"):
        return await cmd_sync(update, context)
    if cmd in ("catchup", "подхвати"):
        return await cmd_catchup(update, context)
    if cmd.startswith("исследуй") or cmd.startswith("research") or cmd.startswith("ресерч"):
        parts = text.split(maxsplit=1)
        context.args = [parts[1].strip()] if len(parts) > 1 else []
        return await cmd_research(update, context)
    if cmd.startswith("сделай") or cmd == "do" or cmd.startswith("do ") or cmd == "ду" or cmd.startswith("ду "):
        parts = text.split(maxsplit=1)
        context.args = [parts[1].strip()] if len(parts) > 1 else []
        return await cmd_do(update, context)
    if cmd in ("задачи ок", "tasks ok"):
        pending = read_pending_tasks()
        if not pending:
            await message.reply_text("Нет задач на утверждение.")
            return
        add_tasks_to_sostoyaniye(pending)
        PENDING_TASKS.unlink()
        names = "\n".join(f"  {i+1}. {t.splitlines()[0]}" for i, t in enumerate(pending))
        await message.reply_text(f"Добавлено в план ({len(pending)}):\n{names}")
        return
    # Явное добавление в буфер: "буфер <текст>"
    if cmd.startswith("буфер ") or cmd.startswith("buffer "):
        parts = text.split(maxsplit=1)
        content = parts[1].strip() if len(parts) > 1 else ""
        if content:
            sender = _display_name(message.from_user) if message.from_user else "Админ"
            buffer_append(sender, "текст", content)
            count = buffer_count()
            await message.reply_text(
                f"Добавлено в буфер ({len(content)} симв). Всего: {count}\n"
                f"/push — обработать"
            )
        else:
            return await cmd_buffer(update, context)
        return

    # Комментарий по кнопке "Исправить" / "Доработать" / "Уточнить"
    if pending_edit:
        # Таймаут: если "Уточнить" нажали давно (>10 мин) — сбросить, обработать как новый вопрос
        if pending_edit.get("type") == "ask" and time.time() - pending_edit.get("ts", 0) > 600:
            pending_edit = None
        else:
            await _handle_edit_comment(message, text)
            return

    # Свободный вопрос → Claude
    if len(text) >= 10:
        await _handle_ask(message, text)
        return

    # Слишком короткий текст
    await message.reply_text(
        "Не понимаю. Команды — в меню (кнопка /).\n"
        "Или задай вопрос (от 10 символов)."
    )


# --- Жизненный цикл ---

BOT_COMMANDS = [
    BotCommand("push", "Обработать буфер"),
    BotCommand("status", "Статус (буфер, Claude, задачи)"),
    BotCommand("buffer", "Показать буфер"),
    BotCommand("plan", "Задачи из плана"),
    BotCommand("research", "Исследование (до 30 мин)"),
    BotCommand("do", "Быстрая задача (до 5 мин)"),
    BotCommand("sync", "Синхронизация с Notion"),
    BotCommand("catchup", "Подхватить сообщения из группы"),
    BotCommand("clear", "Очистить буфер"),
]


async def _catchup_pending_voices(bot: Bot):
    """При старте: дотранскрибировать голосовые из pending_voices.json (крэш до завершения)."""
    if not PENDING_VOICES.exists():
        print("  Catchup pending: очередь пуста")
    else:
        try:
            pending = json.loads(PENDING_VOICES.read_text(encoding="utf-8"))
        except Exception:
            pending = []
        if pending:
            print(f"  Catchup pending: {len(pending)} голосовых в очереди")
            for v in pending:
                print(f"  Catchup: {v['sender']} ({v['ts']}), транскрибирую...")
                await _process_voice(bot, v["file_id"], v["sender"], v["duration"], v["ts"], tag="[подхвачено]")

    # Проверить пропущенные голосовые в группе через Telethon
    await _catchup_group_history(bot)


async def _catchup_group_history(bot: Bot, force: bool = False):
    """Проверить последние сообщения в группе, подхватить пропущенные голосовые и текст.
    force=True — игнорировать фильтр по времени (подхватить всё из последних 50 сообщений)."""
    try:
        existing_stems = {f.stem for f in OUTPUT_DIR.glob("*.md")}

        if force:
            last_push_time = datetime.min.replace(tzinfo=timezone.utc)
        else:
            # Время последнего /push — по самому свежему архиву буфера
            archives = sorted(BUFFER_FILE.parent.glob("buffer_*.md"), reverse=True)
            if archives:
                last_push_time = datetime.fromtimestamp(
                    archives[0].stat().st_mtime, tz=timezone.utc
                )
            else:
                last_push_time = datetime.min.replace(tzinfo=timezone.utc)

        # Получить entity через dialogs (надёжнее чем get_entity для групп)
        dialogs = await telethon_client.get_dialogs()
        target = None
        for d in dialogs:
            if d.id == GROUP_CHAT_ID:
                target = d
                break
        if not target:
            print(f"  Catchup group: группа {GROUP_CHAT_ID} не найдена в диалогах")
            return

        print(f"  Catchup group: {target.name}, unread={target.unread_count}", flush=True)
        messages = await telethon_client.get_messages(target.entity, limit=50)
        print(f"  Catchup group: {len(messages)} сообщений", flush=True)
        # Debug в файл — с именами и ID отправителей
        debug_lines = [f"name={target.name} unread={target.unread_count} msgs={len(messages)}"]
        for m in messages:
            sender_fn = getattr(m.sender, "first_name", "?") if m.sender else "?"
            sender_id = m.sender_id or "?"
            fwd = ""
            if m.forward and m.forward.sender_id:
                fwd = f" [fwd from {m.forward.sender_id}]"
            debug_lines.append(
                f"  {m.date} | id={sender_id} name={sender_fn}{fwd} | {(m.text or '-')[:50]}"
            )
        Path(__file__).parent.joinpath("catchup_debug.txt").write_text(
            "\n".join(debug_lines), encoding="utf-8",
        )

        catchup_known_ids = buffer_msg_ids()
        for msg in reversed(messages):  # от старых к новым
            if not is_group_member_telethon(msg.sender):
                continue
            sender_name = _display_name(msg.sender) if msg.sender else "?"

            local_dt = msg.date.astimezone()
            ts = local_dt.strftime("%Y-%m-%d_%H-%M-%S")

            # Голосовое (включая пересланные аудио-документы)
            is_audio_doc = (msg.document and hasattr(msg.document, 'mime_type')
                           and msg.document.mime_type and msg.document.mime_type.startswith("audio/"))
            if msg.voice or msg.audio or msg.video_note or is_audio_doc:
                # Дедупликация по timestamp (без имени — оно может отличаться между Bot API и Telethon)
                if any(s.startswith(ts) for s in existing_stems):
                    continue
                media = msg.voice or msg.audio or msg.video_note or msg.document
                duration = getattr(media, "duration", 0) or 0
                # Telethon: duration хранится в DocumentAttributeAudio
                if not duration:
                    for attr in getattr(media, "attributes", []):
                        duration = getattr(attr, "duration", 0) or 0
                        if duration:
                            break
                print(f"  Catchup group: голосовое {sender_name} ({ts})")
                # Скачать через Telethon, транскрибировать
                audio_path = OUTPUT_DIR / f"_temp_{ts}.oga"
                await telethon_client.download_media(msg, file=str(audio_path))
                catchup_id = f"catchup_{ts}"
                _save_pending_voice(catchup_id, sender_name, duration, ts)
                await _process_voice(bot, catchup_id, sender_name, duration, ts, tag="[подхвачено]")
                continue

            # Текст
            text = (msg.text or "").strip()
            if not text or text.startswith("/"):
                continue
            is_fwd = msg.forward is not None
            min_len = MIN_FORWARDED_TEXT if is_fwd else MIN_GROUP_TEXT
            if text.startswith("http"):
                continue
            if len(text) < min_len:
                continue
            # Пропускать сообщения старше последнего /push (уже обработаны)
            if msg.date < last_push_time:
                continue
            # Дедупликация по msg_id; обновление текста если сообщение было отредактировано
            if msg.id in catchup_known_ids:
                buffer_update(msg.id, text)  # обновит если текст изменился, иначе noop
                continue
            buffer_append(sender_name, "текст", text, message_id=msg.id)
            catchup_known_ids.add(msg.id)
            cnt = buffer_count_display()
            preview = text[:80] + ("..." if len(text) > 80 else "")
            print(f"  Catchup group: текст {sender_name} ({ts}): {preview[:40]}")
            if ADMIN_CHAT_ID:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"\U0001f4dd {sender_name}: {preview} | Буфер: {cnt} [подхвачено]",
                )
    except Exception as e:
        print(f"  Catchup group ошибка: {e}")


async def post_init(app):
    global bot_ref, BOT_USER_ID
    await telethon_client.start()
    me = await telethon_client.get_me()
    print(f"  Userbot: {me.first_name} ({me.phone})")
    bot_ref = app.bot
    await app.bot.set_my_commands(BOT_COMMANDS)
    print(f"  Меню команд установлено ({len(BOT_COMMANDS)} шт)")
    # Автообнаружение участников группы
    try:
        entity = await telethon_client.get_entity(GROUP_CHAT_ID)
        # Синхронизировать историю
        test_msg = await telethon_client.send_message(entity, "\U0001f504")
        await telethon_client.delete_messages(entity, [test_msg.id])
        print("  Telethon: группа синхронизирована")
        # Получить участников и записать
        participants = await telethon_client.get_participants(entity)
        lines = []
        for p in participants:
            uname = f"@{p.username}" if p.username else "-"
            lines.append(f"id={p.id}  name={p.first_name or '?'}  username={uname}")
            # Автозаполнение usernames для фильтрации (без ботов)
            if p.username and not p.bot:
                GROUP_ALLOWED_USERNAMES.add(p.username.lower())
            # Каноническое имя: берём из Bot API (актуальнее Telethon-кеша)
            if not p.bot:
                try:
                    member = await app.bot.get_chat_member(GROUP_CHAT_ID, p.id)
                    _user_display_names[p.id] = _resolve_name(
                        member.user.first_name or "", member.user.last_name or "")
                except Exception:
                    _user_display_names[p.id] = _resolve_name(
                        p.first_name or "", getattr(p, "last_name", "") or "")
        GROUP_MEMBERS_FILE.write_text("\n".join(lines), encoding="utf-8")
        print(f"  Участники группы ({len(participants)}):")
        for line in lines:
            print(f"    {line}")
        # Показать канонические имена
        for uid, name in _user_display_names.items():
            print(f"    → {uid} = {name}")
        if not GROUP_ALLOWED_IDS:
            # ID не заданы в .env — заполняем из участников (все участники группы = разрешены)
            for p in participants:
                if not p.bot:
                    GROUP_ALLOWED_IDS.add(p.id)
            print(f"  GROUP_ALLOWED_IDS (авто): {GROUP_ALLOWED_IDS}")
    except Exception as e:
        print(f"  Telethon: не удалось получить участников группы: {e}")
    # Диагностика: проверить статус бота в группе через Bot API
    try:
        bot_me = await app.bot.get_me()
        BOT_USER_ID = bot_me.id
        print(f"  Bot API: я = {bot_me.first_name} (id={bot_me.id})")
        chat = await app.bot.get_chat(GROUP_CHAT_ID)
        print(f"  Bot API: группа = {chat.title} (id={chat.id}, type={chat.type})")
        member = await app.bot.get_chat_member(GROUP_CHAT_ID, bot_me.id)
        print(f"  Bot API: статус бота = {member.status}")
        if hasattr(member, 'can_read_messages'):
            print(f"  Bot API: can_read_messages = {member.can_read_messages}")
    except Exception as e:
        print(f"  Bot API диагностика: ОШИБКА — {e}")
    # Дотранскрибировать голосовые из очереди (крэш во время транскрипции)
    await _catchup_pending_voices(app.bot)

    # Периодический catchup — подхватить пропущенное после сетевых сбоев
    CATCHUP_INTERVAL = 3600  # секунд (1 час)

    async def _periodic_catchup():
        while True:
            await asyncio.sleep(CATCHUP_INTERVAL)
            try:
                print("  [catchup-таймер] Проверяю пропущенные...", flush=True)
                await _catchup_group_history(app.bot)
            except Exception as e:
                print(f"  [catchup-таймер] Ошибка: {e}", flush=True)

    asyncio.create_task(_periodic_catchup())
    print(f"  Catchup-таймер: каждые {CATCHUP_INTERVAL // 60} мин")

    # Проверить буфер при старте — мог накопиться до перезапуска
    _maybe_schedule_autopush()


async def post_shutdown(app):
    await telethon_client.disconnect()


def ensure_single_instance():
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # проверяем, жив ли (не убиваем)
            print(f"  Бот уже запущен (PID {old_pid}), выхожу")
            sys.exit(0)
        except (ProcessLookupError, OSError, ValueError):
            pass  # процесс мёртв — stale PID, продолжаем
    PID_FILE.write_text(str(os.getpid()))


def main():
    ensure_single_instance()
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Всегда писать в bot.log (+ в консоль если есть)
    log_path = Path(__file__).parent / "bot.log"
    log_file = open(log_path, "a", encoding="utf-8")
    original_out = None
    try:
        if sys.stdout and hasattr(sys.stdout, 'fileno'):
            sys.stdout.fileno()
            original_out = sys.stdout
    except Exception:
        pass

    class _Tee:
        def __init__(self, *streams):
            self.streams = [s for s in streams if s]
        def write(self, data):
            for s in self.streams:
                try:
                    s.write(data)
                    s.flush()
                except Exception:
                    pass
        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    sys.stdout = sys.stderr = _Tee(original_out, log_file)

    print(f"Бот | PID {os.getpid()} | Админ {ADMIN_CHAT_ID} | Группа {GROUP_CHAT_ID}")

    from telegram.request import HTTPXRequest
    request = HTTPXRequest(connect_timeout=20.0, read_timeout=30.0)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .build()
    )

    # Slash-команды (меню Telegram)
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("buffer", cmd_buffer))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("catchup", cmd_catchup))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("do", cmd_do))
    app.add_handler(CommandHandler("chatid", handle_chatid))

    # Голосовые: из группы + личные от админа
    app.add_handler(MessageHandler(
        (filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE)
        & (filters.ChatType.PRIVATE | filters.Chat(chat_id=GROUP_CHAT_ID)),
        handle_voice,
    ))

    # Пересланные аудио-документы из группы (.m4a, .ogg и т.д.)
    app.add_handler(MessageHandler(
        filters.Document.AUDIO & filters.Chat(chat_id=GROUP_CHAT_ID),
        handle_voice,
    ))

    # Отредактированные сообщения из группы → обновить запись в буфере
    # (регистрируем ДО обычного handle_group_text — более узкий фильтр первым)
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & filters.Chat(chat_id=GROUP_CHAT_ID)
        & filters.UpdateType.EDITED_MESSAGE,
        handle_edited_group_text,
    ))

    # Текст и подписи к медиа из группы → буфер (только новые сообщения)
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & filters.Chat(chat_id=GROUP_CHAT_ID)
        & filters.UpdateType.MESSAGE,
        handle_group_text,
    ))

    # Текст от админа (алиасы + вопрос Claude + комментарии к уточнениям)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE, handle_admin_text,
    ))

    app.add_handler(CallbackQueryHandler(handle_callback))

    # ДИАГНОСТИКА: catch-all в group=1 — ловит ВСЁ, не мешает основным хендлерам
    async def _debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if not msg:
            return
        chat_type = msg.chat.type if msg.chat else "?"
        chat_id = msg.chat.id if msg.chat else "?"
        sender = msg.from_user.first_name if msg.from_user else "?"
        text = (msg.text or msg.caption or "")[:40]
        print(f"  [DEBUG-ALL] {chat_type} chat={chat_id} from={sender}: {text!r}", flush=True)

    app.add_handler(MessageHandler(filters.ALL, _debug_all), group=1)

    async def _error_handler(update, context):
        """Логировать ошибки вместо молчаливого проглатывания."""
        err = context.error
        if isinstance(err, (NetworkError, TimedOut)):
            print(f"  [!] Сеть: {type(err).__name__}: {err}", flush=True)
        else:
            print(f"  [!] Ошибка: {type(err).__name__}: {err}", flush=True)

    app.add_error_handler(_error_handler)
    app.run_polling(allowed_updates=["message", "edited_message", "callback_query"])


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        crash_file = Path(__file__).parent / "_tray_crash.log"
        with open(crash_file, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now()} PID={os.getpid()} ---\n")
            traceback.print_exc(file=f)
        sys.exit(1)
