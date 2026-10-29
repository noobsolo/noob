# noob

A terminal dashboard for your Codex and DeepSeek usage — quota bars, spend by model, reset credits, and shareable cards, without leaving the shell.

![noob dashboard](assets/dashboard.png)

## What it shows

- Codex 5H / WEEK quota bars (remaining %)
- Cost per model: today, week, month, all (press `k`)
- Token totals: today, week, month, all
- Rate-limit reset credits and their expiry
- DeepSeek balance (optional)

Per-model breakdown with `k`:

![noob models view](assets/models-view.png)

## Cards

Press `s` to save a usage card, `m` for reset credits, or open the models view with `k` and press `s` for the model-cost card. All are written to `~/Downloads` as PNGs.

<p align="center">
  <a href="assets/card-usage.png"><img src="assets/card-usage.png" width="46%" alt="noob usage card"></a>
  &nbsp;&nbsp;
  <a href="assets/card-reset.png"><img src="assets/card-reset.png" width="46%" alt="noob reset-credit card"></a>
</p>
<p align="center"><sub>Usage card with <code>s</code> · reset-credit card with <code>m</code></sub></p>

<p align="center">
  <a href="assets/models-card.png"><img src="assets/models-card.png" width="46%" alt="noob model cost card"></a>
</p>
<p align="center"><sub>Model cost card with <code>k</code> → <code>s</code></sub></p>

## Requirements

- macOS
- `codex` CLI, logged in with `codex login`
- Python 3 (no extra packages). Compressed Codex rollouts (`*.jsonl.zst`) need
  Python 3.14+, the `zstandard` package, or the `zstd` command; without one of
  them those older sessions are skipped.

## Run

```sh
noob          # dashboard, refreshes every 60s
noob --once   # print one snapshot and exit
```

Keys:

| Key | Action |
| --- | --- |
| `s` | Save a card for the current view (usage, or model costs in the models view) |
| `m` | Save a reset-credit card PNG (home view) |
| `k` | Toggle the models view |
| `t` / `w` / `m` / `a` | Models view: sort by today / week / month / all; press the same key again to flip ascending |
| `Ctrl-C` | Quit |

Sorting is keyboard-only; `m` means month inside the models view, while on the
home view it still saves the reset-credit card.

## Auto-update

`noob` checks its own checkout at launch and fast-forwards to the upstream
branch, then restarts into the new code, so new dashboard features show up
without a manual `git pull`.

It only updates a clean checkout that is strictly behind its upstream branch.
Local commits, uncommitted tracked changes, a detached HEAD, a missing upstream,
or an offline machine are left untouched, and a lock keeps two noob instances
from updating at the same time.

```sh
noob --update      # check and install an update now, then exit
noob --no-update   # skip the start-up check
```

Set `NOOB_NO_UPDATE=1` to disable the start-up check from scripts or launchd.

## Versioning

`VERSION` in `noob.py` is the single source of truth and is shown in dim gray at
the bottom-right of the dashboard (`v0.2.0`). Releases follow `0.x.0`: every
change bumps the middle number unless the request says otherwise, so the next
versions are `0.3.0`, `0.4.0`, and so on.

When a launch-time update installs a new version, the badge replaces the old
`noob updated: …` line and turns green for that session, so you can tell at a
glance that you are now running the freshly pulled code.


## Local usage history

Costs are not recomputed from the raw logs every launch anymore. `noob` records
each new token increment in a local append-only ledger, together with the rate
snapshot that was in effect at that moment:

```
usage/events/YYYY-MM.jsonl
usage/state.json
```

The folder sits next to `noob.py` and is gitignored, so every checkout keeps its
own private history. Only the parsing, pricing, and rendering logic lives in the
repository.

A later price change therefore never rewrites an older cost. The ledger also
keeps history after Codex compresses, archives, or deletes a session, and after
OpenCode/pi clean up their logs. The first run backfills the logs that still
exist; those older days are priced with the prices available at backfill time.

The dashboard is unchanged: it aggregates the ledger into the same day/week/
month/all figures and model list as before.

```sh
noob --capture-usage   # record new usage, then exit
```

Nightly `--daily-cards` also records usage, so the schedule created by
`--install-daily` keeps the history current even when the dashboard is closed.

## Nightly cards

Capture the home usage card and the models detail card every night at midnight,
into `~/Downloads/noob-daily/`:

```sh
noob --install-daily          # every night at 00:00
noob --install-daily 23:50    # or another time
noob --uninstall-daily        # remove the schedule
noob --daily-cards            # capture right now
```

Files are named `noobsolo-usage-YYYYMMDD.png` and `noobsolo-models-YYYYMMDD.png`,
so re-running on the same day replaces that day's pair instead of piling up
duplicates. Pass `--daily-dir DIR` (on its own or together with
`--install-daily`) to archive somewhere other than Downloads.
A dashboard left open across midnight captures on its own too.

The schedule is a launchd agent at `~/Library/LaunchAgents/com.noob.dailycards.plist`;
its output goes to `~/Library/Logs/noob-daily.log`. If the Mac is asleep at
midnight, launchd runs the capture once it wakes. The agent reuses the Python
interpreter that ran `--install-daily`, so it keeps that interpreter's
decompression support instead of falling back to the system Python.

## Optional

DeepSeek balance — store the key once in the macOS Keychain:

```sh
security add-generic-password -s noob-deepseek -a "$USER" -w
```

Or set `DEEPSEEK_API_KEY` in your shell. The env var wins.

OpenAI API bill — set `OPENAI_ADMIN_KEY`, then run `noob --api`.

## Where the numbers come from

- Codex quota: the Codex app-server
- Codex usage: `~/.codex/sessions` logs
- Prices: OpenAI's pricing page (cached in `pricing_cache.json`)
- DeepSeek usage: local `opencode` and `pi` logs, priced with DeepSeek's CNY table
- Renewal date: `subscription_info.json`

Costs are API-price equivalents for your tokens, not what you were actually billed.

## Files

- `noob.py` — main dashboard
- `noob_usage.py` — local priced-usage ledger
- `noob_pricing.py` — price tables
- `noob_card.py` — usage card
- `noob_reset_card.py` — reset card
- `subscription_info.json` — renewal date

## Sources

- https://learn.chatgpt.com/docs/app-server
- https://developers.openai.com/api/docs/pricing
- https://api-docs.deepseek.com/quick_start/pricing
