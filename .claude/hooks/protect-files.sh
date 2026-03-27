#!/bin/bash
# Hook: PreToolUse (Edit|Write) — блокирует запись в защищённые файлы
# exit 2 = жёсткая блокировка

FILE_PATH="$TOOL_INPUT_FILE_PATH"
if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# Защищённые паттерны
case "$FILE_PATH" in
    *.env)
        echo "BLOCKED: запись в .env запрещена. Секреты редактируются вручную."
        exit 2
        ;;
    *.session|*.session-journal)
        echo "BLOCKED: запись в session-файлы запрещена."
        exit 2
        ;;
    */.claude/settings.local.json)
        echo "BLOCKED: settings.local.json редактируется вручную или через /update-config."
        exit 2
        ;;
    */realizaciya/*)
        echo "BLOCKED: realizaciya/ защищена. Менять только по прямому запросу пользователя."
        exit 2
        ;;
esac

exit 0