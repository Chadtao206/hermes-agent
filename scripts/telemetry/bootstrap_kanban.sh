#!/bin/bash
set -euo pipefail

BOARD_SLUG="${1:-default}"
DISPLAY_NAME="${2:-Self Improvement}"

if ! command -v hermes >/dev/null 2>&1; then
  echo "hermes CLI not found" >&2
  exit 1
fi

CURRENT_OUTPUT="$(hermes kanban boards show 2>/dev/null || true)"
LIST_OUTPUT="$(hermes kanban boards list 2>/dev/null || true)"

if printf '%s
' "$LIST_OUTPUT" | grep -q "^[[:space:]]*[●[:space:]]*[[:space:]]*$BOARD_SLUG[[:space:]]"; then
  echo "Board '$BOARD_SLUG' already exists."
else
  if [ "$BOARD_SLUG" = "default" ]; then
    hermes kanban init >/dev/null
    echo "Initialized default board."
  else
    hermes kanban boards create "$BOARD_SLUG" --name "$DISPLAY_NAME" >/dev/null
    echo "Created board '$BOARD_SLUG' ($DISPLAY_NAME)."
  fi
fi

hermes kanban boards switch "$BOARD_SLUG" >/dev/null

echo "Active board:"
hermes kanban boards show

echo
echo "Board stats:"
hermes kanban --board "$BOARD_SLUG" stats || true

echo
echo "Quick start:"
echo "  hermes kanban create --title 'Example task' --assignee engineer"
echo "  hermes kanban list"
echo "  hermes kanban show <task-id>"
