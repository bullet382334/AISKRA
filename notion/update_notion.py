#!/usr/bin/env python3
"""
update_notion.py — синхронизация файлов -> Notion

Читает .md-карточки из karta-idej/idei/ и project/,
парсит markdown -> Notion-блоки, обновляет карточки мечт.

Запуск: python notion/update_notion.py
Автозапуск: через Claude Code hook после изменения файлов.
"""

import os
import re
import json
import hashlib
import time
import signal
import threading
import yaml
import requests
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv, dotenv_values

# ─── Глобальный таймаут (watchdog) ───────────────────────────

GLOBAL_TIMEOUT_SEC = 1080  # 18 мин (бот даёт 15 мин + 3 мин буфер на cleanup)

def _watchdog():
    """Убивает процесс если скрипт завис дольше GLOBAL_TIMEOUT_SEC."""
    time.sleep(GLOBAL_TIMEOUT_SEC)
    safe_print(f"\n[TIMEOUT] Скрипт работает > {GLOBAL_TIMEOUT_SEC // 60} мин — принудительное завершение")
    _write_progress(error="timeout")
    # Сохраняем state перед смертью (чтобы хеши не потерялись)
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if state:
                save_state(state)
    except Exception:
        pass
    _release_lock()
    os._exit(1)

_watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
_watchdog_thread.start()

# ─── Прогресс-файл ──────────────────────────────────────────

PROGRESS_FILE = Path(__file__).parent / ".sync_progress.json"

def _write_progress(current=0, total=0, card="", error=None):
    """Пишет прогресс в JSON для внешних читателей (трей и тд)."""
    data = {
        "ts": datetime.now().isoformat(),
        "current": current,
        "total": total,
        "card": card,
        "done": current >= total and total > 0 and error is None,
        "error": error,
    }
    try:
        PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

# ─── Конфигурация ─────────────────────────────────────────────

NOTION_DIR = Path(__file__).parent
PROJECT_DIR = NOTION_DIR.parent
load_dotenv(NOTION_DIR / ".env")

NOTION_TOKEN = os.environ["NOTION_API_TOKEN"]
ROOT_PAGE_ID = os.environ["NOTION_ROOT_PAGE"]
NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": NOTION_VERSION,
}

STATE_FILE = NOTION_DIR / ".notion_state.json"
LOCK_FILE = NOTION_DIR / ".sync.lock"

# ─── Безопасный вывод (cp1251 не поддерживает эмодзи) ────────

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FFFF\U00002702-\U000027B0\U0000FE0F\u200D]"
)


def safe_print(*args, **kwargs):
    """print() без проблемных символов — для Windows cp1251 консоли."""
    parts = []
    for a in args:
        s = _EMOJI_RE.sub("", str(a))
        # Заменяем все символы, которые cp1251 не может закодировать
        s = s.encode("cp1251", errors="replace").decode("cp1251")
        parts.append(s)
    kwargs.setdefault("flush", True)
    try:
        print(*parts, **kwargs)
    except Exception:
        pass


# ─── Telegram-уведомления ────────────────────────────────────

PERSON_TG_USERNAME = os.environ.get("PERSON_TG_USERNAME", "")


def load_telegram_config():
    """Загружает BOT_TOKEN и GROUP_CHAT_ID из bot/.env."""
    bot_env_path = PROJECT_DIR / "bot" / ".env"
    if not bot_env_path.exists():
        return None, None
    values = dotenv_values(bot_env_path)
    token = values.get("BOT_TOKEN")
    group_id = values.get("GROUP_CHAT_ID")
    if token and group_id:
        return token, group_id
    return None, None


def notion_page_url(page_id):
    """Формирует URL страницы Notion из page_id."""
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def extract_topic(title):
    """Извлекает тему из заголовка: убирает эмодзи и '— исследование'."""
    topic = re.sub(r'[\U0001F300-\U0001FFFF\U00002702-\U000027B0\U0000FE0F]', '', title)
    topic = re.sub(r'\s*[—–-]\s*исследование\s*$', '', topic, flags=re.IGNORECASE)
    return topic.strip()


def extract_v_processe(source_file):
    """Извлекает чек-лист из md-файла для отправки в Telegram.
    Ищет секции: '## В процессе', '## Что делать', '## Что делать дальше'."""
    path = PROJECT_DIR / source_file
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    section_markers = ["## В процессе", "## Что делать"]
    lines = text.split("\n")
    in_section = False
    items = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(m) for m in section_markers):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("## ") or stripped == "---":
                if items:
                    break  # нашли чек-лист, хватит
                in_section = False  # пустая секция, ищем дальше
                continue
            if stripped.startswith("- [x]") or stripped.startswith("- [ ]"):
                done = stripped.startswith("- [x]")
                text_part = re.sub(r'^- \[.\]\s*', '', stripped)
                text_part = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text_part)
                mark = "\u2705" if done else "\u2B55"
                items.append(f"{mark} {text_part}")
    return items if items else None


# Коллектор обновлённых карточек за текущий sync (для единого уведомления)
_synced_cards = []  # list of (topic, is_new)

# Файл с описаниями изменений от бота/Claude (пишется ДО sync)
_SYNC_CHANGES_FILE = NOTION_DIR / ".sync_changes.json"


