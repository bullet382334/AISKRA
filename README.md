# AISKRA — AI-Powered Idea Collection & Development System

Ready-to-deploy template: **Telegram bot + Claude AI + Notion**.

A person sends voice messages → bot transcribes → Claude creates idea cards, finds patterns, generates follow-up questions → Notion dashboard with covers and galleries.

```
Telegram group → Bot → Buffer → Claude → Cards / Research → Notion
       ↑                                        ↓
    Person                               Admin (notifications)
```

## Features

- **Voice transcription** via [Bukvitsa](https://bykvitsa.ru/) (or Whisper API)
- **Automatic processing**: transcriptions → idea cards, hypotheses, decisions, risks
- **Adaptive interviews**: AI analyzes answers and generates next questions
- **Research**: `/research topic` → Claude does web search, creates report → Notion
- **Notion sync**: cards with covers, galleries, task tracking
- **Analytics**: hypothesis testing framework, prioritization criteria, checklists

## Quick Start

```bash
git clone https://github.com/bullet382334/AISKRA.git
cd AISKRA
cp secrets.txt.example secrets.txt   # fill in tokens
python setup.py                       # auto-configure
cd bot && python bot.py               # run
```

Full setup guide (step-by-step, with screenshots descriptions) → [SETUP.md](SETUP.md)

## Requirements

- Python 3.10+
- Node.js (for `npx claude`)
- [Claude Code](https://claude.ai/claude-code) (Anthropic Max/Pro subscription)
- Telegram account
- Notion account (free tier works)

## Documentation

- [SETUP.md](SETUP.md) — full deployment guide (RU)
- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture, data flows, functions (RU)
- [CLAUDE.md](CLAUDE.md) — AI instructions (RU)

---

# RU: Подробное описание

## Что это

AI-система для сбора и развития идей одного человека. Заказчик говорит голосовые в Telegram-группу, бот транскрибирует, Claude обрабатывает — создаёт карточки идей, находит паттерны, генерирует вопросы, проводит исследования. Всё синхронизируется в Notion.

## Команды бота

| Команда | Что делает |
|---------|-----------|
| `/push` | Отправить буфер транскрипций Claude для обработки |
| `/research тема` | Исследование по теме (веб-поиск, отчёт, Notion) |
| `/do задача` | Быстрая задача через Claude |
| `/sync` | Синхронизировать карточки в Notion |
| `/status` | Статус буфера, Claude, Notion |
| `/buffer` | Показать содержимое буфера |
| `/plan` | Текущий план задач |
| `/catchup` | Проверить пропущенные сообщения |
| `/clear` | Очистить буфер |

## Структура проекта

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
├── svodki/           ← Сводки
├── SETUP.md          ← Пошаговая инструкция
├── ARCHITECTURE.md   ← Полная архитектура (305 строк)
└── setup.py          ← Автоматизация настройки
```

## Транскрипция

По умолчанию — через [Буквицу](https://bykvitsa.ru/) ([@BukvitsaAI_bot](https://t.me/BukvitsaAI_bot), ~8 т.р./год, отличное качество для русского). Альтернативы:
- **OpenAI Whisper API** (~$0.006/мин) — заменить одну функцию в bot.py
- **Локальный Whisper** (бесплатно, нужен GPU)

Подробности в [SETUP.md](SETUP.md).

## Создать токены (~15 минут)

| Что | Где | Что получишь |
|-----|-----|-------------|
| Telegram-бот | [@BotFather](https://t.me/BotFather) | BOT_TOKEN |
| Telegram-группа | Telegram | GROUP_CHAT_ID (setup.py получит сам) |
| Telethon API | [my.telegram.org](https://my.telegram.org/) | API_ID, API_HASH |
| Notion интеграция | [notion.so/my-integrations](https://www.notion.so/my-integrations) | NOTION_API_TOKEN |
| Unsplash (опц.) | [unsplash.com/developers](https://unsplash.com/developers) | UNSPLASH_ACCESS_KEY |

## Лицензия

MIT
