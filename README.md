# ⚡ PyLauncher Pro

**One dark-themed dashboard to start, stop, monitor and auto-restart all your Python scripts, Node servers and tunnels on Windows.** No more juggling a dozen console windows.

![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB)
![Dependencies](https://img.shields.io/badge/dependencies-none%20(stdlib%20only)-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

If you self-host a handful of small apps (bots, Flask/Node servers, ngrok or Cloudflare tunnels), PyLauncher Pro gives each one a **card** with a Start/Stop button and a live log. It starts the ones you want at launch and brings them back automatically when they crash.

## Features

- **One card per program**: ▶ START / ■ STOP, live output log, PID, uptime and whether its port is actually listening.
- **Scripts or commands**: run a Python script (with the system Python or **any interpreter / virtualenv**), and/or up to **three extra commands** in parallel, e.g. `node server.js` + `ngrok http 3001` + `cloudflared tunnel run`.
- **Auto-start on launch** for selected programs.
- **Crash watchdog**: optional auto-restart when a process dies (gives up after 10 attempts).
- **Wedged-tunnel detection**: with `tunnel_watch`, a program is restarted when its `health_urls` stop answering even though the process is still alive. This catches the classic "Node server died behind a live ngrok tunnel".
- **ngrok public-URL discovery** through ngrok's local API (`:4040`).
- **HTTP health probe** for programs with a known port (auto-detected from the commands if you don't set one).
- **Diagnostics per program** and a global **System Check**: interpreters, working dirs, tools on PATH, Node dependencies, ngrok config, ports.
- **Restart / Restart All**, open folder, open terminal, copy command, and a search filter.
- **Pure standard library**, so there's nothing to `pip install`. Also available as a single `.exe`.

---

## Download & run

### Option A: ready-made `.exe`

Download **`PyLauncher_Pro.exe`** from the [latest release](../../releases/latest), put it in its own folder and run it. Your program list (`launcher_config.json`) is created **next to the exe**.

> The exe isn't code-signed, so Windows SmartScreen may warn about it. Click **More info → Run anyway**, or run from source.

### Option B: from source

1. Install **Python 3.9+** from [python.org](https://www.python.org/downloads/) (tkinter is included).
2. Download this repository (**Code → Download ZIP**, or `git clone https://github.com/deniskostadinov1995/pylauncher-pro.git`).
3. Double-click **`start_pro.bat`** (or run `python launcher_pro.py`).

## Adding your programs

Click **+ Add** and fill in the dialog, or copy `launcher_config.example.json` to `launcher_config.json` and edit it. Every program is one object in `programs`:

| Field | Meaning |
|---|---|
| `name` | Card title (must be unique) |
| `path` | Python script to run. Leave blank to run only commands. |
| `description` | Short text on the card. **Keep it under ~75 characters**, or it pushes the buttons off the card. |
| `interpreter` | Python executable to use (e.g. a venv's `python.exe`). Blank = system Python. |
| `extra_cmd`, `extra_cmd2`, `extra_cmd3` | Extra shell commands started alongside, e.g. `node server.js`, `ngrok http 3001` |
| `workdir` | Working directory. Blank = the script's folder; **required** if there is no script. |
| `port` | Port for the health check. Auto-detected from the commands if blank. |
| `autostart` | Start automatically when PyLauncher opens |
| `auto_restart` | Restart automatically if the process exits |
| `tunnel_watch` + `health_urls` | Restart when any of these URLs stops answering (2xx–4xx counts as healthy) while the process still runs |

### Tips

- **Edit `launcher_config.json` while PyLauncher is closed.** It reads the file at startup and rewrites it whenever you save a card in the UI.
- **Save it as UTF-8 *without* BOM.** PowerShell's `Set-Content -Encoding UTF8` adds a BOM that Python's `json` refuses. If the file can't be parsed, PyLauncher starts over with an example config, so keep a backup.
- Stopping uses `taskkill /F /T`, which also ends child processes such as a tunnel started by the same card.

## Start it with Windows

The simplest way: press `Win + R`, type `shell:startup`, and put a shortcut to `PyLauncher_Pro.exe` (or `start_pro.bat`) in that folder. Everything marked **Auto-start on launch** then comes up after you log in.

## Building the exe yourself

```bash
pip install pyinstaller
pyinstaller PyLauncher_Pro.spec
```

The result is `dist\PyLauncher_Pro.exe`.

## License

[MIT](LICENSE) © Denis Kostadinov
