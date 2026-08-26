# AI Usage Tracker

Local dashboard showing how much usage you have left on your **Claude**,
**Codex**, and **Cursor** accounts. Zero dependencies (Python 3 stdlib only),
everything runs on your machine, and no tokens ever leave it.

## Run

```bash
python3 server.py
```

Then open <http://127.0.0.1:8899>. The page auto-refreshes every minute;
the Refresh button forces a re-fetch.

## How each service is read

| Service | Data source | Freshness |
|---------|-------------|-----------|
| Claude  | Statusline hook snapshot (`~/.claude/usage-snapshot.json`), with the OAuth usage endpoint as backup | Live while you use Claude Code |
| Codex   | `codex app-server` JSON-RPC (`account/rateLimits/read`) | Live |
| Cursor  | Access token from Cursor's local state DB → dashboard API | Live |

### Claude details

Anthropic's `/api/oauth/usage` endpoint rate-limits aggressively (~5 calls per
access token), so the primary source is a Claude Code **statusline hook**
(`claude_statusline.py`, wired into `~/.claude/settings.json`). Claude Code
pipes rate-limit data to it after every assistant message; the hook saves a
snapshot the dashboard reads. The OAuth endpoint is still tried at most every
15 minutes (1-hour backoff after a 429) so data can refresh even when Claude
Code is idle.

Consequence: right after setup the Claude card may say "no usage data yet" —
run any Claude Code prompt once and it will populate.

### Cursor details

The access token is read (read-only) from
`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` and used
against the same endpoints the cursor.com dashboard uses. If Cursor changes
its auth, re-login in Cursor and the token refreshes automatically.

### Codex details

Spawns `codex app-server` and asks over JSON-RPC. Requires the `codex` CLI to
be logged in (`codex login`).

## Caveats

- All three data sources are unofficial/undocumented and may break when
  vendors change their APIs.
- Cached state lives in `~/.cache/usage-tracker/`.
- The server binds to 127.0.0.1 only.
