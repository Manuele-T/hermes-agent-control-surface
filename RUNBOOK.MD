# Terminal 1 — dashboard
cd ~/Documents/Coding/Hermes/hermes-agent-control-surface
export $(grep -v '^#' .env | grep HERMES_DASHBOARD_SESSION_TOKEN)
hermes dashboard --no-open

# Terminal 2 — backend
cd ~/Documents/Coding/Hermes/hermes-agent-control-surface/backend
source .venv/bin/activate
uvicorn app.main:app --port 8123 --reload

# Terminal 3 — frontend
cd ~/Documents/Coding/Hermes/hermes-agent-control-surface/frontend
npm run dev

# Terminal 4 — gateway
tmux new -s gateway 'hermes -p autonomous-builder gateway run'
# detach: Ctrl+B then D — reattach: tmux attach -t gateway
```

Open `http://localhost:5173`.

```bash
# Spawn work
hermes -p autonomous-builder kanban create \
  "Research how transformer attention mechanisms work. Cover: what a token is, what Q/K/V matrices do, why scaled dot-product attention avoids vanishing gradients, and how multi-head attention differs from single-head. Summarise in 600 words with a concrete analogy for each concept." \
  --assignee researcher

hermes -p autonomous-builder kanban create \
  "Write a 700-word blog post introduction explaining why local AI agents are becoming viable in 2026. Cover: model size reduction, hardware improvements, and privacy advantages over cloud APIs. Tone: technical but accessible. End with a hook sentence for the next section." \
  --assignee writer

hermes -p autonomous-builder kanban create \
  "Review the following claim: 'Running AI agents locally is now cheaper than using cloud APIs for most developer workloads.' Research current pricing for Claude API, GPT-4o, and a typical local DeepSeek setup on consumer hardware. Produce a verdict with numbers." \
  --assignee reviewer

hermes -p autonomous-builder kanban dispatch  # skip if gateway already running
```

```bash
# Sanity check
curl -s 127.0.0.1:8123/health
# Expect "adapter":"KanbanAdapter" and kanban_db_path pointing to ~/.hermes/kanban.db

# Clear the board
hermes -p autonomous-builder kanban list
hermes -p autonomous-builder kanban archive <id>

# Stale backend after code changes
pkill -f "uvicorn.app[.]main"
cd ~/Documents/Coding/Hermes/hermes-agent-control-surface/backend
source .venv/bin/activate
uvicorn app.main:app --port 8123 --reload

# Simulated data (no Hermes needed)
cd ~/Documents/Coding/Hermes/hermes-agent-control-surface
HERMES_DATA_SOURCE=synthetic ./run.sh
# Open http://127.0.0.1:8124
```