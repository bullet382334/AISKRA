#!/bin/bash
# Hook: PreCompact — генерирует компактный промпт для продолжения в новой сессии
# Не дублирует _sostoyaniye.md и digest.md — Claude прочитает их сам по CLAUDE.md
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$PROJECT_DIR/.claude/continue-prompt.md"

cd "$PROJECT_DIR"

# Файлы, изменённые с последнего коммита (= работа текущей сессии)
CHANGED=$(git diff --name-only 2>/dev/null | head -15)
UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null | head -10)
LAST_COMMITS=$(git log --oneline -3 2>/dev/null)

{
  echo "Продолжение сессии. Читай _sostoyaniye.md и digest.md — там актуальное состояние."
  echo ""
  if [ -n "$CHANGED" ]; then
    echo "Файлы, изменённые в прошлой сессии:"
    echo "$CHANGED" | sed 's/^/- /'
    echo ""
  fi
  if [ -n "$UNTRACKED" ]; then
    echo "Новые файлы:"
    echo "$UNTRACKED" | sed 's/^/- /'
    echo ""
  fi
  echo "Последние коммиты:"
  echo "$LAST_COMMITS"
  echo ""
  echo "Задача: (допиши что делали)"
} > "$OUT"

python3 -c "
import json, sys
content = open(sys.argv[1], encoding='utf-8').read()
msg = 'Сжатие контекста. Промпт для новой сессии (скопируй после /clear):\n\n' + content
print(json.dumps({'systemMessage': msg}))
" "$OUT"