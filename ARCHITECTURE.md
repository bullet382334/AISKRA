# Архитектура системы

## 1. Обзор

Система сбора и развития идей: голосовые и текстовые сообщения от заказчика проходят цепочку
транскрипция - буферизация - обработка Claude - размещение в файлах - синхронизация в Notion.
Управление через Telegram-бота (админ) и группу (заказчик).

```
Telegram-группа ──→ Бот ──→ Буфер ──→ Claude ──→ Карточки/Исследования ──→ Notion
       ↑                                                    ↓
  Заказчик                                         Админ (уведомления)
```

## 2. Компоненты

| Компонент | Файл | Роль |
|-----------|------|------|
| Telegram-бот | `bot/bot.py` | Приём сообщений, буферизация, запуск Claude, управление |
| Notion sync | `notion/update_notion.py` | Конвертация .md → Notion-блоки, обновление карточек |
| Отправка админу | `bot/send_to_admin.py` | Отправка сообщений с inline-кнопками в ЛС админу |
| Отправка в группу | `bot/send_to_group.py` | Отправка текстовых сообщений в группу заказчика |
| Claude CLI | внешний (`npx claude`) | Обработка транскрипций, исследования, ответы на вопросы |
| Буквица | внешний (`BukvitsaAI_bot`) | Транскрипция голосовых через Telethon |
| Gating rules | `.claude/workflows/gating-rules.md` | Правила размещения новой информации |
| Kontekst loading | `.claude/workflows/kontekst-loading.md` | Маршрутизация загрузки контекста по типу входа |

## 3. Поток данных: голосовое → карточка

```
1. handle_voice(update)
   ├─ is_group_member(user)           # фильтрация по ID / username / name
   ├─ _save_pending_voice(file_id)    # защита от крэша
   └─ _process_voice(bot, file_id, sender, duration, ts)
       ├─ bot.get_file() → download_to_drive()
       ├─ transcribe_via_bukvitsa(audio_path)
       │   └─ telethon_client.send_file() → ждёт ответ (txt или текст)
       ├─ Сохранение .md в transkriptsii/
       ├─ buffer_append(sender, dur_str, text, msg_id)
       └─ _remove_pending_voice(file_id)

2. cmd_push(update)  →  _push_core(reply_fn)
   ├─ buffer_read()
   ├─ build_push_prompt(content)      # контекст проекта + буфер
   ├─ run_claude(prompt, 1800)        # subprocess → npx claude -p
   ├─ buffer_clear(raw_snapshot)      # архивация обработанной части
   ├─ read_pending_tasks()            # [do]/[research] задачи
   ├─ add_tasks_to_sostoyaniye()
   ├─ _do_sync(reply_fn)             # Notion sync
   └─ _autopush()                    # если буфер >= AUTOPUSH_THRESHOLD

3. _do_sync(reply_fn)
   └─ subprocess.Popen(["python", "-u", "update_notion.py"])
       ├─ discover_card_map()         # karta-idej/idei/ → CARD_MAP
       ├─ discover_realizaciya_cards()# realizaciya/ → REALIZACIYA_CARDS
       ├─ update_card()               # карточки мечт
       ├─ sync_realizaciya_gallery()  # галерея исследований
       ├─ update_tasks_on_page()      # блок задач на главной
       └─ send_sync_summary()         # уведомление админу
```

## 4. Команды бота

