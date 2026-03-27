#!/bin/bash
# Hook: PostToolUse (Edit|Write) — предупреждает если файлы превышают лимиты
# Не блокирует (exit 0), только предупреждает

FILE_PATH="$TOOL_INPUT_FILE_PATH"
if [ -z "$FILE_PATH" ]; then
    exit 0
fi

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
WARNINGS=""

check_limit() {
    local file="$1"
    local limit="$2"
    local name="$3"
    if [ -f "$file" ]; then
        local lines
        lines=$(wc -l < "$file")
        if [ "$lines" -gt "$limit" ]; then
            WARNINGS="${WARNINGS}⚠ $name: $lines строк (лимит: $limit)\n"
        fi
    fi
}

# Проверяем ключевые файлы после каждой записи
check_limit "$PROJECT_DIR/_sostoyaniye.md" 40 "_sostoyaniye.md"
check_limit "$PROJECT_DIR/karta-idej/digest.md" 25 "digest.md"
check_limit "$PROJECT_DIR/CLAUDE.md" 80 "CLAUDE.md"

# Memory файлы
MEMORY_DIR="$HOME/.claude/projects/$(echo "$PROJECT_DIR" | tr '/:' '-' | sed 's/^-*//')/memory"
if [ -d "$MEMORY_DIR" ]; then
    for f in "$MEMORY_DIR"/*.md; do
        [ -f "$f" ] || continue
        fname=$(basename "$f")
        if [ "$fname" = "MEMORY.md" ]; then
            check_limit "$f" 50 "memory/MEMORY.md"
        else
            check_limit "$f" 30 "memory/$fname"
        fi
    done
fi

if [ -n "$WARNINGS" ]; then
    echo -e "$WARNINGS"
fi

exit 0