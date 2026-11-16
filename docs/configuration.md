# Configuration

SellerDesk reads environment variables. With Docker, put them in `.env`.

## Eldorado

| Variable | Default | Meaning |
|---|---|---|
| `ELDORADO_CLIENT_ID` | | Required |
| `ELDORADO_CLIENT_SECRET` | | Required |
| `ELDORADO_BASE_URL` | `https://www.eldorado.gg` | API address |

## Timing

| Variable | Default | Meaning |
|---|---|---|
| `POLL_SECONDS` | `30` | Time between checks. This sets how late an alarm can start, and it caps how fast repeats go |
| `ALARM_REPEAT_SECONDS` | `60` | Repeat rate in the calm stage. Hurry runs at half this, critical and late at a quarter |
| `ALARM_STAGE_BUDGET_MINUTES` | `30` | Stop repeating after this long in one stage. The next stage starts a fresh budget |
| `ALARM_HURRY_AT` | `0.5` | Share of the delivery window left when hurry starts |
| `ALARM_CRITICAL_AT` | `0.2` | Share left when critical starts |
| `UNHEALTHY_AFTER_SECONDS` | `600` | Send the "can't reach Eldorado" alert after this long |

To hear critical repeats at the full 15 second rate, set `POLL_SECONDS=15`.

## Dead-man switch

| Variable | Default | Meaning |
|---|---|---|
| `DEADMAN_ENABLED` | `false` | Set to `true` to allow SellerDesk to take you offline |
| `DEADMAN_MINUTES` | `10` | How long an alarm can go unanswered first |

When it fires, SellerDesk calls `switchOffline` once and alerts you. SellerDesk never goes online by itself: click **Go online** on the dashboard, or switch on Eldorado.

Going offline doesn't cancel or deliver the order that triggered it.

## Dashboard

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_HOST_PORT` | `8787` | With Docker: the port on your machine |
| `DASHBOARD_PORT` | `8787` | Without Docker: the port to listen on. `0` turns the dashboard off |
| `DASHBOARD_HOST` | `0.0.0.0` | Without Docker: the bind address. Use `127.0.0.1` on a public server |
| `DASHBOARD_TOKEN` | | Password for the dashboard. Set this before you expose the port. The market and finance API stay off until you set it |

## Market hub and ledger

| Variable | Default | Meaning |
|---|---|---|
| `MARKET_ENABLED` | `true` | Set to `false` to stop the server reading Eldorado's public listings |
| `MARKET_PASS_SECONDS` | `180` | Time between checks of where your offers stand. Every 5th check also walks the site order |
| `MARKET_SWEEP_MINUTES` | `30` | Time between reads of the listings you trade in, for the market watch |
| `MARKET_FILE` | `market.json` next to `STATE_FILE` | Where the market hub keeps what it has seen |
| `LEDGER_FILE` | `ledger.json` next to `STATE_FILE` | Where the finance ledger is kept |

## G2G webhooks (optional)

Set all three and SellerDesk accepts G2G's webhooks at `/api/g2g/webhook`. It checks each signature and keeps the events in `G2G_FILE`. Nothing else uses them yet.

| Variable | Default | Meaning |
|---|---|---|
| `G2G_USER_ID` | | Your numeric G2G user ID |
| `G2G_WEBHOOK_SECRET` | | The webhook secret token from G2G's API Integration page |
| `G2G_WEBHOOK_URL` | | The callback URL exactly as you typed it on G2G, with `https://`. G2G signs with it |
| `G2G_FILE` | `g2g-events.jsonl` next to `STATE_FILE` | Where the events are kept |

## Channels

| Variable | Used by |
|---|---|
| `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN`, `NTFY_PUBLIC_URL` | ntfy |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram |
| `DISCORD_WEBHOOK_URL` | Discord |
| `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` | Pushover |
| `WEBHOOK_URL`, `WEBHOOK_TOKEN` | Your own alert server |

[Channels](channels.md) shows where to get each value.

## Alert switches

You pick which alerts the server sends in the desktop app: **Settings**, **Alerts**. The server keeps your choice, so every PC and channel follows it. The tab needs the shared server address and token under **Eldorado key**.

| Kind | Starts | Covers |
|---|---|---|
| `order` | on | A new order |
| `deadline` | on | Deadline close, order late |
| `dispute` | on | A dispute opens, admin messages about it |
| `boosting` | off | Boosting requests |
| `news` | off | Announcements Eldorado writes, such as new games |
| `connection` | off | SellerDesk can't reach Eldorado, or reaches it again |
| `other` | off | Buyer messages, reviews and other Eldorado events |

**Off** sends nothing at all. SellerDesk drops what you switch off; it doesn't send it later. Two exceptions:

- `WEBHOOK_URL` gets every alert, because your own alert server has its own switches. Each message carries `category` and, for a notification, Eldorado's `event`.
- "SellerDesk took you offline" always goes out, since only you can switch back online. With `order` and `deadline` both off, the dead-man switch stays quiet too.

Scripts can read and set the switches at `GET` and `PUT /api/alerts` with `{"paused": false, "off": ["boosting"]}`. A `PUT` needs the token and the `X-Manager: 1` header.

## Docker build

| Variable | Default | Meaning |
|---|---|---|
| `PYTHON_IMAGE` | `python:3.12-alpine` | Base image |
| `NTFY_IMAGE` | `binwiederhier/ntfy:v2.37.0` | ntfy image |
| `STATE_FILE` | `/data/state.json` | Where SellerDesk remembers alarms between restarts |