| Команда | Функция | Таймаут | Что создаёт |
|---------|---------|---------|-------------|
| `/push` | `cmd_push` → `_push_core` | 1800 сек (30 мин) | Карточки, digest, _sostoyaniye, pending_tasks |
| `/research N\|тема` | `cmd_research` | `RESEARCH_TIMEOUT` = 1800 сек | realizaciya/{slug}.md, bot/msg-{slug}.txt |
| `/do N\|тема` | `cmd_do` | `DO_TIMEOUT` = 300 сек (5 мин) | Текстовый ответ, _sostoyaniye |
| `/status` | `cmd_status` | - | - (отображение) |
| `/buffer [текст]` | `cmd_buffer` | - | Показать или добавить текст в буфер |
| `/plan` | `cmd_plan` | - | - (отображение задач [do]/[research]) |
| `/sync` | `cmd_sync` → `_do_sync` | `SYNC_TIMEOUT` = 900 сек | Notion-страницы |
| `/catchup` | `cmd_catchup` | - | Буферизация пропущенных сообщений |
| `/clear` | `cmd_clear` | - | Архив буфера |
| `/chatid` | `handle_chatid` | - | - (диагностика) |

## 5. Callback-кнопки

| `callback_data` | Действие |
|-----------------|----------|
| `stop_sync` | Убить процесс `sync_proc` |
| `edit_research:{slug}` | `pending_edit = {"type": "research", "slug": slug}` → ждёт текст |
| `edit_tasks` | `pending_edit = {"type": "tasks"}` → ждёт текст |
| `approve_tasks` | `add_tasks_to_sostoyaniye(pending)`, удалить `pending_tasks.txt` |
| `skip_tasks` | Удалить `pending_tasks.txt` |
| `ask_send` | Отправить ответ Claude в `GROUP_CHAT_ID` |
| `ask_continue` | `pending_edit = {"type": "ask", ...}` → ждёт уточнение |
| `ask_done` | Сброс `_last_ask` |
| `notion_sync:{slug}` | (кнопки из send_to_admin.py) |
| `notion_skip:{slug}` | (кнопки из send_to_admin.py) |

## 6. Файлы состояния

| Файл | Содержимое | Кто пишет | Кто читает |
|------|-----------|-----------|------------|
| `bot/buffer.md` | Накопитель транскрипций и текстов | `buffer_append`, `buffer_update` | `_push_core`, `cmd_buffer` |
| `bot/buffer_ГГГГММДД_ЧЧММ.md` | Архив обработанного буфера | `buffer_clear` | - |
| `bot/pending_tasks.txt` | Задачи [do]/[research] от Claude | Claude (через build_push_prompt) | `read_pending_tasks`, `cmd_push` |
| `bot/pending_voices.json` | Очередь голосовых до транскрипции | `_save_pending_voice` | `_catchup_pending_voices` |
| `bot/bot.pid` | PID процесса бота | `ensure_single_instance` | `ensure_single_instance` |
| `bot/bot.log` | Лог бота (tee stdout) | `_Tee` | человек |
| `bot/msg-{slug}.txt` | TG-сообщения по исследованию | Claude | `_send_research_to_group` |
| `bot/group_members.txt` | Участники группы | `post_init` | человек (диагностика) |
| `notion/.notion_state.json` | ID страниц, хеши файлов, карточки | `save_state` | `load_state`, бот |
| `notion/.sync.lock` | PID процесса sync | `_acquire_lock` | `_acquire_lock`, `_release_lock` |
| `notion/.sync_progress.json` | Прогресс sync (для трея) | `_write_progress` | трей, бот |
| `notion/.sync_changes.json` | Описания изменений от бота | бот (до sync) | `send_sync_summary` |
| `_sostoyaniye.md` | Текущее состояние проекта | Claude, `add_tasks_to_sostoyaniye` | промпты, хуки |
| `karta-idej/digest.md` | Навигатор по базе знаний | Claude | промпты, хуки |

## 7. Gating Rules (дерево решений)

