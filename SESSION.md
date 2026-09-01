# Session journal

- 2026-08-26 21:34 — dispatched to gemini: Add a live 'who is working on what' view to the dispatcher dashboard. Problem: when several agent CL
- 2026-08-26 21:36 — dispatched to cursor: Add a live 'who is working on what' view to the dispatcher dashboard. Problem: when several agent CL

## 2026-08-26 — Live "Now working" panel (cursor)

Shipped the in-flight dispatch view end-to-end. Every launch now writes two
JSONL events instead of one — a `start` event with `run_id` + `pid` before the
child runs, and an `end` event with `exit_code` + `duration_s` after it exits.
The pair is written from `dispatcher/launch.py`'s `launch()` via
`log_dispatch_start` / `log_dispatch_end`; the end is inside a `try/finally` so
a Ctrl+C still closes out the run. Old single-line records without `event` /
`run_id` are treated as `legacy` and still render in history unchanged (the
production log at `~/.cache/usage-tracker/dispatches.jsonl` was verified to
parse fine against the new reader).

`read_dispatch_log(limit)` now returns `(records, active)`. Active =
start-with-no-end whose `pid` is alive via `os.kill(pid, 0)`. Dead pid +
headless = `interrupted` (crashed dispatcher); dead pid + interactive =
`closed` (execvp'd process exited normally — we cannot write an `end` from a
replaced image, which is the trade-off for keeping the same pid so liveness
probing works at all). `server.py` returns `{records, active, stats,
server_time}` from `/api/dispatches`; combined `records + active` feed both
stats and `daily_series` so counts don't drop while a run is in flight.

Dashboard: new "Now working" card at the top with agent pill, repo (basename
of cwd), task, live-ticking elapsed (client computes from `started_at` +
`server_time` skew, re-rendered every 1s), and a "running" status tag. A 3s
poll refreshes just `/api/dispatches` so the panel updates promptly without
re-hitting provider endpoints. When empty, the card shows a subdued "nothing
dispatched right now" line rather than disappearing (feels less flickery).
The history table below reuses the new `dispatchResult()` helper which also
renders `interrupted` as a yellow tag.

Bonus: `ai --ps` prints the same active list on the terminal — one line per
run with agent, repo, elapsed, pid, mode, and the task snippet.

Test-wise: added `tests/test_dispatch_log.py` (14 tests) covering pairing,
alive/dead pid, interactive-closed vs headless-interrupted, legacy-only,
mixed logs, malformed lines, sort order, and the write helpers. `python3 -m
unittest discover tests` → 74/74 green. Manual smoke: parsed the real prod
log, ran a synthetic headless launch with `/bin/true` (start+end both written
with matching `run_id`, `duration_s ≈ 0.004`), and confirmed the panel via
`/api/dispatches` on a temp port. As I was finishing, another dispatch
(language-globe/cursor from a separate session) was launched under the new
code and immediately appeared as `active` — best kind of validation.

Worth watching: (a) if the user has a running `server.py` from before this
change (there is one, pid 13309), it will keep serving the old JS/handler
until restarted — a fresh `python3 server.py` is needed to see the panel; (b)
pid reuse is theoretically possible but ignored — dispatches are short-lived
and locally owned, so a recycled pid falsely reading as "alive" is very
unlikely; (c) an interactive dispatch that is still open will show as
`running` in the panel indefinitely, which is correct but may surprise users
who don't realise their cursor-agent window is still holding the pid.