def _load_sync_changes():
    """Загружает описания изменений, записанные ботом перед sync."""
    if _SYNC_CHANGES_FILE.exists():
        try:
            return json.loads(_SYNC_CHANGES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _clear_sync_changes():
    """Удаляет файл описаний после использования."""
    try:
        _SYNC_CHANGES_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def track_synced_card(title, is_new=False, **_kwargs):
    """Запоминает обновлённую карточку для сводки после sync."""
    topic = extract_topic(title)
    _synced_cards.append((topic, is_new))


def notify_new_research(title, page_id=None, state=None, notify_key=None, source_file=None):
    """Обратная совместимость: вызывается при создании карточки. Добавляет как 'новое'."""
    track_synced_card(title, is_new=True, source_file=source_file)


def send_sync_summary(state):
    """Отправляет компактную сводку sync в личку админу.

    Формат:
      Notion sync OK.
      [Общий смысл изменений — из .sync_changes.json, если есть]
      Новое (N): название1, название2
      Обновлено (M): название3, название4
      [ссылка]
    """
    if not _synced_cards:
        return
    bot_token, _ = load_telegram_config()
    if not bot_token:
        return
    bot_env_path = PROJECT_DIR / "bot" / ".env"
    if not bot_env_path.exists():
        return
    values = dotenv_values(bot_env_path)
    admin_id = values.get("ADMIN_CHAT_ID")
    if not admin_id:
        return

    db_id = state.get("realizaciya_gallery_db_id", "")
    db_url = f"https://www.notion.so/{db_id.replace('-', '')}" if db_id else ""

    # Описания изменений от бота (если есть)
    changes = _load_sync_changes()
    summary_text = changes.get("summary", "")  # общий смысл изменений

    new_cards = [t for t, n in _synced_cards if n]
    updated_cards = [t for t, n in _synced_cards if not n]

    parts = []
    if summary_text:
        parts.append(summary_text)
    if new_cards:
        names = ", ".join(new_cards)
        parts.append(f"<b>Новое ({len(new_cards)}):</b> {names}")
    if updated_cards:
        names = ", ".join(updated_cards)
        parts.append(f"Обновлено ({len(updated_cards)}): {names}")

    text = "\n".join(parts)
    if db_url:
        text += f"\n\n{db_url}"

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": admin_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code == 200:
            safe_print(f"  [tg] Сводка отправлена админу ({len(_synced_cards)} шт)")
        else:
            safe_print(f"  [!] TG ошибка: {r.status_code}")
    except Exception as e:
        safe_print(f"  [!] Ошибка отправки сводки: {e}")
    _synced_cards.clear()
    _clear_sync_changes()


# ─── Маппинг: файл -> Notion-карточка (автоматический) ──────────
# Сканирует karta-idej/idei/*.md, читает frontmatter `название:`,
# и строит CARD_MAP автоматически. project/ подключается
# к главной карточке проекта.

# Файлы из project/ которые НЕ синхронизируются в главную карточку проекта
_PROJECT_SKIP = {"README.md"}


def _parse_frontmatter_name(text):
    """Извлекает название карточки: сначала 'название:' из frontmatter, затем '# Заголовок'."""
    # 1) Из frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            fm = text[3:end]
            for line in fm.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("название:"):
                    return stripped.split(":", 1)[1].strip()
    # 2) Fallback: первый заголовок # в файле
    for line in text.splitlines():
        if line.startswith("# "):
            return line.lstrip("#").strip()
    return None


def _extract_slug(filename):
    """Убирает дату-префикс (YYYY-MM-DD-) из имени файла."""
    stem = Path(filename).stem
    m = re.match(r"\d{4}-\d{2}-\d{2}-(.*)", stem)
    return m.group(1) if m else stem


def discover_card_map():
    """Сканирует karta-idej/idei/ и строит маппинг карточек мечт.

    Новые файлы подхватываются без правки кода.
    Главная карточка проекта дополнительно включает все файлы из project/.
    """
    idei_dir = PROJECT_DIR / "karta-idej" / "idei"
    if not idei_dir.exists():
        return {}

    card_map = {}
    for md_file in sorted(idei_dir.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        name = _parse_frontmatter_name(content)
        if not name:
            continue

        slug = _extract_slug(md_file.name)
        rel_path = f"karta-idej/idei/{md_file.name}"

        source_files = [rel_path]

        # Главная карточка проекта — добавить все файлы project/
        _main_kw = os.environ.get("MAIN_PROJECT_KEYWORD", "").lower()
        if _main_kw and _main_kw in name.lower():
            project_dir = PROJECT_DIR / "project"
            if project_dir.exists():
                for bf in sorted(project_dir.glob("*.md")):
                    if bf.name not in _PROJECT_SKIP:
                        source_files.append(f"project/{bf.name}")

        # Составные карточки ("YouTube-канал и две книги" → 2 записи)
        parts = re.split(r'\s+и\s+(?:две\s+)?', name, maxsplit=1)
        if len(parts) == 2 and len(parts[0]) > 3 and len(parts[1]) > 3:
            for i, part in enumerate(parts):
                part_slug = f"{slug}-part{i}"
                card_map[part_slug] = {
                    "match_title": part.split("—")[0].split("–")[0].strip(),
                    "source_files": list(source_files),
                }
        else:
            card_map[slug] = {
                "match_title": name.split("—")[0].split("–")[0].strip(),
                "source_files": source_files,
            }

    return card_map


CARD_MAP = discover_card_map()

# ─── Standalone Pages: отключены (не используются) ─────────────

STANDALONE_PAGES = {}

# ─── Реализация: Gallery Database ─────────────────────────────

# ─── Обложки: Unsplash API → кеш → fallback ──────────────────
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
_COVER_CACHE_FILE = NOTION_DIR / ".cover_cache.json"
_COVER_FALLBACK = "https://images.unsplash.com/photo-1506905925346-21bda4d32df4?w=1600&h=900&fit=crop"

# Русские ключевые слова → английские запросы для Unsplash
_SEARCH_HINTS = [
    (["рынок", "обзор"], "market analytics"),
    (["юридик", "прецедент", "юрист"], "law legal documents"),
    (["бартер", "обмен услуг"], "handshake business deal"),
    (["спрос", "востребованност"], "demand chart analytics"),
    (["гостев", "базы отдых"], "resort wooden cabin"),
    (["дом", "брус", "строительств"], "wooden house construction"),
    (["участок", "земл"], "land plot forest"),
    (["pvz", "ozon", "пвз", "доставк"], "delivery warehouse logistics"),
    (["ретрит", "wellness"], "wellness retreat nature"),
    (["youtube", "канал", "медиа"], "youtube video creator"),
    (["книг", "издан"], "books library"),
    (["живопис", "лёд", "льд", "картин"], "painting art ice"),
    (["свеч"], "candles handmade craft"),

    (["кредит", "ипотек", "финанс"], "finance money bank"),
    (["грант", "программ", "субсид"], "government grant funding"),
    (["солнечн", "энерг"], "solar panels energy"),
    (["модульн", "бан"], "wooden sauna bathhouse"),
    # Добавьте подсказки для своего проекта
]


def _load_cover_cache():
    """Загружает кеш обложек из файла."""
    if _COVER_CACHE_FILE.exists():
        try:
            return json.loads(_COVER_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cover_cache(cache):
    """Сохраняет кеш обложек в файл."""
    try:
        _COVER_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _search_unsplash(query):
    """Ищет фото на Unsplash по запросу. Возвращает список URL или []."""
    if not UNSPLASH_ACCESS_KEY:
        return []
    try:
        resp = requests.get(
            "https://api.unsplash.com/search/photos",
            params={
                "query": query,
                "orientation": "landscape",
                "per_page": 20,
            },
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return []
        results = resp.json().get("results", [])
        urls = []
        for r in results:
            raw = r.get("urls", {}).get("raw", "")
            if raw:
                urls.append(raw + "&w=1600&h=900&fit=crop&q=80")
        return urls
    except Exception:
        return []


def _build_search_query(title):
    """Строит поисковый запрос для Unsplash из заголовка карточки."""
    title_low = title.lower()
    # Сначала проверяем подсказки
    for keywords, query in _SEARCH_HINTS:
        if any(kw in title_low for kw in keywords):
            return query
    # Убираем мусор из заголовка и используем как запрос
    clean = re.sub(r'[\U0001F300-\U0001FFFF\U00002702-\U000027B0\U0000FE0F]', '', title)
    clean = re.sub(r'\s*[—–-]\s*исследование\s*$', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def get_cover_url(title):
    """Подбирает обложку: Unsplash API (с кешем) → fallback.

    Кеш хранится в .cover_cache.json — повторные sync не тратят API-запросы.
    Выбор из результатов детерминированный (по хешу заголовка)."""
    cache = _load_cover_cache()
    cache_key = hashlib.md5(title.lower().encode()).hexdigest()[:12]

    # Есть в кеше — сразу отдаём
    if cache_key in cache:
        return cache[cache_key]

    # Ищем через Unsplash API
    query = _build_search_query(title)
    urls = _search_unsplash(query)

    if urls:
        # Детерминированный выбор по хешу (разные карточки → разные фото)
        idx = int(hashlib.md5(title.lower().encode()).hexdigest(), 16) % len(urls)
        url = urls[idx]
        cache[cache_key] = url
        _save_cover_cache(cache)
        safe_print(f"  [cover] Unsplash: {query}")
        return url

    # Нет ключа или API недоступен — fallback
    return _COVER_FALLBACK

REALIZACIYA_STATUS_OPTIONS = [
    {"name": "✅ Готово", "color": "green"},
    {"name": "🔬 Исследование", "color": "blue"},
    {"name": "⏳ Ожидает", "color": "gray"},
    {"name": "🚧 В работе", "color": "yellow"},
]

REALIZACIYA_TYPE_OPTIONS = [
    {"name": "📊 Исследование", "color": "blue"},
    {"name": "📋 КП", "color": "green"},
    {"name": "💰 Бизнес-план", "color": "orange"},
]

# Файлы-дочерние страницы (не создавать как отдельные карточки)
# Динамически: все kp-tekst-*.md в realizaciya/ считаются дочерними
def _discover_child_files():
    """Находит все kp-tekst-*.md в realizaciya/."""
    d = PROJECT_DIR / "realizaciya"
    if not d.exists():
        return set()
    return {f.name for f in d.glob("kp-tekst-*.md")}

_CHILD_FILES = _discover_child_files()
_SKIP_FILES = {"index.md"}

_STATUS_MAP = {
    "исследование": "🔬 Исследование",
    "исследование завершено": "✅ Готово",
    "готово": "✅ Готово",
    "готово к передаче": "✅ Готово",
    "в работе": "🚧 В работе",
    "ожидает": "⏳ Ожидает",
    "приложение": "✅ Готово",
}


def _parse_status_from_file(lines):
    """Извлекает статус из '> Статус: ...' в первых 5 строках."""
    for line in lines[:5]:
        stripped = line.strip().strip(">").strip()
        if stripped.lower().startswith("статус:"):
            raw = stripped.split(":", 1)[1].strip().strip("*").strip()
            raw_low = raw.lower()
            for key, val in _STATUS_MAP.items():
                if key in raw_low:
                    return val
            return "🔬 Исследование"
    return "🔬 Исследование"


def _detect_type(stem):
    """Определяет тип карточки по имени файла."""
    if stem.startswith("kp-"):
        return "📋 КП"
    return "📊 Исследование"


def discover_realizaciya_cards():
    """Сканирует realizaciya/ и строит список карточек автоматически.

    Новые файлы подхватываются без правки кода.
    Заголовок и статус читаются из первых строк .md файла.
    """
    realizaciya_dir = PROJECT_DIR / "realizaciya"
    if not realizaciya_dir.exists():
        return []

    cards = []
    for md_file in sorted(realizaciya_dir.glob("*.md")):
        if md_file.name in _SKIP_FILES or md_file.name in _CHILD_FILES:
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        lines = content.splitlines()
        if not lines:
            continue

        title = lines[0].lstrip("#").strip()
        if not title:
            continue

        stem = md_file.stem
        card = {
            "key": stem,
            "title": title,
            "icon": "📄",
            "status": _parse_status_from_file(lines),
            "type": _detect_type(stem),
            "cover_key": stem,
            "source_file": f"realizaciya/{md_file.name}",
        }

        # КП с дочерними файлами — автоматическое обнаружение kp-tekst-*.md
        if stem == "kp-obmen-uslugi":
            children = []
            for child_name in sorted(_CHILD_FILES):
                child_path = realizaciya_dir / child_name
                try:
                    first_line = child_path.read_text(encoding="utf-8").split("\n", 1)[0]
                    child_title = first_line.lstrip("#").strip() or child_name
                except Exception:
                    child_title = child_name
                child_stem = child_path.stem  # e.g. "kp-tekst-oteli"
                child_key = child_stem.replace("kp-tekst-", "kp-")
                icon = "\U0001f4c4"  # 📄
                children.append({
                    "key": child_key,
                    "title": child_title,
                    "icon": icon,
                    "source_file": f"realizaciya/{child_name}",
                })
            if children:
                card["child_pages"] = children

        cards.append(card)

    return cards


REALIZACIYA_CARDS = discover_realizaciya_cards()

# ─── Notion API ────────────────────────────────────────────────


def api(method, endpoint, data=None):
    for attempt in range(3):
        try:
            r = requests.request(
                method, f"{BASE_URL}{endpoint}", headers=HEADERS, json=data,
                timeout=30,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            safe_print(f"  CONN ERR (attempt {attempt+1}): {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            retry_after = 2
            try:
                retry_after = int(float(r.headers.get("Retry-After", 2)))
            except (ValueError, TypeError):
                pass
            time.sleep(retry_after)
            continue
        if r.status_code in (502, 503, 504):
            safe_print(f"  {r.status_code} (attempt {attempt+1}), retry...")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code not in (200, 201):
            safe_print(f"  ERR {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    return None


# ─── Notion Block Builders ────────────────────────────────────


NOTION_TEXT_LIMIT = 2000  # Notion API: max 2000 chars per rich_text element


def rt(text, bold=False, italic=False, color="default"):
    # Notion API отвергает текст > 2000 символов — обрезаем с многоточием
    if len(text) > NOTION_TEXT_LIMIT:
        text = text[:NOTION_TEXT_LIMIT - 1] + "…"
    return {
        "type": "text",
        "text": {"content": text},
        "annotations": {
            "bold": bold,
            "italic": italic,
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": color,
        },
    }


def rt_link(text, url):
    """Rich text element with link."""
    if len(text) > NOTION_TEXT_LIMIT:
        text = text[:NOTION_TEXT_LIMIT - 1] + "…"
    return {
        "type": "text",
        "text": {"content": text, "link": {"url": url}},
        "annotations": {
            "bold": False, "italic": False, "strikethrough": False,
            "underline": False, "code": False, "color": "default",
        },
    }


def _split_long_text(text, limit=NOTION_TEXT_LIMIT):
    """Разбивает текст на части ≤ limit символов по пробелам/переносам."""
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        # Ищем последний пробел/перенос в пределах лимита
        cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit  # нет хорошего места — режем жёстко
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    return parts


def _parse_inline(text):
    """Парсит **bold**, *italic* и [ссылки](url) в rich_text элементы."""
    elements = []
    # Паттерн: [текст](url), **bold**, *italic*, обычный текст
    pattern = r"(\[([^\]]+)\]\(([^)]+)\)|\*\*(.+?)\*\*|\*(.+?)\*|([^*\[]+))"
    for match in re.finditer(pattern, text):
        if match.group(2) and match.group(3):  # [текст](url)
            link_text = match.group(2)
            url = match.group(3)
            if url.startswith("http"):
                elements.append(rt_link(link_text, url))
            else:
                # Локальная ссылка на файл — просто текст
                elements.append(rt(link_text, bold=True))
        elif match.group(4):  # **bold**
            # Разбиваем длинный bold на части
            for chunk in _split_long_text(match.group(4)):
                elements.append(rt(chunk, bold=True))
        elif match.group(5):  # *italic*
            for chunk in _split_long_text(match.group(5)):
                elements.append(rt(chunk, italic=True))
        elif match.group(6):  # plain text
            for chunk in _split_long_text(match.group(6)):
                elements.append(rt(chunk))
    return elements if elements else [rt(text)]


def block_h2(text, color="default"):
    return {
        "type": "heading_2",
        "heading_2": {
            "rich_text": [rt(text)],
            "color": color,
            "is_toggleable": False,
        },
    }


def block_h3(text, color="default"):
    return {
        "type": "heading_3",
        "heading_3": {
            "rich_text": [rt(text)],
            "color": color,
            "is_toggleable": False,
        },
    }


def block_para(text, color="default"):
    return {
        "type": "paragraph",
        "paragraph": {"rich_text": _parse_inline(text), "color": color},
    }


def block_empty():
    return {"type": "paragraph", "paragraph": {"rich_text": [], "color": "default"}}


def block_bullet(text):
    return {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _parse_inline(text)},
    }


def block_quote(text, color="default"):
    return {
        "type": "quote",
        "quote": {"rich_text": [rt(text, italic=True)], "color": color},
    }


def block_todo(text, checked=False):
    return {
        "type": "to_do",
        "to_do": {"rich_text": _parse_inline(text), "checked": checked},
    }


def block_callout(text, icon="💡", color="default", children=None):
    b = {
        "type": "callout",
        "callout": {
            "rich_text": _parse_inline(text) if isinstance(text, str) else text,
            "icon": {"type": "emoji", "emoji": icon},
            "color": color,
        },
    }
    if children:
        b["callout"]["children"] = children
    return b


def block_divider():
    return {"type": "divider", "divider": {}}


def block_toc(color="gray"):
    return {"type": "table_of_contents", "table_of_contents": {"color": color}}


def block_toggle_h3(text, color="default", children=None):
    """Toggle-заголовок H3 с вложенными блоками."""
    b = {
        "type": "heading_3",
        "heading_3": {
            "rich_text": _parse_inline(text),
            "color": color,
            "is_toggleable": True,
        },
    }
    if children:
        b["heading_3"]["children"] = children
    return b


# ─── Markdown -> Notion Parser ─────────────────────────────────

# Секция -> цвет и эмодзи
SECTION_STYLES = {
    "суть": ("blue", "💡"),
    "видение": ("blue", "🏠"),
    "концепция": ("blue", "💡"),
    "формат": ("green", "🧘"),
    "программа": ("blue", "📋"),
    "архитектур": ("blue", "🏡"),
    "почему": ("blue", "🌊"),
    "участок": ("orange", "📍"),
    "финанс": ("orange", "💰"),
    "актив": ("orange", "💰"),
    "риск": ("red", "⚠️"),
    "вопрос": ("red", "❓"),
    "открыт": ("red", "❓"),
    "связ": ("green", "🔗"),
    "след": ("green", "✅"),
    "шаг": ("green", "✅"),
    "потенциал": ("purple", "🚀"),
    "портрет": ("blue", "👤"),
    "покупател": ("blue", "👤"),
    "бизнес": ("orange", "💰"),
    "цитат": ("purple", "💬"),
    "контекст": ("gray", "📝"),
    "заметк": ("gray", "📝"),
    "повтор": ("purple", "🔄"),
    "реализац": ("green", "🚀"),
    "сервис": ("green", "🧩"),
}


def get_section_color(heading_text):
    """Подбирает цвет и эмодзи по ключевым словам в заголовке."""
    lower = heading_text.lower()
    for keyword, (color, _emoji) in SECTION_STYLES.items():
        if keyword in lower:
            return color
    return "default"


def parse_markdown_to_blocks(markdown_text):
    """Конвертирует markdown-текст в список Notion-блоков."""
    blocks = []
    lines = markdown_text.split("\n")

    # Пропустить YAML frontmatter
    in_frontmatter = False
    content_lines = []
    for line in lines:
        if line.strip() == "---":
            if not in_frontmatter and not content_lines:
                in_frontmatter = True
                continue
            elif in_frontmatter:
                in_frontmatter = False
                continue
        if not in_frontmatter:
            content_lines.append(line)

    # Убрать ведущие пустые строки
    while content_lines and not content_lines[0].strip():
        content_lines.pop(0)

    i = 0
    while i < len(content_lines):
        line = content_lines[i]
        stripped = line.strip()

        # Пустая строка
        if not stripped:
            # Добавляем пустой параграф только если предыдущий блок не пустой
            if blocks and blocks[-1].get("type") != "paragraph" or (
                blocks
                and blocks[-1].get("paragraph", {}).get("rich_text")
            ):
                blocks.append(block_empty())
            i += 1
            continue

        # ## Heading 2
        if stripped.startswith("## "):
            text = stripped[3:].strip()
            color = get_section_color(text)
            blocks.append(block_h2(text, color=color))
            i += 1
            continue

        # ### Heading 3
        if stripped.startswith("### "):
            text = stripped[4:].strip()
            color = get_section_color(text)
            blocks.append(block_h3(text, color=color))
            i += 1
            continue

        # # Heading 1 -> тоже h2 (не используем h1 внутри карточек)
        if stripped.startswith("# ") and not stripped.startswith("## "):
            text = stripped[2:].strip()
            blocks.append(block_h2(text, color="blue"))
            i += 1
            continue

        # #### Heading 4 -> жирный параграф
        if stripped.startswith("#### "):
            text = stripped[5:].strip()
            blocks.append(block_para("**" + text + "**"))
            i += 1
            continue

        # Таблица (| ... |)
        if stripped.startswith("|") and "|" in stripped[1:]:
            table_rows = []
            while i < len(content_lines):
                row = content_lines[i].strip()
                if not row.startswith("|"):
                    break
                # Пропускаем разделитель (|---|---|)
                if re.match(r"^\|[\s\-:]+\|", row):
                    i += 1
                    continue
                cells = [c.strip() for c in row.split("|")[1:-1]]
                table_rows.append(cells)
                i += 1

            if table_rows:
                # Определяем ширину таблицы
                width = max(len(r) for r in table_rows)
                notion_rows = []
                for row_cells in table_rows:
                    # Дополняем до width
                    while len(row_cells) < width:
                        row_cells.append("")
                    notion_cells = [_parse_inline(c) for c in row_cells[:width]]
                    notion_rows.append({
                        "type": "table_row",
                        "table_row": {"cells": notion_cells},
                    })
                blocks.append({
                    "type": "table",
                    "table": {
                        "table_width": width,
                        "has_column_header": True,
                        "has_row_header": False,
                        "children": notion_rows,
                    },
                })
            continue

        # > Quote
        if stripped.startswith("> "):
            text = stripped[2:].strip()
            blocks.append(block_quote(text, color="blue_background"))
            i += 1
            continue

        # - [ ] / - [x] Todo
        if re.match(r"^-\s*\[([ xX])\]\s*", stripped):
            match = re.match(r"^-\s*\[([ xX])\]\s*(.*)", stripped)
            checked = match.group(1).lower() == "x"
            text = match.group(2)
            blocks.append(block_todo(text, checked=checked))
            i += 1
            continue

        # - Bullet
        if stripped.startswith("- ") or stripped.startswith("• "):
            text = stripped[2:].strip()
            blocks.append(block_bullet(text))
            i += 1
            continue

        # Numbered list (1. 2. etc.) -> bullet
        if re.match(r"^\d+\.\s+", stripped):
            text = re.sub(r"^\d+\.\s+", "", stripped)
            blocks.append(block_bullet(text))
            i += 1
            continue

        # --- Divider (не дублируем подряд)
        if stripped == "---" or stripped == "***" or stripped == "___":
            if not blocks or blocks[-1].get("type") != "divider":
                blocks.append(block_divider())
            i += 1
            continue

        # Обычный текст -> параграф
        # Собираем многострочные параграфы
        para_lines = [stripped]
        while (
            i + 1 < len(content_lines)
            and content_lines[i + 1].strip()
            and not content_lines[i + 1].strip().startswith("#")
            and not content_lines[i + 1].strip().startswith(">")
            and not content_lines[i + 1].strip().startswith("- ")
            and not content_lines[i + 1].strip().startswith("• ")
            and not content_lines[i + 1].strip().startswith("|")
            and not re.match(r"^\d+\.\s+", content_lines[i + 1].strip())
            and not re.match(r"^-\s*\[", content_lines[i + 1].strip())
            and content_lines[i + 1].strip() not in ("---", "***", "___")
        ):
            i += 1
            para_lines.append(content_lines[i].strip())

        blocks.append(block_para("\n".join(para_lines)))
        i += 1

    # Убрать trailing пустые блоки
    while blocks and blocks[-1] == block_empty():
        blocks.pop()

    return blocks


def parse_frontmatter(markdown_text):
    """Извлекает YAML frontmatter из markdown."""
    lines = markdown_text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end == -1:
        return {}
    try:
        return yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return {}


# ─── Sync Logic ────────────────────────────────────────────────


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def find_card_id(state, match_title):
    """Найти ID Notion-карточки по подстроке в заголовке."""
    for card in state.get("dream_cards", []):
        if match_title.lower() in card["title"].lower():
            return card["id"]
    return None


def recreate_page(page_id):
    """Пересоздать страницу: архивировать старую → создать новую с теми же свойствами.

    Возвращает new_page_id или None при ошибке.
    3 API-вызова вместо сотен DELETE. Работает за секунды, не минуты.
    """
    # 1. Прочитать свойства старой страницы
    old_page = api("GET", f"/pages/{page_id}")
    if not old_page:
        return None
    parent = old_page.get("parent")
    if not parent:
        return None

    # 2. Собрать свойства для новой страницы
    new_props = {}
    for name, prop in old_page.get("properties", {}).items():
        ptype = prop.get("type")
        if ptype == "title" and prop.get("title"):
            new_props[name] = {"title": prop["title"]}
        elif ptype == "select" and prop.get("select"):
            new_props[name] = {"select": prop["select"]}
        elif ptype == "rich_text" and prop.get("rich_text"):
            new_props[name] = {"rich_text": prop["rich_text"]}
        elif ptype == "url" and prop.get("url"):
            new_props[name] = {"url": prop["url"]}

    new_data = {"parent": parent, "properties": new_props}
    if old_page.get("icon"):
        new_data["icon"] = old_page["icon"]
    if old_page.get("cover"):
        new_data["cover"] = old_page["cover"]

    # 3. Создать новую пустую страницу
    new_page = api("POST", "/pages", new_data)
    if not new_page:
        return None
    new_id = new_page["id"]

    # 4. Архивировать старую
    api("PATCH", f"/pages/{page_id}", {"archived": True})

    return new_id


def clear_page_content(page_id):
    """Очистить страницу через пересоздание. Возвращает (new_page_id, True) или (page_id, False)."""
    new_id = recreate_page(page_id)
    if new_id:
        return new_id, True
    # Fallback: если пересоздание не удалось — вернуть старый ID, не очищая
    safe_print("    [!] Пересоздание не удалось, пропускаю очистку")
    return page_id, False


def fill_page(page_id, blocks, card_name=""):
    """Записать блоки на страницу (батчами по 100). Возвращает True если все батчи записаны."""
    total = len(blocks)
    for i in range(0, total, 100):
        batch_end = min(i + 100, total)
        _write_progress(current=batch_end, total=total, card=card_name)
        result = api("PATCH", f"/blocks/{page_id}/children", {"children": blocks[i : batch_end]})
        if result is None:
            safe_print(f"    [ERR] fill_page провалился на батче {i//100 + 1}")
            return False
        time.sleep(0.4)
    return True


def read_file(path):
    """Читает файл, возвращает содержимое или None."""
    full = PROJECT_DIR / path
    if full.exists():
        return full.read_text(encoding="utf-8")
    return None


def merge_sources(source_files):
    """Читает и объединяет несколько файлов в один markdown."""
    parts = []
    seen_files = set()
    for path in source_files:
        if path in seen_files:
            continue
        seen_files.add(path)
        content = read_file(path)
        if content:
            # Убираем frontmatter для дополнительных файлов
            if parts:  # не первый файл
                lines = content.split("\n")
                in_fm = False
                clean_lines = []
                for line in lines:
                    if line.strip() == "---" and not in_fm and not clean_lines:
                        in_fm = True
                        continue
                    if line.strip() == "---" and in_fm:
                        in_fm = False
                        continue
                    if not in_fm:
                        clean_lines.append(line)
                content = "\n".join(clean_lines)
            parts.append(content.strip())
    return "\n\n---\n\n".join(parts)


def update_card(card_key, card_config, state):
    """Обновить одну карточку в Notion из файлов."""
    card_id = find_card_id(state, card_config["match_title"])
    if not card_id:
        safe_print(f"  [!] Карточка '{card_config['match_title']}' не найдена в Notion")
        return False

    # Пропускаем архивированные карточки
    page_info = api("GET", f"/pages/{card_id}")
    if not page_info or page_info.get("archived"):
        safe_print(f"  [i] Карточка '{card_config['match_title']}' архивирована, пропускаю")
        return False

    # Собрать содержимое из файлов
    merged = merge_sources(card_config["source_files"])
    if not merged:
        safe_print(f"  [!] Нет файлов для '{card_key}'")
        return False

    # Парсить frontmatter для метаданных
    first_file = read_file(card_config["source_files"][0])
    meta = parse_frontmatter(first_file) if first_file else {}

    # Конвертировать в Notion-блоки
    blocks = parse_markdown_to_blocks(merged)

    # Добавить аффирмацию из frontmatter если есть
    affirmation = meta.get("аффирмация") or meta.get("affirmation")
    if affirmation:
        blocks.insert(
            0,
            block_callout(affirmation, icon="💜", color="purple_background"),
        )
        blocks.insert(1, block_empty())

    if not blocks:
        safe_print(f"  [!] Пустое содержимое для '{card_key}'")
        return False

    # Пересоздать страницу (быстро) и заполнить
    new_id, recreated = clear_page_content(card_id)
    if recreated:
        # Обновить ID в state
        for dc in state.get("dream_cards", []):
            if dc["id"] == card_id:
                dc["id"] = new_id
                break
        save_state(state)
    time.sleep(0.3)
    if not fill_page(new_id, blocks, card_name=card_config.get("match_title", card_key)):
        return False

    return True


# ─── Хеширование файлов ──────────────────────────────────────


def file_hash(path):
    """MD5-хеш содержимого файла."""
    content = read_file(path)
    if content is None:
        return None
    return hashlib.md5(content.encode()).hexdigest()


# ─── Извлечение задач из файлов реализации ───────────────────


def extract_tasks_from_realizaciya():
    """Извлекает задачи из всех файлов realizaciya/.

    Парсит:
    - «**Что делать прежде всего:**» из блока «Главное» (высший приоритет → 🔴)
    - «## Что делать» — нумерованные, чекбоксы, dash-элементы, ### Задача N: (обычные → 🟡)

    Возвращает список: [{"project": str, "icon": str, "tasks": [{"text", "priority", "checked"}]}]
    """
    all_tasks = []

    for card in REALIZACIYA_CARDS:
        content = read_file(card["source_file"])
        if not content:
            continue

        tasks = []
        priority_texts = set()
        lines = content.split("\n")

        # 1) Ищем «Что делать прежде всего» (приоритетные → 🔴)
        in_priority = False
        for line in lines:
            stripped = line.strip()
            if "что делать" in stripped.lower() and stripped.startswith("**"):
                in_priority = True
                continue
            if in_priority:
                if not stripped:
                    break
                m = re.match(r"^\d+\.\s+(.+)", stripped)
                if m:
                    tasks.append({"text": m.group(1), "priority": "high", "checked": False})
                    priority_texts.add(m.group(1))

        # 2) Ищем «## Что делать» (обычные → 🟡)
        in_section = False
        under_zadacha = False  # под «### Задача N:» — подпункты = детали, не задачи
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## Что делать"):
                in_section = True
                under_zadacha = False
                continue
            if not in_section:
                continue
            # Конец секции
            if stripped.startswith("## ") or stripped == "---":
                break
            # «### Задача N:» — сам заголовок = задача
            m_zadacha = re.match(r"^###\s+Задача\s+\d+[:.]\s*(.+)", stripped)
            if m_zadacha:
                under_zadacha = True
                task_text = m_zadacha.group(1)
                if task_text not in priority_texts:
                    tasks.append({"text": task_text, "priority": "medium", "checked": False})
                continue
            # Прочие заголовки — группировка, сбрасываем флаг
            if stripped.startswith("#"):
                under_zadacha = False
                continue
            # Под «### Задача» — пропускаем детали
            if under_zadacha:
                continue
            # Жирные подзаголовки (**Этап 1.**, **Неделя 1:**)
            if stripped.startswith("**") and (stripped.endswith("**") or stripped.endswith(":**")):
                continue
            # Вложенные элементы (с отступом 2+)
            if re.match(r"^[ \t]{2,}", line):
                continue
            if not stripped:
                continue
            # Чекбоксы, нумерованные, простые dash-элементы
            cb_m = re.match(r"^- \[([ xX])\]\s+(.+)", stripped)
            num_m = re.match(r"^\d+\.\s+(.+)", stripped)
            dash_m = re.match(r"^- (.+)", stripped)
            if cb_m:
                checked = cb_m.group(1).lower() == "x"
                task_text = cb_m.group(2)
            elif num_m:
                checked = False
                task_text = num_m.group(1)
            elif dash_m:
                checked = False
                task_text = dash_m.group(1)
            else:
                continue
            # Пропускаем подзаголовки-списки (заканчиваются на :)
            if task_text.rstrip().endswith(":"):
                continue
            # Пропускаем key-value поля (**Телефон:** значение)
            if re.match(r"^\*\*[^*]+:\*\*", task_text):
                continue
            if task_text not in priority_texts:
                tasks.append({"text": task_text, "priority": "medium", "checked": checked})

        if tasks:
            all_tasks.append({
                "project": card["title"],
                "icon": card["icon"],
                "tasks": tasks,
            })

    return all_tasks


# ─── Обновление задач на главной странице ──────────────────────


TASK_HEADING = "📋 Задачи — начни делать"
PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡"}


def update_tasks_on_page(state):
    """Обновить блок задач на главной странице.

    Собирает задачи из realizaciya/, группирует по проектам.
    Визуал: toggle-заголовки + to-do чекбоксы + цветные приоритеты.
    """
    tasks_by_project = extract_tasks_from_realizaciya()
    if not tasks_by_project:
        safe_print("  [i] Нет задач для обновления")
        return

    # Проверяем, изменились ли задачи (по хешу)
    tasks_str = json.dumps(tasks_by_project, ensure_ascii=False, sort_keys=True)
    tasks_hash = hashlib.md5(tasks_str.encode()).hexdigest()
    if state.get("tasks_hash") == tasks_hash:
        safe_print("  [i] Задачи без изменений")
        return

    # Находим блок «Задачи» на странице
    children = api("GET", f"/blocks/{ROOT_PAGE_ID}/children?page_size=100")
    if not children:
        return

    tasks_block_id = None
    tasks_block_idx = None
    blocks_list = children.get("results", [])
    for idx, b in enumerate(blocks_list):
        if b["type"] == "heading_2":
            texts = b["heading_2"].get("rich_text", [])
            if texts and "Задачи" in texts[0].get("plain_text", ""):
                tasks_block_id = b["id"]
                tasks_block_idx = idx
                break

    # Удаляем старое содержимое (блоки после «Задачи» до следующего heading_2 или divider)
    if tasks_block_id:
        safe_print("  [~] Обновляю задачи...")
        # Обновляем заголовок на актуальный
        api("PATCH", f"/blocks/{tasks_block_id}", {
            "heading_2": {
                "rich_text": [rt(TASK_HEADING)],
                "color": "orange",
            }
        })
        blocks_to_delete = []
        for b in blocks_list[tasks_block_idx + 1:]:
            btype = b["type"]
            if btype == "heading_2" or btype == "divider":
                break
            blocks_to_delete.append(b["id"])
        for bid in blocks_to_delete:
            api("DELETE", f"/blocks/{bid}")
            time.sleep(0.15)
    else:
        # Создаём заголовок «Задачи» в конце страницы
        safe_print("  [+] Создаю блок задач...")
        api("PATCH", f"/blocks/{ROOT_PAGE_ID}/children", {
            "children": [
                block_divider(),
                block_h2(TASK_HEADING, color="orange"),
            ],
        })
        time.sleep(0.4)
        children = api("GET", f"/blocks/{ROOT_PAGE_ID}/children?page_size=100")
        if not children:
            return
        for b in children.get("results", []):
            if b["type"] == "heading_2":
                texts = b["heading_2"].get("rich_text", [])
                if texts and "Задачи" in texts[0].get("plain_text", ""):
                    tasks_block_id = b["id"]
                    break

    # Собираем Notion-блоки: легенда + toggle-проекты с to-do внутри
    new_blocks = []

    # Легенда приоритетов
    new_blocks.append(block_callout(
        "🔴 сделать первым   🟡 важно",
        icon="📋", color="gray_background",
    ))

    # По каждому проекту — toggle-заголовок с чекбоксами внутри
    for project in tasks_by_project:
        todo_children = []
        for task in project["tasks"]:
            emoji = PRIORITY_EMOJI.get(task["priority"], "🟡")
            todo_children.append(block_todo(f"{emoji} {task['text']}", checked=task.get("checked", False)))

        new_blocks.append(block_toggle_h3(
            f"{project['icon']} {project['project']}",
            color="blue",
            children=todo_children,
        ))

    # Вставляем после заголовка «Задачи» (одним батчем)
    if tasks_block_id and new_blocks:
        api("PATCH", f"/blocks/{ROOT_PAGE_ID}/children", {
            "children": new_blocks[:100],
            "after": tasks_block_id,
        })
        time.sleep(0.4)

    state["tasks_hash"] = tasks_hash
    save_state(state)
    total = sum(len(p["tasks"]) for p in tasks_by_project)
    safe_print(f"  [ok] Задачи обновлены ({total} пунктов)")


# ─── Standalone Pages Sync ─────────────────────────────────────


def ensure_standalone_page(state, page_config):
    """Создаёт отдельную Notion-страницу если её ещё нет. Возвращает (page_id, is_new)."""
    page_id = state.get(page_config["state_key"])
    if page_id:
        # Проверяем, что страница ещё существует
        check = api("GET", f"/pages/{page_id}")
        if check and not check.get("archived"):
            return page_id, False

    # Определяем родительскую страницу
    parent_key = page_config.get("parent_state_key")
    if parent_key:
        parent_id = state.get(parent_key)
        if not parent_id:
            safe_print(f"  [!] Родительская страница ({parent_key}) не создана, создаю под корнем")
            parent_id = ROOT_PAGE_ID
    else:
        parent_id = ROOT_PAGE_ID

    safe_print(f"\n[+] Создаю страницу «{page_config['title']}»...")
    result = api("POST", "/pages", {
        "parent": {"page_id": parent_id},
        "icon": {"type": "emoji", "emoji": page_config["icon"]},
        "properties": {
            "title": {"title": [rt(page_config["title"])]},
        },
    })
    if not result:
        safe_print(f"  [ERR] Не удалось создать страницу")
        return None, False

    page_id = result["id"]
    state[page_config["state_key"]] = page_id
    save_state(state)
    safe_print(f"  [ok] Страница создана: {page_id}")
    time.sleep(0.5)
    return page_id, True


def collect_standalone_sources(page_config):
    """Собирает список source_files, включая auto_append_dir."""
    files = list(page_config["source_files"])
    auto_dir = page_config.get("auto_append_dir")
    if auto_dir:
        dir_path = PROJECT_DIR / auto_dir
        if dir_path.exists():
            index_names = {"index.md", "README.md"}
            exclude = set(page_config.get("auto_append_exclude", []))
            for md in sorted(dir_path.glob("*.md")):
                rel = str(md.relative_to(PROJECT_DIR)).replace("\\", "/")
                if md.name not in index_names and md.name not in exclude and rel not in files:
                    files.append(rel)
    return files


def sync_standalone_pages(state, old_hashes, new_hashes):
    """Синхронизирует standalone-страницы в Notion."""
    updated = 0
    skipped = 0

    for key, config in STANDALONE_PAGES.items():
        source_files = collect_standalone_sources(config)

        # Проверяем, изменились ли файлы
        changed_files = []
        for path in source_files:
            h = file_hash(path)
            if h is None:
                continue
            new_hashes[path] = h
            if old_hashes.get(path) != h:
                changed_files.append(path)

        # Если страница ещё не создана, обрабатываем даже без изменений в файлах
        page_exists = bool(state.get(config["state_key"]))
        if not changed_files and page_exists:
            skipped += 1
            continue

        page_id, is_new = ensure_standalone_page(state, config)
        if not page_id:
            continue

        safe_print(f"\n[~] {config['title']}...")

        # Собрать и конвертировать содержимое
        merged = merge_sources(source_files)
        if not merged:
            continue

        blocks = parse_markdown_to_blocks(merged)
        if not blocks:
            continue

        # Пересоздать и заполнить
        new_pid, recreated = clear_page_content(page_id)
        if recreated:
            state[config["state_key"]] = new_pid
            page_id = new_pid
            save_state(state)
        time.sleep(0.3)
        fill_page(page_id, blocks, card_name=config.get("title", ""))

        updated += 1
        safe_print(f"  [ok] Обновлено ({len(changed_files)} файлов)")

        # Уведомление о новом материале в разделе «Реализация»
        if is_new and config.get("parent_state_key") == "realizaciya_page_id":
            notify_new_research(config["title"], page_id, state=state, notify_key=f"standalone_{key}", source_file=config["source_files"][0])

    return updated, skipped


# ─── Реализация Gallery Sync ──────────────────────────────────


def extract_glavnoe(md_text):
    """Извлекает секцию '## Главное' из markdown. Возвращает (glavnoe_lines, rest_lines).

    Также убирает заголовок файла (# Title, > Статус, > Дата) из rest,
    т.к. они уже отражены в заголовке и свойствах карточки Notion.
    """
    lines = md_text.split("\n")
    in_glavnoe = False
    glavnoe = []
    rest = []

    # Фаза 1: пропускаем шапку файла (# Title, > Статус/Дата, ---)
    i = 0
    in_header = True
    while i < len(lines) and in_header:
        s = lines[i].strip()
        if not s:  # пустые строки в шапке — пропускаем
            i += 1
            continue
        if s.startswith("# ") and not s.startswith("## "):  # заголовок файла
            i += 1
            continue
        # Только метаданные: > Статус: и > Дата: — НЕ произвольные > строки
        s_lower = s.lower().lstrip("> ").strip()
        if s.startswith(">") and (s_lower.startswith("статус") or s_lower.startswith("дата")):
            i += 1
            continue
        if s == "---":  # разделитель после шапки
            i += 1
            in_header = False
            continue
        in_header = False  # встретили контент — шапка закончилась

    # Фаза 2: разделяем «Главное» и остальное
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("## Главное"):
            in_glavnoe = True
            i += 1
            continue
        if in_glavnoe:
            if s.startswith("## ") or s == "---":
                in_glavnoe = False
                rest.append(line)
                i += 1
                continue
            glavnoe.append(line)
        else:
            rest.append(line)
        i += 1

    # Убираем ведущие разделители и пустые строки из rest (избегаем двойных ---)
    while rest and rest[0].strip() in ("", "---"):
        rest.pop(0)

    return "\n".join(glavnoe).strip(), "\n".join(rest).strip()


def format_research_blocks(md_text):
    """Форматирует исследование: callout с выводами -> оглавление -> детали."""
    glavnoe_text, rest_text = extract_glavnoe(md_text)

    blocks = []

    # ── Блок «Главное» — яркий callout наверху ──
    if glavnoe_text:
        glavnoe_children = parse_markdown_to_blocks(glavnoe_text)
        blocks.append(block_callout(
            [rt("Главное", bold=True, color="blue")],
            icon="🎯",
            color="blue_background",
            children=glavnoe_children[:90],  # Notion: max 100 children
        ))
        blocks.append(block_empty())

    # ── Кликабельное оглавление ──
    blocks.append(block_h2("📑 Оглавление", color="gray"))
    blocks.append(block_toc(color="gray"))
    blocks.append(block_empty())
    blocks.append(block_divider())

    # ── Детальное содержимое ──
    rest_blocks = parse_markdown_to_blocks(rest_text)
    blocks.extend(rest_blocks)

    return blocks


def ensure_realizaciya_db(state):
    """Создаёт gallery database «Реализация» на главной странице."""
    db_id = state.get("realizaciya_gallery_db_id")
    if db_id:
        check = api("GET", f"/databases/{db_id}")
        if check and not check.get("archived"):
            return db_id

    safe_print("\n[+] Создаю базу «Реализация» (галерея)...")

    # Заголовок перед базой
    api("PATCH", f"/blocks/{ROOT_PAGE_ID}/children", {
        "children": [
            block_divider(),
            block_h2("🚀 Реализация — исследования и задачи", color="green"),
        ],
    })
    time.sleep(0.4)

    result = api("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": ROOT_PAGE_ID},
        "title": [rt("Реализация")],
        "is_inline": True,
        "properties": {
            "Название": {"title": {}},
            "Статус": {"select": {"options": REALIZACIYA_STATUS_OPTIONS}},
            "Тип": {"select": {"options": REALIZACIYA_TYPE_OPTIONS}},
        },
    })
    if not result:
        safe_print("  [ERR] Не удалось создать базу")
        return None

    db_id = result["id"]
    state["realizaciya_gallery_db_id"] = db_id
    save_state(state)
    safe_print(f"  [ok] База создана: {db_id}")
    safe_print("  [i] В Notion: переключи на Gallery view (••• -> Layout -> Gallery -> Card preview: Page Cover)")
    time.sleep(0.5)
    return db_id


def sync_realizaciya_gallery(state, old_hashes, new_hashes):
    """Синхронизирует карточки исследований в галерею «Реализация»."""
    db_id = ensure_realizaciya_db(state)
    if not db_id:
        return 0, 0

    cards_state = state.get("realizaciya_cards", {})
    updated = 0
    skipped = 0

    for card_config in REALIZACIYA_CARDS:
        key = card_config["key"]
        source = card_config["source_file"]

        # Проверяем хеш
        h = file_hash(source)
        if h is None:
            continue

        source_changed = old_hashes.get(source) != h
        # НЕ пишем new_hashes здесь — только после успешного fill_page

        # Проверяем дочерние файлы
        children_changed = False
        child_hashes_pending = {}  # сохраним после успешной записи
        for child in card_config.get("child_pages", []):
            ch = file_hash(child["source_file"])
            if ch:
                if old_hashes.get(child["source_file"]) != ch:
                    children_changed = True
                child_hashes_pending[child["source_file"]] = ch

        if not source_changed and not children_changed and key in cards_state:
            skipped += 1
            continue

        card_id = cards_state.get(key)

        # ── Создание карточки (с защитой от дублей) ──
        if not card_id:
            # Сначала проверяем, нет ли уже такой карточки в базе Notion
            existing = api("POST", f"/databases/{db_id}/query", {
                "filter": {"property": "Название", "title": {"equals": card_config["title"]}},
                "page_size": 1,
            })
            if existing and existing.get("results"):
                card_id = existing["results"][0]["id"]
                cards_state[key] = card_id
                state["realizaciya_cards"] = cards_state
                save_state(state)
                safe_print(f"\n  [~] Найдена существующая карточка: {card_config['title']}")
            else:
                cover_url = get_cover_url(card_config["title"])
                page_data = {
                    "parent": {"database_id": db_id},
                    "icon": {"type": "emoji", "emoji": card_config["icon"]},
                    "properties": {
                        "Название": {"title": [rt(card_config["title"])]},
                        "Статус": {"select": {"name": card_config["status"]}},
                        "Тип": {"select": {"name": card_config["type"]}},
                    },
                    "cover": {"type": "external", "external": {"url": cover_url}},
                }

                result = api("POST", "/pages", page_data)
                if not result:
                    continue
                card_id = result["id"]
                cards_state[key] = card_id
                state["realizaciya_cards"] = cards_state
                save_state(state)
                safe_print(f"\n  [+] Создана карточка: {card_config['title']}")
            time.sleep(0.5)
        else:
            # Если карточка архивирована — сбросить и создать заново
            page_check = api("GET", f"/pages/{card_id}")
            if not page_check or page_check.get("archived"):
                safe_print(f"  [i] {card_config['title'][:40]}... архивирована, пересоздаю")
                card_id = None
                del cards_state[key]
                # Пойдёт на создание ниже

        # Если card_id сброшен (была архивирована) — создать заново
        if not card_id:
            cover_url = get_cover_url(card_config["title"])
            page_data = {
                "parent": {"database_id": db_id},
                "icon": {"type": "emoji", "emoji": card_config["icon"]},
                "properties": {
                    "Название": {"title": [rt(card_config["title"])]},
                    "Статус": {"select": {"name": card_config["status"]}},
                    "Тип": {"select": {"name": card_config["type"]}},
                },
                "cover": {"type": "external", "external": {"url": cover_url}},
            }
            result = api("POST", "/pages", page_data)
            if not result:
                continue
            card_id = result["id"]
            cards_state[key] = card_id
            state["realizaciya_cards"] = cards_state
            save_state(state)
            safe_print(f"\n  [+] Пересоздана карточка: {card_config['title']}")
            time.sleep(0.5)
        else:
            # Обновляем статус и обложку
            patch_data = {
                "properties": {
                    "Статус": {"select": {"name": card_config["status"]}},
                },
            }
            cover_url = get_cover_url(card_config["title"])
            patch_data["cover"] = {"type": "external", "external": {"url": cover_url}}
            api("PATCH", f"/pages/{card_id}", patch_data)

        # ── Пересоздать основную карточку ПЕРЕД дочерними ──
        content = read_file(source)
        if content:
            new_card_id, card_recreated = clear_page_content(card_id)
            if card_recreated:
                cards_state[key] = new_card_id
                card_id = new_card_id
            time.sleep(0.3)

        # ── Дочерние страницы (создаём внутри новой карточки) ──
        for child in card_config.get("child_pages", []):
            child_key = child["key"]
            # Всегда создаём заново внутри новой карточки
            child_result = api("POST", "/pages", {
                "parent": {"page_id": card_id},
                "icon": {"type": "emoji", "emoji": child["icon"]},
                "properties": {
                    "title": {"title": [rt(child["title"])]},
                },
            })
            if child_result:
                child_id = child_result["id"]
                cards_state[child_key] = child_id
                time.sleep(0.3)

                child_content = read_file(child["source_file"])
                if child_content:
                    child_blocks = parse_markdown_to_blocks(child_content)
                    fill_page(child_id, child_blocks, card_name=child.get("title", ""))

        # ── Заполнение содержимым основной карточки ──
        if content:

            blocks = []

            # Если есть дочерние — кнопки наверху с подсказкой
            children_list = card_config.get("child_pages", [])
            if children_list:
                blocks.append(block_callout(
                    [rt("Выбери нужный вариант КП — нажми и скопируй текст 👇", bold=True, color="green")],
                    icon="📨",
                    color="green_background",
                ))
                for child in children_list:
                    cid = cards_state.get(child["key"])
                    if cid:
                        blocks.append({
                            "type": "link_to_page",
                            "link_to_page": {"type": "page_id", "page_id": cid},
                        })
                blocks.append(block_divider())
                # Оглавление исследования — после кнопок
                blocks.append(block_toc(color="gray"))
                blocks.append(block_empty())

            # Содержимое исследования — обычный парсинг, без callout «Главное»
            if children_list:
                blocks.extend(parse_markdown_to_blocks(content))
            else:
                blocks.extend(format_research_blocks(content))

            if not fill_page(card_id, blocks, card_name=card_config["title"]):
                safe_print(f"  [ERR] Не удалось записать: {card_config['title']}")
                continue
            # Хеш пишем ТОЛЬКО после успешной записи
            new_hashes[source] = h
            for cf, ch in child_hashes_pending.items():
                new_hashes[cf] = ch
            safe_print(f"  [ok] Обновлено содержимое: {card_config['title']}")

            updated += 1
            is_new_card = f"card_{key}" not in state.get("notified_cards", [])
            track_synced_card(card_config["title"], is_new=is_new_card, source_file=source)

        # Сохраняем хеши и state после каждой карточки (защита от таймаута)
        state["realizaciya_cards"] = cards_state
        state["file_hashes"] = new_hashes
        save_state(state)

    # ── Auto-cleanup: удалить ключи, которых нет среди текущих файлов ──
    current_keys = {c["key"] for c in REALIZACIYA_CARDS}
    # Дочерние ключи тоже легитимны
    for c in REALIZACIYA_CARDS:
        for child in c.get("child_pages", []):
            current_keys.add(child["key"])
    stale_keys = [k for k in cards_state if k not in current_keys]
    for k in stale_keys:
        stale_id = cards_state[k]
        safe_print(f"  [cleanup] Архивирую устаревшую карточку: {k}")
        api("PATCH", f"/pages/{stale_id}", {"archived": True})
        del cards_state[k]
        time.sleep(0.3)

    state["realizaciya_cards"] = cards_state
    save_state(state)

    return updated, skipped


# ─── Lock (защита от параллельного запуска) ──────────────────


def _acquire_lock():
    """Проверяет lock-файл. Если sync уже запущен — возвращает False."""
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            # Проверяем, жив ли процесс (не убиваем!)
            os.kill(old_pid, 0)
            safe_print(f"[LOCK] Sync уже запущен (PID {old_pid}), выхожу")
            return False
        except (ProcessLookupError, OSError, ValueError):
            # Процесс мёртв — stale lock, перехватываем
            pass
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _release_lock():
    """Удаляет lock-файл."""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ─── Main ──────────────────────────────────────────────────────


def main():
    if not _acquire_lock():
        _write_progress(error="locked")
        safe_print("[LOCK] Sync уже запущен другим процессом, выхожу")
        os._exit(2)  # не 0 — чтобы бот показал ошибку, а не "OK"

    safe_print("=" * 50)
    safe_print("Синхронизация файлов -> Notion")
    safe_print("=" * 50)

    _exit_code = 1  # по умолчанию ошибка, перезаписывается при успехе
    try:
        safe_print("  Загрузка состояния...")
        state = load_state()
        if not state.get("dream_cards"):
            safe_print("[ERR] Нет .notion_state.json. State-файл повреждён или отсутствует")
            _write_progress(error="state file missing")
            return

        safe_print("  Начинаю синхронизацию...")
        old_hashes = state.get("file_hashes", {})
        new_hashes = dict(old_hashes)

        updated = 0
        skipped = 0
        errors = 0

        card_list = list(CARD_MAP.items())
        total_steps = len(card_list) + 3  # cards + standalone + realizaciya + tasks
        for idx, (card_key, config) in enumerate(card_list, 1):
            _write_progress(current=idx, total=total_steps, card=config.get('match_title', card_key))
            safe_print(f"  [{idx}/{len(card_list)}] {config.get('match_title', card_key)}...")
            # Определяем, какие файлы изменились
            changed_files = []
            for path in config["source_files"]:
                h = file_hash(path)
                if h is None:
                    continue
                new_hashes[path] = h
                if old_hashes.get(path) != h:
                    changed_files.append(path)

            if not changed_files:
                skipped += 1
                continue

            safe_print(f"\n[~] {config['match_title']}...")
            if update_card(card_key, config, state):
                updated += 1
                safe_print(f"  [ok] Обновлено")
                # Сохраняем хеши после каждой карточки (защита от таймаута)
                state["file_hashes"] = new_hashes
                save_state(state)
            else:
                errors += 1
                # Откатываем хеши — чтобы следующий sync повторил попытку
                for path in changed_files:
                    if path in old_hashes:
                        new_hashes[path] = old_hashes[path]
                    elif path in new_hashes:
                        del new_hashes[path]

        # Standalone pages (состояние)
        _write_progress(current=len(card_list) + 1, total=total_steps, card="Standalone pages")
        safe_print("\n  Standalone pages...")
        sa_updated, sa_skipped = sync_standalone_pages(
            state, old_hashes, new_hashes
        )
        updated += sa_updated
        skipped += sa_skipped

        # Реализация — галерея с карточками исследований
        _write_progress(current=len(card_list) + 2, total=total_steps, card="Realizaciya gallery")
        safe_print("  Realizaciya gallery...")
        rg_updated, rg_skipped = sync_realizaciya_gallery(
            state, old_hashes, new_hashes
        )
        updated += rg_updated
        skipped += rg_skipped

        # Сохраняем хеши
        safe_print("  Сохранение состояния...")
        state["file_hashes"] = new_hashes
        save_state(state)

        _write_progress(current=len(card_list) + 3, total=total_steps, card="Tasks")
        safe_print("  Tasks...")
        import threading
        _tasks_done = threading.Event()
        def _run_tasks():
            try:
                update_tasks_on_page(state)
            except Exception as e:
                safe_print(f"  [!] Tasks ошибка: {e}")
            finally:
                _tasks_done.set()
        _t = threading.Thread(target=_run_tasks, daemon=True)
        _t.start()
        if not _tasks_done.wait(timeout=120):
            safe_print("  [!] Tasks зависли (>120 сек), пропускаю")

        # Единое уведомление о новых карточках — в личку админу
        send_sync_summary(state)

        safe_print(f"\n{'=' * 50}")
        safe_print(f"[ok] Обновлено: {updated} | Без изменений: {skipped} | Ошибок: {errors}")
        safe_print("=" * 50)
        _write_progress(current=1, total=1, card="done")
        _exit_code = 0
    except Exception as e:
        safe_print(f"\n[CRASH] {type(e).__name__}: {e}")
        _write_progress(error=f"{type(e).__name__}: {e}")
        _exit_code = 1
    finally:
        _release_lock()
        os._exit(_exit_code)


if __name__ == "__main__":
    _write_progress(current=0, total=0, card="starting")
    main()