```
Новая информация
  │
  ├─ Шаг 1: Извлечь сущности (люди, места, идеи, проекты, решения, задачи, факты)
  │
  ├─ Шаг 2: Проверить по katalog.yaml
  │   ├─ Есть, совпадает        → дополнить существующий файл
  │   ├─ Есть, новые детали      → дополнить файл + обновить суть в каталоге
  │   ├─ Нет, понятно что это    → создать файл + katalog.yaml + index.yaml
  │   ├─ Нет, непонятно          → inbox/ (помечено для разбора)
  │   ├─ Противоречит            → НЕ затирать. «Ранее: X. Теперь: Y (дата)»
  │   └─ Дубликат                → не создавать, ссылка на существующий
  │
  ├─ Шаг 3: Обновить связи в katalog.yaml (в обе стороны)
  │
  ├─ Шаг 4: Обновить digest-файлы (≤ 40 строк каждый)
  │
  ├─ Шаг 5: Обновить _sostoyaniye.md (если статус задач изменился)
  │
  └─ Шаг 6: Чеклист — каждый новый файл есть в katalog.yaml и index.yaml
```

## 8. Kontekst Loading (маршрутизация по типу входа)

| Тип входа | Читать | НЕ читать |
|-----------|--------|-----------|
| Ответ на вопросы интервью | _sostoyaniye, digest→нужный, katalog, intervyu/analiz/, gating-rules | все прошлые анализы, все карточки |
| Спонтанное голосовое | _sostoyaniye, digest→нужный, katalog, шаблон | intervyu/, analitika/ |
| Вопрос по собранному | digest→нужный, katalog, файлы из каталога | sostoyaniye, workflow, шаблоны |
| Что спросить дальше? | digest→digest-idei, intervyu/analiz/, workflow интервью | - |
| Конкретная задача/расчёт | realizaciya/index.md, файл задачи, digest-proekty | intervyu/, analitika/, шаблоны |
| Через бот (push) | Контекст уже в промпте | то, что в промпте; realizaciya/ без запроса |
| Техническая задача | bot/, notion/, .env.example | контент-файлы, digest, карточки |

## 9. Claude-промпты

### `build_push_prompt(buffer_content)`
- **Вход:** _sostoyaniye.md, digest.md, katalog.yaml, index.yaml, realizaciya/index.md + буфер
- **Инструкции:** workflow-obrabotki, gating rules, создание карточек в inbox/ и karta-idej/, не трогать realizaciya/, обновить digest и _sostoyaniye
- **Выход:** карточки, обновлённые каталоги, pending_tasks.txt (если задачи)

### `build_research_prompt(task)`
- **Вход:** _sostoyaniye.md, digest.md, katalog.yaml, index.yaml, realizaciya/index.md + задача
- **Инструкции:** workflow-realizaciya, web search, создать realizaciya/{slug}.md + bot/msg-{slug}.txt, обновить index.md, katalog.yaml, _sostoyaniye.md
- **Выход:** файл исследования, msg-файл для группы, обновлённые каталоги

### `cmd_do` (inline prompt)
- **Вход:** _sostoyaniye.md + задача
- **Инструкции:** быстрое выполнение, результат для Telegram (HTML), не создавать файлы в realizaciya/, не делать web search
- **Выход:** текстовый ответ (до 3500 символов)

### `_handle_ask(message, question)`
- **Вход:** _sostoyaniye.md + вопрос
- **Инструкции:** read-only (не создавать/изменять файлы), ответ для Telegram (HTML, до 3500 символов), без web search
- **Выход:** текстовый ответ с кнопками [В группу / Уточнить / Готово]

### `_handle_edit_comment` (type=research)
- **Вход:** realizaciya/{slug}.md + комментарий
- **Инструкции:** workflow-realizaciya, web search, обновить файл + msg-{slug}.txt
- **Выход:** обновлённое исследование → sync → отправка в группу

### `_handle_edit_comment` (type=ask)
- **Вход:** _sostoyaniye.md + исходный вопрос + предыдущий ответ + уточнение
- **Инструкции:** может читать/изменять файлы, ответ для Telegram (HTML)
- **Выход:** текстовый ответ с кнопками [В группу / Уточнить / Готово]

## 10. Дополнительные обработчики

