# noob

A terminal dashboard for your Codex and DeepSeek usage.

## What it shows

- Codex 5H / WEEK quota bars (remaining %)
- Cost and tokens per model: today, week, month, all
- Rate-limit reset credits and their expiry
- DeepSeek balance (optional)

## Requirements

- macOS
- `codex` CLI, logged in with `codex login`
- Python 3 (no extra packages)

## Run

```sh
noob          # dashboard, refreshes every 60s
noob --once   # print one snapshot and exit
```

Keys:

| Key | Action |
| --- | --- |
| `s` | Save a usage card PNG to `~/Downloads` |
| `m` | Save a reset-credit card PNG |
| `k` | Toggle the models view |
| `Ctrl-C` | Quit |

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
- `noob_pricing.py` — price tables
- `noob_card.py` — usage card
- `noob_reset_card.py` — reset card
- `subscription_info.json` — renewal date

## Sources

- https://learn.chatgpt.com/docs/app-server
- https://developers.openai.com/api/docs/pricing
- https://api-docs.deepseek.com/quick_start/pricing
