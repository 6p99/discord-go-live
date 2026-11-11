# Discord Go Live · Streamer + Viewer

`stream.py` is a single-file Python tool that keeps a Discord account **live** in
a voice channel while a second account sits in the same channel as a **viewer** —
the setup Discord requires before it counts stream time.

> **Author:** anonymous
> **Discord:** `6p_9`

---

## ⚠️ Read this first

- This uses **user tokens** (a self-bot). That is against Discord's Terms of
  Service and **accounts can be banned**. Use throwaway accounts only.
- Stream time only counts when **both** conditions are true:
  1. another account is actually sitting in the voice channel (that is the
     watcher this tool runs for you), and
  2. *User Settings → Data & Privacy → Use data to personalize my Discord
     experience* is **ON** for the streaming account.
- No warranty. You run this at your own risk.

---

## Quick start

```bash
pip install -r requirements.txt                      # optional: installed automatically
python stream.py --guild 123456789012345678 --channel 987654321098765432
```

On the first run it asks for the two tokens with hidden input, saves them, then
shows the duration menu. After that the same command starts the stream directly.

- Python **3.10+** (tested on 3.14, Windows)
- `websockets`, `davey`, `cryptography` — install with `pip install -r requirements.txt`
  (or let the script install them on first run).
- The gear icon next to your server → *Copy Server ID* gives the guild ID;
  right-click a voice channel → *Copy Channel ID* gives the channel ID
  (enable **Developer Mode** in *Advanced* settings first).

---

## What it does

| Account | Role | What it does |
| --- | --- | --- |
| **Streaming account** (`--token`) | Broadcaster | Connects to the gateway, joins the voice channel, requests **Go Live** (screen share) and keeps the stream alive |
| **Watching account** (`--watch-token`) | Viewer | Joins the same voice channel muted, sends a *watch stream* request and opens a viewer connection on that stream |

Both accounts run inside one process. The watcher starts the moment the stream
goes live (gateway event `STREAM_CREATE`) and stops when the stream ends.

---

## Command line

| Flag | Description |
| --- | --- |
| `--token` | Streaming account token (else `DISCORD_TOKEN`, then the saved value, then a prompt) |
| `--watch-token` | Watching account token (else `DISCORD_WATCH_TOKEN`, then the saved value, then a prompt) |
| `--guild` | Server (guild) ID |
| `--channel` | Voice channel ID |
| `--hours N` | Skip the menu and stream `N` hours (`0` = until Ctrl+C) |
| `--no-watch` | Stream only, do not start the viewer (stream time will not count) |
| `--reset-tokens` | Forget the saved tokens and ask again |
| `--log PATH` | Where to write the redacted watcher log (default: `watcher_debug.log`) |

Examples:

```bash
python stream.py --guild 123456789012345678 --channel 987654321098765432
python stream.py --guild 123456789012345678 --channel 987654321098765432 --hours 6
python stream.py --token "..." --watch-token "..." --guild 123 --channel 456
python stream.py --reset-tokens
```

---

## Duration menu

Without `--hours` the tool asks how long to stream before starting:

```
  Stream to which tier?
  ↑/↓ move · enter select
  › ○ Tier  1 · Newcomer           1h     +0.4h
    ○ Tier  2 · Fledgling          5h     +4.4h
    ○ Tier  3 · Breakout          20h    +19.4h
    ○ Tier  4 · Standout          75h    +74.4h
    ○ Tier  5 · Trendsetter      150h   +149.4h
    ○ Tier  6 · Headliner        300h   +299.4h
    ○ Tier  7 · Star             500h   +499.4h
    ○ Tier  8 · Sensation      1,000h   +999.4h
    ○ Tier  9 · Visionary      2,000h +1,999.4h
    ○ Tier 10 · Phenomenon     5,000h +4,999.4h
    ○ Custom hours...
```

- `↑` / `↓` (or `W`/`S`, `K`/`J`) move, `Enter` selects.
- The `+Xh` column is the remaining time to reach that tier, based on the hours
  this tool has already recorded on your machine.
- **Custom hours** accepts any number (`2.5`); `0` means *no limit*.
- Pressing `Enter` never stops a running session — only **Ctrl+C** or the
  selected duration does.

---

## Exit order

The streaming account always leaves the voice channel **before** the watching
account:

```
  · stopping: the streaming account leaves first, the watcher after it...
  · streamer left, now closing the watching account...
```

| Situation | What happens |
| --- | --- |
| **Ctrl+C** | Stream deleted (`op 19`), voice left (`op 4`), streamer sockets closed → then the viewer leaves |
| **Console window closed** | Same order, through a Windows console-control handler |
| **Network drop** | The streamer's sockets are closed first, then the viewer is stopped after an 8-second grace period |
| **Network comes back quickly** | The grace timer is cancelled and the viewer stays connected |
| **Stream returns after a drop** | The viewer restarts automatically |

---

## Features

- One file: websocket client, voice/DAVE layer, streamer and viewer — no other
  project files needed.
- Duration menu with the ten tiers plus custom hours.
- Ctrl+C-only stop (Enter does nothing).
- Guaranteed exit order (streamer first, viewer second).
- Automatic reconnects with gateway session resume and exponential backoff.
- 4-hour rotation — leaves and rejoins shortly before Discord's 5-hour limit.
- End-to-end encrypted voice (DAVE) plus a silence/keepalive pump.
- Dependencies install themselves on first run if missing.
- Redacted debug log — tokens never reach the log file.

---

## Files created at runtime

| File | Contents |
| --- | --- |
| `stream_session.json` | Your two tokens + local stream totals (chmod 600 when possible) |
| `watcher_debug.log` | Redacted protocol trace of the viewer (tokens become `<redacted>`) |

> **Never commit `stream_session.json`.** It contains live account tokens.
> The included `.gitignore` already excludes it and `*.log`.

---

## How it works (short version)

1. Connects to `wss://gateway.discord.gg` with the streaming token
   (`identify`), then joins the voice channel (`op 4`).
2. Opens the voice websocket (`v=7`), performs UDP discovery, picks an
   encryption mode and sets up DAVE end-to-end encryption.
3. Requests Go Live with `op 18` (`STREAM_CREATE`), then keeps the UDP
   keepalive / silence pump running.
4. On `STREAM_CREATE` the viewer thread connects with the second token, joins
   the same channel muted (`op 4`), sends `op 20` (watch stream) and opens its
   own voice connection with `video=true` — that is what makes Discord register
   a viewer.
5. Every 4 hours the streamer leaves and rejoins (`op 19` + `op 4` + `op 4`) to
   stay under Discord's 5-hour stream limit.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Gateway closes with **4004** | Token invalid or expired. Run `--reset-tokens` or delete `stream_session.json` |
| Stream is live but **hours do not increase** | Is the viewer in the channel? Is *Use data to personalize my Discord experience* enabled? |
| Viewer never joins | Check that the two tokens are **different** accounts and that the watcher can see the channel |
| `UDP handshake failed` in the log | Firewall/network blocking UDP — voice may connect but the viewer can be unstable |
| `both tokens belong to the same account` | Use a second account; a self-viewer does not count |
| Enter does nothing | By design: only **Ctrl+C** (or a chosen duration) stops the tool |

---

## License

MIT — do whatever you want with it, no warranty.