| Обработчик | Фильтр | Действие |
|-----------|--------|----------|
| `handle_group_text` | TEXT/CAPTION из GROUP_CHAT_ID, MESSAGE | `buffer_append` (мин. 20 симв, 10 для пересланных) |
| `handle_edited_group_text` | TEXT/CAPTION из GROUP_CHAT_ID, EDITED_MESSAGE | `buffer_update` по msg_id или `buffer_append` |
| `handle_admin_text` | TEXT, PRIVATE | Алиасы команд → pending_edit → `_handle_ask` (>= 10 симв) |
| `handle_voice` | VOICE/AUDIO/VIDEO_NOTE/Document.AUDIO | `_process_voice` (группа: фильтр по участникам) |
| Пересланные сообщения | `forward_origin is not None` | Порог текста снижен до `MIN_FORWARDED_TEXT` = 10 |

Алиасы в `handle_admin_text`: `push/пуш`, `статус/status/?`, `покажи/буфер/buffer`,
`план/plan/задачи/tasks`, `очисти/clear`, `sync/синх`, `catchup/подхвати`,
`исследуй/research/ресерч`, `сделай/do/ду`, `задачи ок/tasks ok`

## 11. Восстановление после сбоя

### post_init (запуск бота)
1. `telethon_client.start()` — подключение userbot
2. `set_my_commands()` — регистрация меню команд
3. Автообнаружение участников группы (`get_participants` → `GROUP_ALLOWED_IDS/USERNAMES`, `_user_display_names` через Bot API)
4. `BOT_USER_ID` = `bot_me.id` — для фильтрации своих сообщений в catchup
5. `_catchup_pending_voices()` — дотранскрибировать голосовые из `pending_voices.json`
6. `_catchup_group_history()` — подхватить пропущенные сообщения (голосовые + текст, с дедупликацией)
7. `_periodic_catchup()` — фоновая задача каждые `CATCHUP_INTERVAL` = 3600 сек

### ensure_single_instance
- `bot.pid` — если процесс жив, не запускать второй экземпляр; stale PID перезаписывается

### Watchdog (update_notion.py)
- `_watchdog` — поток-демон, убивает процесс через `GLOBAL_TIMEOUT_SEC` = 1080 сек (18 мин)
- Перед смертью: сохраняет state, снимает lock

### Stale lock (update_notion.py)
- `_acquire_lock()` — проверяет `os.kill(old_pid, 0)`, stale lock перехватывается

## 12. Таймауты

| Константа | Значение | Где |
|-----------|----------|-----|
| `RESEARCH_TIMEOUT` | 1800 сек (30 мин) | `cmd_research`, `_handle_edit_comment(research)` |
| `SYNC_TIMEOUT` | 900 сек (15 мин) | `_do_sync` |
| `ASK_TIMEOUT` | 120 сек (2 мин) | `_handle_ask` |
| `DO_TIMEOUT` | 300 сек (5 мин) | `cmd_do` |
| `AUTOPUSH_THRESHOLD` | 15 сообщений | `_push_core` → `_autopush` |
| `CATCHUP_INTERVAL` | 3600 сек (1 час) | `_periodic_catchup` |
| `GLOBAL_TIMEOUT_SEC` | 1080 сек (18 мин) | `_watchdog` (update_notion.py) |
| `run_claude` default timeout | 600 сек (10 мин) | `run_claude` (перезаписывается вызывающим) |
| `_handle_edit_comment(tasks)` | 180 сек | Редактирование задач (model=haiku) |
| Буквица ожидание | 600 сек (10 мин) | `transcribe_via_bukvitsa` (120 итераций x 5 сек) |
| `pending_edit(ask)` TTL | 600 сек (10 мин) | Сброс если уточнение нажато давно |

## 13. Переменные окружения

### `bot/.env`

