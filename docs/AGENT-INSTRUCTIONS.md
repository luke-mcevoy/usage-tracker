# Shared AI tooling: usage tracker + `ai` dispatcher

Canonical instruction block for AI coding agents on this machine. Installed at
`~/.claude/CLAUDE.md` (Claude Code) and `~/.codex/AGENTS.md` (Codex); paste the
same text into Cursor Settings → Rules → User Rules for Cursor agents.

---

- A local dashboard tracks remaining quota on my Claude, Codex, and Cursor
  subscriptions. Live JSON: `GET http://127.0.0.1:8899/api/usage`. If it is
  down, start it with `python3 ~/Develop/Code/usage-tracker/server.py`.
- The `ai` command (on PATH) routes a coding task to whichever vendor CLI
  (claude / codex / cursor-agent) has the most quota left:
  - `ai --status` — per-agent headroom and who would be picked, with reasons
  - `ai -p "task"` — run a task headless on the best agent
  - `ai --agent claude|codex|cursor -p "task"` — force a specific agent
- When I ask you to delegate work, or to use "whichever model has quota",
  dispatch through `ai` instead of picking a CLI yourself.
- Session journal convention: if `SESSION.md` exists in the working directory,
  read it before starting — it holds handoff notes from previous AI sessions,
  possibly written by other models. After completing significant work, append
  a short dated handoff entry: what was done, key decisions, anything
  unresolved.
- Source and docs: https://github.com/luke-mcevoy/usage-tracker
