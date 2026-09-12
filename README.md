# Gaty 💜 — DeepSeek Web Bridge

Run a **free local AI** using your logged-in DeepSeek browser tab, bridged to any OpenAI or Anthropic-compatible client — including **Claude CLI**, Cline, Continue, Aider, and more.

```
  ██████╗  █████╗ ████████╗██╗   ██╗
 ██╔════╝ ██╔══██╗╚══██╔══╝╚██╗ ██╔╝
 ██║  ███╗███████║   ██║    ╚████╔╝
 ██║   ██║██╔══██║   ██║     ╚██╔╝
 ╚██████╔╝██║  ██║   ██║      ██║
  ╚═════╝ ╚═╝  ╚═╝   ╚═╝      ╚═╝
```

---

## How it works

```
Claude CLI  ──►  proxy.py (:1337)  ──►  WebSocket  ──►  DeepSeek browser tab
                      │                                        │
                      │◄───────── DOM-polled response ◄────────┘
```

`proxy.py` speaks both the **Anthropic API** (`/v1/messages`) and the **OpenAI API** (`/v1/chat/completions`), so it works with any client.

The Tampermonkey script (`deepseek-bridge.user.js`) runs inside your DeepSeek tab, connects back to `proxy.py` via WebSocket, submits prompts using DeepSeek's React internals, and returns the response by watching the DOM.

---

## Setup

### 1. Install Python dependencies

```cmd
pip install -r requirements.txt
```

### 2. Start the proxy

```cmd
python proxy.py
```

Optional flags:
```cmd
python proxy.py --host 127.0.0.1 --port 1337 --reset-threshold 150000
```

You should see:
```
[Gaty] OpenAI + Anthropic bridge on http://127.0.0.1:1337
[Gaty] WebSocket endpoint: ws://127.0.0.1:1337/ws
```

### 3. Install the Tampermonkey script

1. Install **Tampermonkey** in your browser:
   - Chrome / Edge / Brave: [Chrome Web Store](https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
   - Firefox: [Firefox Add-ons](https://addons.mozilla.org/firefox/addon/tampermonkey/)
   - Safari: [App Store](https://apps.apple.com/app/tampermonkey/id1482490089)
2. Open Tampermonkey → Dashboard → **New script** (the `+` tab)
3. Delete the default template, paste the full contents of [`deepseek-bridge.user.js`](https://raw.githubusercontent.com/nesquickwe/gaty-deepseek-bridge/main/deepseek-bridge.user.js)
4. Save (Ctrl+S)
5. Open [chat.deepseek.com](https://chat.deepseek.com) — you should see a **purple "Gaty: Connected 💜"** badge in the bottom-right corner

---

## Claude CLI setup

### Windows — set environment variables permanently

```cmd
reg add "HKCU\Environment" /v ANTHROPIC_API_KEY  /t REG_SZ /d "nah" /f
reg add "HKCU\Environment" /v ANTHROPIC_BASE_URL /t REG_SZ /d "http://127.0.0.1:1337" /f
reg add "HKCU\Environment" /v ANTHROPIC_MODEL    /t REG_SZ /d "deepseek-reasoner" /f
```

Open a **new** cmd window after running these (environment variables are only picked up by new processes).

### Or use `~/.claude/settings.json`

Create or edit `C:\Users\<you>\.claude\settings.json`:

```json
{
  "env": {
    "ANTHROPIC_API_KEY":  "nah",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:1337",
    "ANTHROPIC_MODEL":    "deepseek-reasoner",
    "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1"
  },
  "permissions": { "allow": [], "deny": [] },
  "theme": "dark"
}
```

### Run Claude CLI

```cmd
claude
```

You'll see `deepseek-reasoner · API Usage Billing` in the header. That's fine — just type and it routes through Gaty to DeepSeek for free.

---

## Models

| Model ID | What it does |
|---|---|
| `deepseek-chat` | Fast standard responses |
| `deepseek-reasoner` | Extended thinking / reasoning (slower, smarter) |

---

## Reset the chat session

Type any of these inside Claude CLI to wipe the DeepSeek session and start fresh:

```
/clear   /reset   /new   /clearchat
```

Or run this in the DeepSeek browser console:
```javascript
window.gatyDeleteChat()
```

---

## Supported clients

| Client | Config |
|---|---|
| **Claude CLI** | `ANTHROPIC_BASE_URL=http://127.0.0.1:1337` |
| **Cline** (VS Code) | Base URL: `http://127.0.0.1:1337/v1`, API key: `nah` |
| **Continue** | OpenAI provider, base URL: `http://127.0.0.1:1337/v1` |
| **Aider** | `--openai-api-base http://127.0.0.1:1337/v1 --openai-api-key nah` |
| **OpenAI SDK** | `base_url="http://127.0.0.1:1337/v1"`, `api_key="nah"` |

---

## Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/messages` | Anthropic-format (Claude CLI) |
| `POST /v1/chat/completions` | OpenAI-format (Cline, Aider, etc.) |
| `GET  /v1/models` | Model list |
| `GET  /health` | Health check + connection status |
| `WS   /ws` | Tampermonkey WebSocket connection |

---

## Troubleshooting

**"Gaty: Disconnected" badge** — click it to reconnect, or reload the DeepSeek tab.

**Empty responses** — the DeepSeek tab navigated away mid-generation. Hard-reload the tab (Ctrl+Shift+R) to re-initialize the bridge.

**Claude CLI shows model error** — make sure `ANTHROPIC_BASE_URL` is `http://127.0.0.1:1337` (no `/v1` suffix) and open a fresh cmd window after setting env vars.

**Slow responses** — `deepseek-reasoner` thinks before answering, expect 10–60 seconds. Use `deepseek-chat` for faster replies.

---

## Disclaimer

Educational and research use only. Not affiliated with DeepSeek or Anthropic.

## License

MIT