| Переменная | Описание |
|-----------|----------|
| `BOT_TOKEN` | Токен Telegram-бота |
| `TELETHON_API_ID` | API ID для Telethon (userbot, транскрипция) |
| `TELETHON_API_HASH` | API Hash для Telethon |
| `GROUP_CHAT_ID` | ID группы заказчика |
| `ADMIN_CHAT_ID` | ID чата админа (ЛС) |
| `GROUP_ALLOWED_IDS` | Разрешённые user ID через запятую (необязательно — авто из участников) |

### `notion/.env`

| Переменная | Описание |
|-----------|----------|
| `NOTION_API_TOKEN` | Токен Notion Internal Integration |
| `NOTION_ROOT_PAGE` | UUID корневой страницы Notion |
| `UNSPLASH_ACCESS_KEY` | API-ключ Unsplash (обложки карточек, необязательно) |
| `PERSON_TG_USERNAME` | Telegram-username заказчика (без @) |
| `MAIN_PROJECT_KEYWORD` | Ключевое слово главного проекта (для объединения карточек) |

## 14. Структура папок

```
project/
├── .claude/
│   ├── hooks/
│   │   ├── session-context.sh        # SessionStart — контекст при старте
│   │   ├── protect-files.sh          # PreToolUse — защита .env, realizaciya/, session
│   │   ├── block-manual-sync.sh      # PreToolUse (Bash) — блок ручного запуска sync
│   │   ├── pre-compact.sh            # PreCompact — промпт для продолжения сессии
│   │   └── check-memory-limits.sh    # PostToolUse — проверка лимитов строк
│   ├── rules/                        # Правила для Claude (no-hardcode-notion, security)
│   ├── skills/
│   │   ├── commit/                   # Smart Commit с ревью
│   │   ├── format-research-tg/       # Формат TG-сообщений по исследованиям
│   │   ├── format-voprosov-tg/       # Формат вопросов для заказчика
│   │   ├── project-status/           # Сводка состояния проекта
│   │   ├── proverka-sostoyaniya/     # Проверка бота/sync перед действиями
│   │   ├── workflow-obrabotki/       # Обработка транскрипций
│   │   └── workflow-realizaciya/     # Исследования и бизнес-задачи
│   ├── workflows/
│   │   ├── gating-rules.md           # Правила размещения информации
│   │   └── kontekst-loading.md       # Маршрутизация загрузки контекста
│   └── settings.json                 # Хуки, плагины
├── bot/
│   ├── bot.py                        # Telegram-бот
│   ├── send_to_admin.py              # Отправка в ЛС админу с кнопками
│   ├── send_to_group.py              # Отправка в группу заказчика
│   ├── buffer.md                     # Буфер транскрипций
│   ├── pending_tasks.txt             # Задачи от Claude
│   ├── pending_voices.json           # Очередь голосовых
│   ├── msg-*.txt                     # TG-сообщения по исследованиям
│   └── .env                          # Секреты (не в git)
├── notion/
│   ├── update_notion.py              # Notion sync
│   ├── .notion_state.json            # Состояние (ID, хеши)
│   └── .env                          # Секреты (не в git)
├── karta-idej/                       # digest.md, digest-*.md, zhurnal.md
│   └── idei/, proekty/, resheniya/,  # Карточки по типам
│       gipotezy/, riski/, shagi/, voprosy/
├── realizaciya/                      # Исследования, КП, бизнес-планы (ЗАЩИЩЕНО)
├── intervyu/                         # otvety/ (ответы), analiz/ (анализы)
├── project/                          # Файлы главного проекта
├── kontekst/                         # Профиль заказчика
├── analitika/                        # Критический анализ
├── inbox/                            # Неразобранное (не удалять)
├── shablony/                         # Шаблоны карточек
├── transkriptsii/                    # .md файлы транскрипций
├── _sostoyaniye.md                   # Состояние проекта (≤ 40 строк)
├── katalog.yaml                      # Каталог сущностей
├── index.yaml                        # Карта всех файлов
└── CLAUDE.md                         # Инструкции для AI
```
