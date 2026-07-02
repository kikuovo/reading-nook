<div align="center">

# 📖 Reading Nook · 共读小屋

**[中文](README.md)** · **[English](README.en.md)**

🌏 Don't see your language? [Open a PR](../../pulls) to add one — all welcome.

![license](https://img.shields.io/github/license/zzyyksl/reading-nook?color=8e7cc3)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-zero-c96f4a)
![stars](https://img.shields.io/github/stars/zzyyksl/reading-nook?style=flat&color=e8a0ac)

*Reading is a journey one person completes together with another.*

</div>

---

A self-hosted web app for reading books together with your AI. You read and highlight on your phone (pink bubbles); your AI reads the same chapter and replies with annotations (blue bubbles). Books, progress, and annotations all live on your own server.

**Core design: annotations never touch an API.** Your notes are stored as JSON files on the server. Your AI (Claude Code, or any agent that can read/write server files) reads the JSON and writes replies directly — everything runs on your existing subscription, the annotation loop costs zero API dollars, and the AI can re-read the whole chapter before replying, which beats page-by-page API feeding by a mile.

## Features

- **Mode 1 · Pure Reading**: paginated reader, paragraph-based pages, auto-saved progress, seamless chapter transitions, ☰ table of contents
- **Mode 2 · Annotated Co-Reading**: select text to highlight or write thoughts; your notes are pink bubbles, AI replies are blue; 💬 view all annotations per chapter
- **Upload and go**: txt upload with automatic encoding detection (UTF-8/UTF-16/GBK/Big5) and chapter splitting
- **Smart chapter splitting**: multiple local regex patterns compete; falls back to DeepSeek heading detection (optional); size-based split as last resort
- **Plot notes**: after upload, DeepSeek pre-reads each chapter and writes a 150–250 word note (optional) so your AI can restore context without re-reading the original text
- **DeepSeek Workbench 🖥️**: a transparency panel for your AI helper — every call, its task, token usage, duration, and estimated cost
- **Themes ⚙**: 8 style themes (Cream Puppy / Matcha House / Mucha Floral / B&W Cute / French Blue / Claymorphism / Neumorphism / Glassmorphism), each with day & night modes, sticker & pattern decorations, and matching interactions; 4 independent color slots (your highlights / AI-replied highlights / both annotation bubbles) from 4 curated palettes (B&W / Morandi / Dopamine / Mint Mambo) plus a free color picker; reading background in white / paper / black / custom image; everything lives in the ⚙ settings panel on the shelf page, stored locally in the browser
- **Page turning**: paginated mode (tap screen edges to turn) or seamless vertical scroll that flows into the next chapter automatically
- 4-digit passcode gate, zero third-party dependencies (single-file Python stdlib), mobile-first UI

## Quick Start

```bash
git clone https://github.com/zzyyksl/reading-nook.git
cd reading-nook
cp config.example.json config.json   # set your own passcode and names
python3 app.py                        # requires Python 3.10+
```

Open `http://your-server:8000` (port configurable in config.json), enter the passcode, upload a txt, start reading.

### systemd (optional)

```ini
# /etc/systemd/system/reading.service
[Unit]
Description=reading nook
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Configuration (config.json)

| Field | Description | Default |
|---|---|---|
| `passcode` | 4-digit login passcode | `"0000"` |
| `port` | listen port | `8000` |
| `subtitle` | home page subtitle (your names) | — |
| `login_hint` | passcode hint text | — |
| `user_name` | pink bubble name | `"我"` |
| `ai_name` | blue bubble name | `"AI"` |
| `deepseek_api_key` | DeepSeek key; leave empty to disable chapter fallback & plot notes | `""` |
| `gardener_log` | JSON log path of an external tidy-up job (leave empty if none) | `""` |

## Hooking Up Your AI Companion

Annotations live in `books/<book>/annotations/<chapter>.json`:

```json
[{"id": "...", "anchor": "the highlighted text", "note": "user's thought",
  "who": "user", "ts": "...", "replies": [{"who": "ai", "text": "AI's reply", "ts": "..."}]}]
```

Your AI appends a reply to `replies`; the user refreshes and sees a blue bubble. Two helper endpoints:

- `GET /api/pending` — list all annotations awaiting a reply (requires passcode cookie: `rk=<passcode>`)
- `GET /api/note/<book>/<chapter>` — read the DeepSeek plot note for fast context recovery

Typical workflow: user highlights → pings the AI in chat → AI reads pending → checks the note / re-reads the chapter → writes back to JSON → user refreshes.

## Security Notes

The 4-digit passcode keeps out passers-by; it is not a security boundary (built-in rate limiting: 5 wrong attempts per IP → 30-minute block; 127.0.0.1 exempt). Recommended: run behind a firewall that only admits you, or put a reverse proxy with HTTPS and real auth in front. Don't store sensitive documents in it.

## License

MIT
