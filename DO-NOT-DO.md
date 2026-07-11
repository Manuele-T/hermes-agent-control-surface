# DO-NOT-DO.md — things Claude must never do in this project

A living list of "looks helpful but isn't" patterns, grounded in real mistakes
made during this build. Update this whenever something goes wrong that shouldn't
have. Reference it in prompts with `see DO-NOT-DO.md`.

---

## Browser / OS

- **Never close, kill, or interfere with browser tabs or windows.** The user
  may have Edge/Chrome sessions open with active work, auth sessions, or
  unsaved state. Screenshot or observation tools must be read-only. Never
  call any browser close/kill API or keyboard shortcut that closes tabs.
- **Never assume a port is safe to kill without checking first.** Run
  `lsof -i :<port>` or `pgrep -af uvicorn` to confirm what's actually
  running before killing anything. Kill by PID, not by broad pattern.

---

## WSL / process management

- **Never use `pkill -f` with a broad pattern.** `pkill -f python` kills
  every Python process on the machine, including Hermes itself. Always use
  the most specific pattern possible: `pkill -f "uvicorn app.main"`, or
  resolve a PID first and kill that.
- **Never run backend or npm commands from Windows.** Both must run from
  inside WSL (Ubuntu). Running from Windows resolves `~` to
  `C:\Users\manue` instead of `/home/manue`, breaking every Hermes path.
  The app's own processes are unaffected (they run in WSL already), but
  any verification or diagnostic commands must also be in WSL.
- **Never use `bash -lc` for commands that need node/npm/nvm.** Ubuntu's
  `.bashrc` returns early for non-interactive shells, so nvm is never
  loaded and npm silently resolves to a Windows binary via WSL interop.
  Use `bash -ic` (interactive) instead. This produces a
  `'tsc' is not recognized` Windows-style error when it goes wrong.
- **Never start a long-running process (uvicorn, npm dev) with `&` /
  `nohup` / `disown` inside a `wsl.exe` call.** The process dies when
  the `wsl.exe` invocation returns, regardless of those flags. Use the
  tool's own background-task mechanism to hold the call open, or instruct
  the user to run it from an interactive WSL terminal they keep open.
- **Never pass `$VAR` through the `wsl.exe` hop from Windows.** Shell
  variables are silently stripped. Even within a single WSL shell,
  `BID=$(...); ... $BID` capture is unreliable for task IDs. Always inline
  literal values; read task IDs back with `hermes kanban list` by eye.

---

## Node / npm

- **Never run `npm install` on Windows if the project will run from WSL
  (or vice versa).** `node_modules` contains OS-specific native binaries
  (rollup, esbuild). A mismatch produces
  `Cannot find module @rollup/rollup-linux-x64-gnu`. Fix:
  `rm -rf node_modules package-lock.json && npm install` from inside WSL,
  then always use WSL for both install and run going forward.
- **Never add npm dependencies to solve a pure CSS/SVG problem.** The
  frontend visual layer must stay dependency-free unless a new dep is
  explicitly approved. No new `package.json` entries without discussion.

---

## Secrets / files

- **Never read, print, or log `~/.hermes/auth.json`.** It contains the
  OpenRouter API key (LLM provider credentials), not the dashboard token.
  It must never appear in output, commits, or logs.
- **Never commit `.env` or any file matching `.env.*` (except
  `.env.example`).** The `.gitignore` already excludes these; never
  override it.
- **Never hardcode `/home/manue`.** Always use `~` so the path is
  portable across machines and WSL usernames.
- **Never write `HERMES_DASHBOARD_SESSION_TOKEN` to any tracked file.**
  Config-only in `.env` (gitignored).

---

## Hermes / Kanban data

- **Never write directly to the SQLite DB.** All writes go via
  `POST /api/plugins/kanban/...` (Bearer token) or the
  `hermes kanban <verb>` CLI. Direct DB writes bypass event logging and
  break the event-driven state machine.
- **Never set `status=running` on a task via the API.** The server rejects
  it with HTTP 400. Always create a task and let the dispatcher/gateway
  claim it.
- **Never treat the kanban REST API as unauthenticated.** Since v0.17 all
  `/api/plugins/kanban/` routes require `Authorization: Bearer <token>`.
  `?token=` in the query string only works for the WebSocket `/events`
  endpoint, not HTTP routes.
- **Never read `~/.hermes/auth.json` for the dashboard token.** That file
  is the LLM provider key (OpenRouter). The dashboard token comes from
  `HERMES_DASHBOARD_SESSION_TOKEN` in `.env`.

---

## Frontend / UI

- **Never rename or remove the CSS state classes** (`state-idle`,
  `state-working`, `state-done`, `state-blocked`). These are the contract
  between the state logic in `fleet.ts` and the visual layer in
  `styles.css` / `Character.tsx`. Renaming them breaks state-driven
  animations silently.
- **Never touch `fleet.ts`, `datasource.ts`, or `App.tsx` in a visual
  overhaul step.** Visual changes are isolated to `Character.tsx`,
  `Room.tsx`, and `styles.css` only.
- **Never let a React component import directly from the backend or call
  Hermes APIs.** All data access goes through the `DataSource` interface.
  This is what keeps v1 → v2 additive instead of a rewrite.