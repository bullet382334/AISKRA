# AISKRA — AI-система сбора и развития идей

Готовый шаблон: Telegram-бот + Claude AI + Notion.

Человек говорит голосовые → бот транскрибирует → Claude создаёт карточки, находит паттерны, генерирует вопросы → Notion-дашборд.

```
Telegram-группа → Бот → Буфер → Claude → Карточки/Исследования → Notion
       ↑                                          ↓
  Заказчик                                 Админ (уведомления)
```

---

## Что умеет

- **Транскрипция** голосовых через [Буквицу](https://bykvitsa.ru/) (или Whisper API)
- **Автоматическая обработка**: транскрипции → карточки идей, гипотез, решений, рисков
- **Адаптивное интервью**: AI анализирует ответы и генерирует следующие вопросы
- **Исследования**: `/research тема` → Claude делает веб-поиск, создаёт отчёт
- **Notion-синхронизация**: карточки с обложками, галереи, задачи
- **Аналитика**: рамка проверки гипотез, критерии приоритизации, чеклисты

## Команды бота

| Команда | Что делает |
|---------|-----------|
| `/push` | Отправить буфер транскрипций Claude для обработки |
| `/research тема` | Исследование по теме (веб-поиск, отчёт, Notion) |
| `/do задача` | Быстрая задача через Claude |
| `/sync` | Синхронизировать карточки в Notion |
| `/status` | Статус буфера, Claude, Notion |
| `/plan` | Текущий план задач |

---

## Быстрый старт

### 1. Склонировать

```bash
git clone https://github.com/bullet382334/AISKRA.git
cd AISKRA
```

### 2. Создать токены (руками, ~15 минут)

| Что | Где |
|-----|-----|
| Telegram-бот | [@BotFather](https://t.me/BotFather) |
| Telegram-группа | Telegram |
| Telethon API | [my.telegram.org](https://my.telegram.org/) |
| Notion интеграция | [notion.so/my-integrations](https://www.notion.so/my-integrations) |
| Unsplash (опционально) | [unsplash.com/developers](https://unsplash.com/developers) |

### 3. Заполнить secrets.txt

```bash
cp secrets.txt.example secrets.txt
# Заполнить токенами
```

### 4. Запустить setup

```bash
python setup.py
```

Скрипт: проверит Python, установит зависимости, раскидает токены в `.env`, получит chat ID, проверит все токены.

### 5. Запустить бота

```bash
cd bot && python bot.py
```

Подробнее → [SETUP.md](SETUP.md)

---

## Структура

```
AISKRA/
├── bot/              ← Telegram-бот (транскрипция, буфер, Claude)
├── notion/           ← Синхронизация с Notion
├── .claude/          ← AI-конфигурация (skills, workflows, hooks, rules)
├── karta-idej/       ← Карта идей (digest, журнал, карточки)
├── realizaciya/      ← Исследования и расчёты
├── intervyu/         ← Адаптивное интервью
├── kontekst/         ← Профиль заказчика, участники, ресурсы
├── analitika/        ← Инструменты проверки гипотез
├── pravila/          ← Workflow, принципы, протоколы
├── shablony/         ← Шаблоны карточек (11 шт.)
├── project/          ← Главный проект (заполняется по ходу)
├── inbox/            ← Входящие (необработанное)
├── transkriptsii/    ← Файлы транскрипций
├── svodki/           ← Сводки для семьи
├── SETUP.md          ← Пошаговая инструкция
├── ARCHITECTURE.md   ← Полная архитектура (305 строк)
└── setup.py          ← Автоматизация настройки
```

---

## Требования

- Python 3.10+
- Node.js (для `npx claude`)
- [Claude Code](https://claude.ai/claude-code) (подписка Anthropic Max/Pro)
- Telegram-аккаунт
- Notion-аккаунт (бесплатный)

## Транскрипция

По умолчанию — через [Буквицу](https://bykvitsa.ru/) ([@BukvitsaAI_bot](https://t.me/BukvitsaAI_bot), ~8 т.р./год). Альтернативы: OpenAI Whisper API, локальный Whisper. Подробности в [SETUP.md](SETUP.md).

## Документация

- [SETUP.md](SETUP.md) — полная инструкция по развёртыванию
- [ARCHITECTURE.md](ARCHITECTURE.md) — архитектура системы, потоки данных, функции
- [CLAUDE.md](CLAUDE.md) — инструкции для AI
