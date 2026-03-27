#!/bin/bash
# Block direct execution of update_notion.py and send_to_admin.py

INPUT=$(cat)

# Extract command value from JSON input
COMMAND=$(echo "$INPUT" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)

[ -z "$COMMAND" ] && exit 0

# Block python running update_notion.py (but not grep/cat/ast checks)
if echo "$COMMAND" | grep -qE 'python.*update_notion\.py'; then
  if ! echo "$COMMAND" | grep -qE '^(grep|cat|head|tail|wc)'; then
    echo '{"decision":"block","reason":"BLOCKED: do not run sync manually - use /sync in bot"}'
    exit 0
  fi
fi

# Block python running send_to_admin.py
if echo "$COMMAND" | grep -qE 'python.*send_to_admin\.py'; then
  if ! echo "$COMMAND" | grep -qE '^(grep|cat|head|tail|wc)'; then
    echo '{"decision":"block","reason":"BLOCKED: do not run send_to_admin manually - bot handles this"}'
    exit 0
  fi
fi

exit 0