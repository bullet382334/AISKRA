#!/bin/bash
# Hook: SessionStart — показывает текущее состояние проекта при старте сессии
# Гарантирует, что AI всегда начинает с актуального контекста

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo "=== КОНТЕКСТ СЕССИИ ==="
echo ""

# Состояние проекта
if [ -f "$PROJECT_DIR/_sostoyaniye.md" ]; then
    echo "--- _sostoyaniye.md ---"
    cat "$PROJECT_DIR/_sostoyaniye.md"
    echo ""
fi

# Сжатый digest
if [ -f "$PROJECT_DIR/karta-idej/digest.md" ]; then
    LINES=$(wc -l < "$PROJECT_DIR/karta-idej/digest.md")
    echo "--- digest.md ($LINES строк, показаны первые 50) ---"
    head -50 "$PROJECT_DIR/karta-idej/digest.md"
    if [ "$LINES" -gt 50 ]; then
        echo "... (ещё $((LINES - 50)) строк — читай полный файл при необходимости)"
    fi
    echo ""
fi

# Git статус + что изменилось
echo "--- git ---"
cd "$PROJECT_DIR"
BRANCH=$(git branch --show-current 2>/dev/null)
UNCOMMITTED=$(git status --short 2>/dev/null | wc -l)
echo "Ветка: $BRANCH | Незакоммичено: $UNCOMMITTED"
echo ""
echo "--- последние изменения ---"
git log --oneline --name-only -3 2>/dev/null
echo ""
echo "=== КОНЕЦ КОНТЕКСТА ==="