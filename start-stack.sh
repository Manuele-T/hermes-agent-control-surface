#!/usr/bin/env bash
set -e

REPO=~/Documents/Coding/Hermes/hermes-agent-control-surface

# Dashboard
tmux new-session -d -s hermes-stack -n dashboard \
  "cd $REPO && export \$(grep -v '^#' .env | grep HERMES_DASHBOARD_SESSION_TOKEN) && hermes dashboard --no-open"

# Backend
tmux new-window -t hermes-stack -n backend \
  "cd $REPO/backend && source .venv/bin/activate && uvicorn app.main:app --port 8123 --reload"

# Frontend
tmux new-window -t hermes-stack -n frontend \
  "cd $REPO/frontend && npm run dev"

# Gateway — separate session, own name, matches existing kill/attach commands
if tmux has-session -t gateway 2>/dev/null; then
  echo "gateway session already running, leaving as-is"
else
  tmux new-session -d -s gateway "hermes -p autonomous-builder gateway run"
fi

echo "Stack up."
echo "  tmux attach -t hermes-stack   (Ctrl+B, w to switch windows; Ctrl+B, d to detach)"
echo "  tmux attach -t gateway"

# Spawn work
cd "$REPO"
sleep 3   # let gateway finish booting before dispatch

hermes -p autonomous-builder kanban create \
  "Research how transformer attention mechanisms work. Cover: what a token is, what Q/K/V matrices do, why scaled dot-product attention avoids vanishing gradients, and how multi-head attention differs from single-head. Summarise in 600 words with a concrete analogy for each concept." \
  --assignee researcher

hermes -p autonomous-builder kanban create \
  "Write a 700-word blog post introduction explaining why local AI agents are becoming viable in 2026. Cover: model size reduction, hardware improvements, and privacy advantages over cloud APIs. Tone: technical but accessible. End with a hook sentence for the next section." \
  --assignee writer

hermes -p autonomous-builder kanban create \
  "Review the following claim: 'Running AI agents locally is now cheaper than using cloud APIs for most developer workloads.' Research current pricing for Claude API, GPT-4o, and a typical local DeepSeek setup on consumer hardware. Produce a verdict with numbers." \
  --assignee reviewer

echo "3 tasks dispatched (researcher, writer, reviewer)."

# open http://localhost:5173 to see the dashboard
#tmux kill-session -t hermes-stack; tmux kill-session -t gateway       - to close al terminals