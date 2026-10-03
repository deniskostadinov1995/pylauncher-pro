"""
PyLauncher Pro — enhanced process launcher & health dashboard.

A drop-in superset of launcher.py. Reads the SAME launcher_config.json (resolved
next to this script / exe) so all your programs carry over. v1 is left untouched;
run this alongside v1, then switch when ready.

Adds on top of v1:
  • Live PID + uptime + real port-listening status per program
  • ngrok public-URL auto-discovery (via ngrok's local API on :4040)
  • Crash detection + optional auto-restart watchdog (per program)
  • Restart / Restart-All
  • Per-program Diagnostics and a global System Check (tools, configs, deps, ports)
  • Open folder / open terminal / copy command / search filter
  • HTTP health probe for programs with a known local port

Pure standard library — no pip installs needed.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import threading
import json
import os
import sys
import signal
import queue
import time
import re
import socket
import shutil
import glob
import webbrowser
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

# ─── Paths ──────────────────────────────────────────────────────────────────────
def _base_dir():
    """Directory of this script (or the frozen exe). Config lives here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _base_dir()
CONFIG_FILE = os.path.join(BASE_DIR, "launcher_config.json")

DEFAULT_CONFIG = {"programs": []}

# ─── Colors ─────────────────────────────────────────────────────────────────────
BG          = "#0e0e0e"
BG2         = "#161616"
BG3         = "#1e1e1e"
BORDER      = "#2a2a2a"
ACCENT      = "#e8a020"
ACCENT2     = "#c47d10"
TEXT        = "#e8e8e8"
TEXT_DIM    = "#888888"
TEXT_DIMMER = "#4a4a4a"
GREEN       = "#2ecc71"
RED         = "#e74c3c"
YELLOW      = "#f1c40f"
BLUE        = "#5aa0e8"
PURPLE      = "#b57edc"
FONT_MAIN   = ("Consolas", 10)
FONT_TITLE  = ("Consolas", 11, "bold")
FONT_SMALL  = ("Consolas", 9)
FONT_TINY   = ("Consolas", 8)
FONT_LOG    = ("Consolas", 9)

# ─── Config I/O ──────────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def ts():
    return datetime.now().strftime("%H:%M:%S")

def fmt_uptime(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

# ─── Tool / network probes ───────────────────────────────────────────────────────
def which_tool(name):
    """Return full path to a tool on PATH, or None."""
    p = shutil.which(name)
    if p:
        return p
    if sys.platform == "win32" and not name.lower().endswith(".exe"):
        return shutil.which(name + ".exe")
    return None

def tool_version(path_or_name, args=("--version",)):
    try:
        r = subprocess.run([path_or_name, *args], capture_output=True, text=True,
                           timeout=5,
                           creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
        out = (r.stdout or "") + (r.stderr or "")
        return out.strip().splitlines()[0] if out.strip() else "(no version output)"
    except Exception as e:
        return f"(error: {e})"

def port_listening(port, host="127.0.0.1", timeout=0.25):
    """True if something is accepting connections on host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False

def http_probe(port, path="/", host="127.0.0.1", timeout=1.5):
    """Return (status_code, elapsed_ms) or (None, None)."""
    url = f"http://{host}:{port}{path}"
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "PyLauncherPro"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None

def probe_url(url, timeout=3.0):
    """Return the HTTP status code for a full URL (e.g. a tunnel health endpoint),
    or None if it is unreachable. Used by the tunnel watchdog."""
    try:
        u = urllib.parse.urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        path = u.path or "/"
        return http_probe(port, path=path, host=host, timeout=timeout)[0]
    except Exception:
        return None

def get_ngrok_tunnels():
    """Return {local_port(int): public_url} from ngrok's local API. Empty if ngrok not running."""
    result = {}
    for base in ("http://127.0.0.1:4040", "http://127.0.0.1:4041"):
        try:
            with urllib.request.urlopen(base + "/api/tunnels", timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            for t in data.get("tunnels", []):
                pub = t.get("public_url", "")
                addr = (t.get("config", {}) or {}).get("addr", "")
                m = re.search(r':(\d{2,5})\b', addr)
                if pub and m:
                    result[int(m.group(1))] = pub
        except Exception:
            pass
    return result

# ─── Port detection ──────────────────────────────────────────────────────────────
_PORT_CACHE = {}

def _ports_from_cmd(cmd):
    ports = []
    if not cmd:
        return ports
    for m in re.finditer(r':(\d{2,5})\b', cmd):
        ports.append(int(m.group(1)))
    for tok in re.findall(r'(?<!\S)(\d{2,5})(?!\S)', cmd):
        ports.append(int(tok))
    return [p for p in ports if 1 <= p <= 65535]

def _scan_script_for_port(path):
    try:
        if not path or not os.path.isfile(path):
            return None
        if os.path.getsize(path) > 2_000_000:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(200_000)
    except Exception:
        return None
    patterns = [
        r'\.run\([^)]*port\s*=\s*(\d{2,5})',
        r'socketio\.run\([^)]*port\s*=\s*(\d{2,5})',
        r'\blisten\(\s*(\d{2,5})',
        r'PORT[^\n=]*=\s*(\d{2,5})',
        r'port[^\n=]*=\s*(\d{2,5})',
        r'\|\|\s*(\d{2,5})',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            v = int(m.group(1))
            if 1000 <= v <= 65535:
                return v
    return None

def detect_port(program):
    """Best-effort single local port for a program. Priority: explicit > command > script scan."""
    key = program.get("name", "") + "|" + program.get("path", "") + "|" + \
          program.get("extra_cmd", "") + "|" + program.get("extra_cmd2", "")
    if key in _PORT_CACHE:
        return _PORT_CACHE[key]
    port = None
    if program.get("port"):
        try:
            port = int(program["port"])
        except (ValueError, TypeError):
            port = None
    if port is None:
        cand = _ports_from_cmd(program.get("extra_cmd", "")) + _ports_from_cmd(program.get("extra_cmd2", ""))
        # prefer app-server range
        pref = [p for p in cand if 1000 <= p <= 9999]
        if pref:
            port = pref[0]
        elif cand:
            port = cand[0]
    if port is None:
        p = program.get("path", "")
        if p.lower().endswith((".py", ".pyw", ".js", ".mjs")):
            port = _scan_script_for_port(p)
    _PORT_CACHE[key] = port
    return port

# ─── Interpreter discovery (from v1) ─────────────────────────────────────────────
def find_python_installs():
    candidates = set()
    exe_names = ("python.exe", "python3.exe") if sys.platform == "win32" else ("python3", "python")
    for p in os.environ.get("PATH", "").split(os.pathsep):
        for name in exe_names:
            full = os.path.join(p, name)
            if os.path.isfile(full):
                candidates.add(os.path.normpath(os.path.realpath(full)))
    if sys.platform == "win32":
        roots = [
            r"C:\Python*", r"C:\Program Files\Python*", r"C:\Program Files (x86)\Python*",
            r"C:\Users\*\AppData\Local\Programs\Python\Python*",
        ]
        for pattern in roots:
            for folder in glob.glob(pattern):
                exe = os.path.join(folder, "python.exe")
                if os.path.isfile(exe):
                    candidates.add(os.path.normpath(exe))
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for sub in (r"SOFTWARE\Python\PythonCore", r"SOFTWARE\WOW6432Node\Python\PythonCore"):
                    try:
                        with winreg.OpenKey(hive, sub) as base:
                            i = 0
                            while True:
                                try:
                                    ver = winreg.EnumKey(base, i); i += 1
                                    with winreg.OpenKey(base, ver + r"\InstallPath") as ip:
                                        path = winreg.QueryValue(ip, None).strip().rstrip("\\")
                                        exe = os.path.join(path, "python.exe")
                                        if os.path.isfile(exe):
                                            candidates.add(os.path.normpath(exe))
                                except OSError:
                                    break
                    except OSError:
                        pass
        except ImportError:
            pass
    results = []
    for exe in sorted(candidates):
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=4)
            ver = (r.stdout + r.stderr).strip()
            results.append((ver, exe))
        except Exception:
            pass
    results.sort(key=lambda x: x[0])
    return results

def _find_system_python():
    exe_name = "python.exe" if sys.platform == "win32" else "python3"
    if sys.platform == "win32":
        candidates = []
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for sub in (r"SOFTWARE\Python\PythonCore", r"SOFTWARE\WOW6432Node\Python\PythonCore"):
                    try:
                        base = winreg.OpenKey(hive, sub, access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
                    except OSError:
                        continue
                    with base:
                        i = 0
                        while True:
                            try:
                                ver = winreg.EnumKey(base, i); i += 1
                            except OSError:
                                break
                            try:
                                with winreg.OpenKey(base, ver + r"\InstallPath", access=winreg.KEY_READ) as ip:
                                    path = winreg.QueryValue(ip, None)
                                    if path:
                                        exe = os.path.join(path.strip().rstrip("\\"), "python.exe")
                                        if os.path.isfile(exe):
                                            candidates.append((ver, exe))
                            except OSError:
                                pass
        except Exception:
            pass
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    for p in os.environ.get("PATH", "").split(os.pathsep):
        try:
            exe = os.path.join(p, exe_name)
            if os.path.isfile(exe):
                return exe
        except Exception:
            pass
    return exe_name

# ─── Process Manager ─────────────────────────────────────────────────────────────
class ProcessManager:
    def __init__(self):
        self.processes = {}        # name -> Popen | None
        self.extra_processes = {}  # key  -> Popen
        self.log_queues = {}       # name -> queue.Queue
        self.threads = {}          # name -> [threads]
        self.start_times = {}      # name -> epoch float
        self.exit_codes = {}       # name -> last exit code (int) or None
        self.intended = {}         # name -> bool (should be running; drives watchdog)
        self.restart_counts = {}   # name -> int (consecutive auto-restarts)

    def start(self, name, path, interpreter=None, extra_cmd=None, extra_cmd2=None, extra_cmd3=None, workdir=None):
        if self.is_running(name):
            return False, "Already running"
        if path and not os.path.exists(path):
            return False, f"File not found: {path}"

        q = self.log_queues.get(name) or queue.Queue()
        self.log_queues[name] = q

        if interpreter:
            python_exe = interpreter
        else:
            python_exe = sys.executable
            if getattr(sys, "frozen", False):
                python_exe = _find_system_python()
            elif sys.platform == "win32" and python_exe.lower().endswith("pythonw.exe"):
                python_exe = python_exe[:-11] + "python.exe"

        if workdir and os.path.isdir(workdir):
            script_cwd = workdir
        elif path:
            script_cwd = os.path.dirname(os.path.abspath(path)) or "."
        else:
            script_cwd = workdir or "."

        try:
            popen_kwargs = {}
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                popen_kwargs["start_new_session"] = True

            def reader(stream, tag):
                try:
                    for line in stream:
                        line = line.rstrip("\n")
                        if line:
                            q.put((tag, line))
                except Exception:
                    pass
                finally:
                    q.put(("EXIT", ""))

            self.threads[name] = []
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

            if path:
                proc = subprocess.Popen(
                    [python_exe, "-u", path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    **popen_kwargs, env=env, cwd=script_cwd,
                )
                self.processes[name] = proc
                t1 = threading.Thread(target=reader, args=(proc.stdout, "OUT"), daemon=True)
                t2 = threading.Thread(target=reader, args=(proc.stderr, "ERR"), daemon=True)
                t1.start(); t2.start()
                self.threads[name] += [t1, t2]
            else:
                self.processes[name] = None

            for cmd, key, tag in ((extra_cmd, name, "CMD"), (extra_cmd2, name + "__2", "CMD"),
                                  (extra_cmd3, name + "__3", "CMD")):
                if not cmd:
                    continue
                try:
                    import shlex
                    parts = shlex.split(cmd, posix=False) if sys.platform == "win32" else shlex.split(cmd)
                    ep = subprocess.Popen(
                        parts, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                        **popen_kwargs, env=env, cwd=script_cwd,
                    )
                    self.extra_processes[key] = ep
                    ta = threading.Thread(target=reader, args=(ep.stdout, tag), daemon=True)
                    tb = threading.Thread(target=reader, args=(ep.stderr, tag), daemon=True)
                    ta.start(); tb.start()
                    self.threads[name] += [ta, tb]
                except Exception as e:
                    q.put(("SYS", f"[cmd error] {cmd!r}: {e}"))

            self.start_times[name] = time.time()
            self.exit_codes[name] = None
            self.intended[name] = True
            return True, "Started"
        except PermissionError as e:
            return False, (
                "Access denied launching process.\n"
                "Try running as Administrator, or set a custom interpreter.\n\n"
                f"Detail: {e}"
            )
        except FileNotFoundError as e:
            return False, f"Interpreter/command not found: {e}"
        except Exception as e:
            return False, str(e)

    def _kill_proc_tree(self, proc):
        if proc is None or proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.call(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    def stop(self, name, user_initiated=True):
        if user_initiated:
            self.intended[name] = False
        was_running = self.is_running(name)
        # capture exit code of main proc if available
        main = self.processes.get(name)
        if main is not None:
            self._kill_proc_tree(main)
            self.exit_codes[name] = main.poll()
        for key in (name, name + "__2", name + "__3"):
            self._kill_proc_tree(self.extra_processes.get(key))
        self.start_times.pop(name, None)
        return (was_running, "Stopped" if was_running else "Not running")

    def is_running(self, name):
        proc = self.processes.get(name)
        if proc is not None and proc.poll() is None:
            return True
        for key in (name, name + "__2", name + "__3"):
            extra = self.extra_processes.get(key)
            if extra is not None and extra.poll() is None:
                return True
        return False

    def pids(self, name):
        out = []
        proc = self.processes.get(name)
        if proc is not None and proc.poll() is None:
            out.append(proc.pid)
        for key in (name, name + "__2", name + "__3"):
            extra = self.extra_processes.get(key)
            if extra is not None and extra.poll() is None:
                out.append(extra.pid)
        return out

    def uptime(self, name):
        if not self.is_running(name):
            return None
        t = self.start_times.get(name)
        return (time.time() - t) if t else None

    def get_log_queue(self, name):
        return self.log_queues.get(name)

# ─── Health monitor (background thread) ──────────────────────────────────────────
class HealthMonitor(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.wait(2.0):
            try:
                programs = list(self.app.config_data.get("programs", []))
                ports = {}
                for p in programs:
                    port = detect_port(p)
                    if port:
                        ports[port] = port_listening(port)
                tunnels = get_ngrok_tunnels()
                with self.app.health_lock:
                    self.app.health_ports = ports
                    self.app.health_tunnels = tunnels
            except Exception:
                pass

# ─── Diagnostics ─────────────────────────────────────────────────────────────────
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

def program_diagnostics(program):
    """Return list of (label, level, detail) checks for one program."""
    checks = []
    path = program.get("path", "")
    workdir = program.get("workdir", "")
    interp = program.get("interpreter", "")
    e1 = program.get("extra_cmd", "")
    e2 = program.get("extra_cmd2", "")
    e3 = program.get("extra_cmd3", "")

    # Script
    if path:
        if os.path.isfile(path):
            checks.append(("Script file", OK, path))
        else:
            checks.append(("Script file", FAIL, f"missing: {path}"))
        if path.lower().endswith((".py", ".pyw")):
            py = interp or "system python"
            if interp and not os.path.isfile(interp):
                checks.append(("Interpreter", FAIL, f"missing: {interp}"))
            elif interp:
                checks.append(("Interpreter", OK, interp))
            else:
                found = which_tool("python") or _find_system_python()
                checks.append(("Python", OK if found else FAIL, found or "not found"))
    else:
        checks.append(("Mode", INFO, "command-only (no script)"))

    # Working dir
    if workdir:
        checks.append(("Working dir", OK if os.path.isdir(workdir) else FAIL,
                       workdir if os.path.isdir(workdir) else f"missing: {workdir}"))
    elif not path:
        checks.append(("Working dir", FAIL, "required when no script is set"))

    # Extra commands -> tool + node deps + ngrok config
    for cmd in (e1, e2, e3):
        if not cmd:
            continue
        tool = cmd.split()[0] if cmd.split() else ""
        low = cmd.lower()
        if tool:
            resolved = which_tool(tool)
            checks.append((f"Tool '{tool}'", OK if resolved else FAIL,
                           resolved or "not on PATH"))
        if tool.lower() == "node":
            base = workdir or (os.path.dirname(path) if path else "")
            if base:
                pkg = os.path.join(base, "package.json")
                nm = os.path.join(base, "node_modules")
                if os.path.isfile(pkg):
                    try:
                        with open(pkg, encoding="utf-8") as f:
                            deps = json.load(f).get("dependencies", {})
                    except Exception:
                        deps = {}
                    if deps and not os.path.isdir(nm):
                        checks.append(("node_modules", WARN,
                                       f"{len(deps)} deps but node_modules missing — run 'npm install'"))
                    elif deps:
                        checks.append(("node_modules", OK, f"{len(deps)} deps installed"))
                    else:
                        checks.append(("node deps", OK, "no dependencies declared"))
        if "ngrok" in low and "--config=" in low:
            m = re.search(r'--config=(?:"([^"]+)"|(\S+))', cmd)
            cfg = (m.group(1) or m.group(2)) if m else None
            if cfg:
                checks.append(("ngrok config", OK if os.path.isfile(cfg) else FAIL,
                               cfg if os.path.isfile(cfg) else f"missing: {cfg}"))

    # Port
    port = detect_port(program)
    if port:
        listening = port_listening(port)
        checks.append((f"Port {port}", OK if listening else INFO,
                       "LISTENING" if listening else "not listening (ok if stopped)"))
        if listening:
            code, ms = http_probe(port)
            if code:
                checks.append(("HTTP probe", OK if code < 500 else WARN,
                               f"HTTP {code}" + (f" · {ms}ms" if ms is not None else "")))
    return checks

def system_check():
    """Return (tool_rows, note_rows). Global environment verification."""
    tools = []
    for name in ("python", "node", "npm", "ngrok", "cloudflared", "git"):
        path = which_tool(name)
        if path:
            tools.append((name, OK, f"{tool_version(path)}   —   {path}"))
        else:
            level = WARN if name in ("cloudflared", "git") else FAIL
            tools.append((name, level, "not found on PATH"))
    return tools

# ─── Add/Edit dialog ─────────────────────────────────────────────────────────────
class ProgramDialog(tk.Toplevel):
    def __init__(self, parent, program=None):
        super().__init__(parent)
        self.result = None
        self.program = program
        self.title("Edit Program" if program else "Add Program")
        self.configure(bg=BG2)
        self.resizable(False, False)
        self.grab_set()
        pad = {"padx": 14, "pady": 4}

        def entry(row, label, key, hint=None, width=36, browse=None):
            tk.Label(self, text=label, bg=BG2, fg=TEXT_DIM, font=FONT_SMALL)\
                .grid(row=row, column=0, sticky="w", **pad)
            var = tk.StringVar(value=(program.get(key, "") if program else ""))
            if browse:
                fr = tk.Frame(self, bg=BG2)
                fr.grid(row=row, column=1, sticky="ew", padx=(0, 14), pady=4)
                tk.Entry(fr, textvariable=var, bg=BG3, fg=TEXT, font=FONT_MAIN,
                         insertbackground=ACCENT, relief="flat", width=width - 8,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT).pack(side="left", fill="x", expand=True)
                tk.Button(fr, text="…", bg=BG3, fg=ACCENT, font=FONT_SMALL, relief="flat",
                          bd=0, padx=8, cursor="hand2", command=browse).pack(side="left", padx=(4, 0))
            else:
                tk.Entry(self, textvariable=var, bg=BG3, fg=TEXT, font=FONT_MAIN,
                         insertbackground=ACCENT, relief="flat", width=width,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT).grid(row=row, column=1, sticky="ew", padx=(0, 14), pady=4)
            if hint:
                tk.Label(self, text=hint, bg=BG2, fg=TEXT_DIMMER, font=FONT_TINY)\
                    .grid(row=row + 1, column=1, sticky="w", padx=(0, 14))
            return var

        self.name_var = entry(0, "Name", "name")
        self.path_var = entry(1, "Python script", "path",
                              hint="Leave blank if using command only", browse=self._browse)
        self.desc_var = entry(3, "Description", "description")
        self.interp_var = entry(4, "Python interpreter", "interpreter",
                                hint="Leave blank to use system Python", browse=self._browse_interp)
        self.extra_var = entry(6, "Extra command", "extra_cmd",
                               hint="e.g.  node server.js   or   ngrok http 3001")
        self.extra2_var = entry(8, "Command 2", "extra_cmd2",
                                hint="Optional second command run at the same time")
        self.extra3_var = entry(10, "Command 3", "extra_cmd3",
                                hint="Optional third command (e.g. a Cloudflare tunnel)")
        self.workdir_var = entry(12, "Working directory", "workdir",
                                 hint="Blank = script folder (required if no script)", browse=self._browse_workdir)
        self.port_var = entry(14, "Port (optional)", "port",
                              hint="Override health-check port; auto-detected if blank")

        self.auto_var = tk.BooleanVar(value=program.get("autostart", False) if program else False)
        self.restart_var = tk.BooleanVar(value=program.get("auto_restart", False) if program else False)
        cbframe = tk.Frame(self, bg=BG2)
        cbframe.grid(row=16, column=0, columnspan=2, sticky="w", padx=14, pady=6)
        tk.Checkbutton(cbframe, text="Auto-start on launch", variable=self.auto_var,
                       bg=BG2, fg=TEXT_DIM, font=FONT_SMALL, selectcolor=BG3,
                       activebackground=BG2, activeforeground=TEXT).pack(side="left", padx=(0, 16))
        tk.Checkbutton(cbframe, text="Auto-restart on crash", variable=self.restart_var,
                       bg=BG2, fg=TEXT_DIM, font=FONT_SMALL, selectcolor=BG3,
                       activebackground=BG2, activeforeground=TEXT).pack(side="left")

        btns = tk.Frame(self, bg=BG2)
        btns.grid(row=17, column=0, columnspan=2, pady=(10, 14))
        tk.Button(btns, text="Cancel", bg=BG3, fg=TEXT_DIM, font=FONT_MAIN, relief="flat",
                  bd=0, padx=20, pady=6, cursor="hand2", command=self.destroy).pack(side="left", padx=6)
        tk.Button(btns, text="Save", bg=ACCENT, fg="#000", font=FONT_TITLE, relief="flat",
                  bd=0, padx=20, pady=6, cursor="hand2", command=self._save).pack(side="left", padx=6)

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x,0)}+{max(y,0)}")

    def _browse(self):
        p = filedialog.askopenfilename(title="Select Python script",
                                       filetypes=[("Python files", "*.py *.pyw"), ("All files", "*.*")])
        if p:
            self.path_var.set(p)
            if not self.name_var.get():
                self.name_var.set(os.path.splitext(os.path.basename(p))[0])

    def _browse_interp(self):
        p = filedialog.askopenfilename(title="Select Python interpreter",
                                       filetypes=[("Python", "python*.exe pythonw*.exe"), ("All", "*.*")])
        if p:
            self.interp_var.set(p)

    def _browse_workdir(self):
        p = filedialog.askdirectory(title="Select working directory")
        if p:
            self.workdir_var.set(p)

    def _save(self):
        name = self.name_var.get().strip()
        path = self.path_var.get().strip()
        extra = self.extra_var.get().strip()
        workdir = self.workdir_var.get().strip()
        if not name:
            messagebox.showwarning("Missing fields", "Name is required.", parent=self); return
        if not path and not extra:
            messagebox.showwarning("Missing fields",
                                   "Provide a Python script, an Extra command, or both.", parent=self); return
        if not path and not workdir:
            messagebox.showwarning("Missing working directory",
                                   "A working directory is required when no script is set.", parent=self); return
        # Start from the existing program so custom keys (tunnel_watch,
        # tunnel_health_url, …) are preserved across GUI edits.
        result = dict(self.program) if self.program else {}
        result.update({
            "name": name, "path": path, "description": self.desc_var.get().strip(),
            "interpreter": self.interp_var.get().strip(), "extra_cmd": extra,
            "extra_cmd2": self.extra2_var.get().strip(),
            "extra_cmd3": self.extra3_var.get().strip(), "workdir": workdir,
            "port": self.port_var.get().strip(),
            "autostart": self.auto_var.get(), "auto_restart": self.restart_var.get(),
        })
        self.result = result
        self.destroy()

# ─── Diagnostics window ──────────────────────────────────────────────────────────
class ChecklistWindow(tk.Toplevel):
    def __init__(self, parent, title, sections):
        """sections: list of (heading, [(label, level, detail), ...])"""
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG2)
        self.grab_set()
        self.geometry("720x520")

        tk.Label(self, text=title, bg=BG2, fg=ACCENT, font=("Consolas", 13, "bold"))\
            .pack(anchor="w", padx=16, pady=(12, 6))

        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=12, pady=6)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview, bg=BG2, troughcolor=BG)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=BG)
        wid = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(wid, width=e.width))

        colors = {OK: GREEN, WARN: YELLOW, FAIL: RED, INFO: BLUE}
        marks = {OK: "✓", WARN: "!", FAIL: "✕", INFO: "•"}
        tallies = {OK: 0, WARN: 0, FAIL: 0, INFO: 0}

        for heading, rows in sections:
            tk.Label(body, text=heading, bg=BG, fg=TEXT, font=FONT_TITLE, anchor="w")\
                .pack(fill="x", padx=8, pady=(12, 2))
            for label, level, detail in rows:
                tallies[level] = tallies.get(level, 0) + 1
                row = tk.Frame(body, bg=BG)
                row.pack(fill="x", padx=8, pady=1)
                tk.Label(row, text=marks.get(level, "•"), bg=BG, fg=colors.get(level, TEXT),
                         font=FONT_TITLE, width=2).pack(side="left")
                tk.Label(row, text=label, bg=BG, fg=TEXT, font=FONT_SMALL, width=16, anchor="w")\
                    .pack(side="left")
                tk.Label(row, text=detail, bg=BG, fg=TEXT_DIM, font=FONT_SMALL, anchor="w",
                         justify="left", wraplength=460).pack(side="left", fill="x", expand=True)

        summary = (f"  {tallies[OK]} ok   ·   {tallies[WARN]} warnings   ·   "
                   f"{tallies[FAIL]} failed   ·   {tallies[INFO]} info")
        bar = tk.Label(self, text=summary, bg=BG3,
                       fg=(RED if tallies[FAIL] else (YELLOW if tallies[WARN] else GREEN)),
                       font=FONT_MAIN, anchor="w", padx=10)
        bar.pack(fill="x", side="bottom")

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x,0)}+{max(y,0)}")

# ─── Program card ────────────────────────────────────────────────────────────────
class ProgramCard(tk.Frame):
    def __init__(self, parent, program, app):
        super().__init__(parent, bg=BG2)
        self.program = program
        self.app = app
        self.manager = app.manager
        self.log_visible = False
        self._build()
        self._poll()
        if self.program.get("autostart"):
            self.after(400, self._start)

    def _build(self):
        self.configure(highlightthickness=1, highlightbackground=BORDER)

        header = tk.Frame(self, bg=BG2)
        header.pack(fill="x", padx=12, pady=(10, 4))

        self.dot_canvas = tk.Canvas(header, width=12, height=12, bg=BG2, highlightthickness=0)
        self.dot_canvas.pack(side="left", padx=(0, 8))
        self.dot = self.dot_canvas.create_oval(1, 1, 11, 11, fill=RED, outline="")

        info = tk.Frame(header, bg=BG2)
        info.pack(side="left", fill="x", expand=True)
        self.name_lbl = tk.Label(info, text=self.program["name"], bg=BG2, fg=TEXT,
                                 font=FONT_TITLE, anchor="w")
        self.name_lbl.pack(fill="x")
        desc = self.program.get("description", "")
        self.desc_lbl = tk.Label(info, text=desc, bg=BG2, fg=TEXT_DIM, font=FONT_SMALL, anchor="w")
        if desc:
            self.desc_lbl.pack(fill="x")

        btns = tk.Frame(header, bg=BG2)
        btns.pack(side="right")

        def mkbtn(txt, fg, cmd, tip=""):
            b = tk.Button(btns, text=txt, bg=BG3, fg=fg, font=FONT_SMALL, relief="flat",
                          bd=0, padx=7, pady=4, cursor="hand2", command=cmd)
            b.pack(side="right", padx=(4, 0))
            return b

        self.toggle_btn = tk.Button(btns, text="▶  START", bg=ACCENT, fg="#000", font=FONT_TITLE,
                                    relief="flat", bd=0, padx=14, pady=4, cursor="hand2",
                                    command=self._toggle)
        self.toggle_btn.pack(side="right", padx=(8, 0))
        mkbtn("⟳", BLUE, self._restart)
        mkbtn("✕", RED, self._remove)
        mkbtn("✎", TEXT_DIM, self._edit)
        mkbtn("📁", TEXT_DIM, self._open_folder)
        mkbtn("⧉", TEXT_DIM, self._copy_cmd)
        self.log_btn = mkbtn("LOG", TEXT_DIM, self._toggle_log)

        # Metrics line
        self.metrics_lbl = tk.Label(self, text="", bg=BG2, fg=TEXT_DIM, font=FONT_SMALL, anchor="w")
        self.metrics_lbl.pack(fill="x", padx=12, pady=(0, 2))

        # ngrok / url line (click to open)
        self.url_lbl = tk.Label(self, text="", bg=BG2, fg=BLUE, font=FONT_SMALL, anchor="w", cursor="hand2")
        self.url_lbl.pack(fill="x", padx=12)
        self.url_lbl.bind("<Button-1>", self._open_url)
        self._url = ""

        # Command line
        self.status_lbl = tk.Label(self, text=self._cmd_text(), bg=BG2, fg=TEXT_DIMMER,
                                   font=FONT_TINY, anchor="w")
        self.status_lbl.pack(fill="x", padx=12, pady=(0, 6))

        # Log panel
        self.log_frame = tk.Frame(self, bg=BG)
        toolbar = tk.Frame(self.log_frame, bg=BG)
        toolbar.pack(fill="x")
        tk.Button(toolbar, text="clear", bg=BG, fg=TEXT_DIM, font=FONT_TINY, relief="flat",
                  bd=0, padx=6, cursor="hand2", command=self._clear_log).pack(side="right", padx=4, pady=2)
        self.log_text = tk.Text(self.log_frame, bg=BG, fg=TEXT_DIM, font=FONT_LOG, relief="flat",
                                bd=0, height=9, wrap="word", state="disabled", insertbackground=ACCENT)
        scroll = tk.Scrollbar(self.log_frame, command=self.log_text.yview, bg=BG2, troughcolor=BG)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True, padx=8, pady=(0, 6))
        self.log_text.tag_config("TS", foreground=TEXT_DIMMER)
        self.log_text.tag_config("OUT", foreground="#a8d8a8")
        self.log_text.tag_config("ERR", foreground="#e8a8a8")
        self.log_text.tag_config("CMD", foreground="#a8c8e8")
        self.log_text.tag_config("SYS", foreground=ACCENT)

    # ── command/label helpers ──
    @staticmethod
    def _redact(cmd):
        """Hide secrets (e.g. --token <...>) when showing a command in the UI."""
        if not cmd:
            return ""
        return re.sub(r'(--token[=\s]+)\S+', r'\1****', cmd)

    def _cmd_text(self):
        p = self.program
        base = p.get("path") or f"[cmd only]  {p.get('workdir', '')}"
        e1 = f"  +  {self._redact(p.get('extra_cmd'))}" if p.get("extra_cmd") else ""
        e2 = f"  +  {self._redact(p.get('extra_cmd2'))}" if p.get("extra_cmd2") else ""
        e3 = f"  +  {self._redact(p.get('extra_cmd3'))}" if p.get("extra_cmd3") else ""
        return f"  {base}{e1}{e2}{e3}"

    # ── actions ──
    def _toggle(self):
        if self.manager.is_running(self.program["name"]):
            self._stop()
        else:
            self._start()

    def _start(self):
        p = self.program
        ok, msg = self.manager.start(
            p["name"], p.get("path", ""),
            interpreter=p.get("interpreter") or None,
            extra_cmd=p.get("extra_cmd") or None,
            extra_cmd2=p.get("extra_cmd2") or None,
            extra_cmd3=p.get("extra_cmd3") or None,
            workdir=p.get("workdir") or None,
        )
        self.manager.restart_counts[p["name"]] = 0
        self._log_sys("Process started" if ok else f"ERROR: {msg}")
        if not ok:
            messagebox.showerror("Start failed", msg)

    def _stop(self):
        ok, msg = self.manager.stop(self.program["name"])
        self._log_sys("Process stopped" if ok else f"Stop: {msg}")

    def _restart(self):
        self._log_sys("Restarting…")
        self.manager.stop(self.program["name"])
        self.after(700, self._start)

    def _edit(self):
        self.app.edit_card(self)

    def _remove(self):
        self.app.remove_card(self)

    def _open_folder(self):
        p = self.program
        target = p.get("workdir") or (os.path.dirname(p.get("path", "")) if p.get("path") else "")
        if target and os.path.isdir(target):
            try:
                os.startfile(target)
            except Exception as e:
                messagebox.showerror("Open folder", str(e))
        else:
            messagebox.showinfo("Open folder", "No valid folder for this program.")

    def _copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append(self._cmd_text().strip())
        self.app._set_status(f"Copied command for {self.program['name']}")

    def _open_url(self, _=None):
        if self._url:
            webbrowser.open(self._url)

    def _toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.pack(fill="x", padx=12, pady=(0, 10))
            self.log_btn.configure(fg=ACCENT)
        else:
            self.log_frame.pack_forget()
            self.log_btn.configure(fg=TEXT_DIM)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log_sys(self, text):
        self._append_log("SYS", f"[SYS] {text}")

    def _append_log(self, tag, line):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts()}] ", "TS")
        self.log_text.insert("end", line + "\n", tag)
        # cap log length
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    # ── periodic UI update ──
    def _poll(self):
        name = self.program["name"]
        running = self.manager.is_running(name)
        self.dot_canvas.itemconfig(self.dot, fill=GREEN if running else RED)
        if running:
            self.toggle_btn.configure(text="■  STOP", bg=RED, fg=TEXT)
        else:
            self.toggle_btn.configure(text="▶  START", bg=ACCENT, fg="#000")

        # metrics
        port = detect_port(self.program)
        with self.app.health_lock:
            port_up = self.app.health_ports.get(port) if port else None
            tunnel = self.app.health_tunnels.get(port) if port else None

        parts = []
        if running:
            pids = self.manager.pids(name)
            parts.append("● running")
            if pids:
                parts.append("PID " + ",".join(str(x) for x in pids))
            parts.append("up " + fmt_uptime(self.manager.uptime(name)))
        else:
            code = self.manager.exit_codes.get(name)
            parts.append("○ stopped" + (f" (exit {code})" if code not in (None, 0) else ""))
        if port:
            if port_up is True:
                parts.append(f":{port} LISTENING")
            elif port_up is False:
                parts.append(f":{port} closed")
            else:
                parts.append(f":{port}")
        if self.program.get("auto_restart"):
            parts.append("⟲ auto")
        self.metrics_lbl.configure(
            text="  " + "   ·   ".join(parts),
            fg=GREEN if running else TEXT_DIM)

        # ngrok url
        if tunnel:
            self._url = tunnel
            self.url_lbl.configure(text=f"  🌐 {tunnel}")
            if not self.url_lbl.winfo_ismapped():
                self.url_lbl.pack(fill="x", padx=12, before=self.status_lbl)
        else:
            self._url = ""
            self.url_lbl.configure(text="")

        # drain logs
        q = self.manager.get_log_queue(name)
        if q:
            try:
                for _ in range(200):
                    tag, line = q.get_nowait()
                    if tag != "EXIT":
                        self._append_log(tag, line)
            except queue.Empty:
                pass

        self.after(500, self._poll)

    def refresh_program(self, program):
        self.program = program
        self.name_lbl.configure(text=program["name"])
        self.status_lbl.configure(text=self._cmd_text())
        desc = program.get("description", "")
        self.desc_lbl.configure(text=desc)

    def diagnostics(self):
        return program_diagnostics(self.program)

# ─── Main app ────────────────────────────────────────────────────────────────────
class LauncherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PyLauncher Pro")
        self.configure(bg=BG)
        self.geometry("900x680")
        self.minsize(680, 460)
        self.manager = ProcessManager()
        self.config_data = load_config()
        self.cards = {}
        self.filter_text = ""

        self.health_lock = threading.Lock()
        self.health_ports = {}
        self.health_tunnels = {}
        self.tunnel_fail = {}      # name -> consecutive failed tunnel health checks

        self._build()
        self._load_cards()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.monitor = HealthMonitor(self)
        self.monitor.start()
        self.after(3000, self._watchdog)

    def _build(self):
        topbar = tk.Frame(self, bg=BG2, height=52)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)
        tk.Label(topbar, text="⚡ PyLauncher Pro", bg=BG2, fg=ACCENT,
                 font=("Consolas", 13, "bold")).pack(side="left", padx=14)

        def tbtn(txt, fg, cmd):
            tk.Button(topbar, text=txt, bg=BG3, fg=fg, font=FONT_MAIN, relief="flat", bd=0,
                      padx=10, pady=6, cursor="hand2", command=cmd).pack(side="right", padx=3, pady=8)

        tk.Button(topbar, text="+ Add", bg=ACCENT, fg="#000", font=FONT_TITLE, relief="flat",
                  bd=0, padx=12, pady=6, cursor="hand2",
                  command=self._add_program).pack(side="right", padx=(3, 12), pady=8)
        tbtn("🩺 System Check", PURPLE, self._system_check)
        tbtn("✓ Diagnose All", BLUE, self._diagnose_all)
        tbtn("⟳ Restart All", YELLOW, self._restart_all)
        tbtn("■ Stop All", RED, self._stop_all)
        tbtn("▶ Start All", GREEN, self._start_all)

        # search bar
        searchbar = tk.Frame(self, bg=BG2)
        searchbar.pack(fill="x")
        tk.Label(searchbar, text="  🔎", bg=BG2, fg=TEXT_DIM, font=FONT_MAIN).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        tk.Entry(searchbar, textvariable=self.search_var, bg=BG3, fg=TEXT, font=FONT_SMALL,
                 insertbackground=ACCENT, relief="flat", highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT).pack(
            side="left", fill="x", expand=True, padx=8, pady=6)
        self.count_lbl = tk.Label(searchbar, text="", bg=BG2, fg=TEXT_DIM, font=FONT_TINY)
        self.count_lbl.pack(side="right", padx=12)

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=self.canvas.yview, bg=BG2, troughcolor=BG)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.cards_frame = tk.Frame(self.canvas, bg=BG)
        self.window_id = self.canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.cards_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.window_id, width=e.width))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        self.empty_lbl = tk.Label(self.cards_frame,
                                  text="No programs yet.\nClick  '+ Add'  to get started.",
                                  bg=BG, fg=TEXT_DIMMER, font=("Consolas", 11), justify="center")

        self.statusbar = tk.Label(self, text="  Ready", bg=BG3, fg=TEXT_DIM, font=FONT_SMALL,
                                  anchor="w", padx=10)
        self.statusbar.pack(fill="x", side="bottom")

    def _load_cards(self):
        for prog in self.config_data.get("programs", []):
            self._add_card(prog)
        self._apply_filter()

    def _add_card(self, program):
        card = ProgramCard(self.cards_frame, program, self)
        self.cards[program["name"]] = card
        self._apply_filter()

    def _apply_filter(self):
        q = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        shown = 0
        for name, card in self.cards.items():
            hay = (name + " " + card.program.get("description", "") + " " +
                   card.program.get("path", "")).lower()
            if not q or q in hay:
                card.pack(fill="x", padx=14, pady=(10, 0))
                shown += 1
            else:
                card.pack_forget()
        if not self.cards:
            self.empty_lbl.pack(expand=True, pady=60)
        else:
            self.empty_lbl.pack_forget()
        self.count_lbl.configure(text=f"{shown}/{len(self.cards)} shown")

    def _add_program(self):
        dlg = ProgramDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            prog = dlg.result
            if prog["name"] in [p["name"] for p in self.config_data["programs"]]:
                messagebox.showerror("Duplicate name", f"'{prog['name']}' already exists.")
                return
            self.config_data["programs"].append(prog)
            save_config(self.config_data)
            self._add_card(prog)
            self._set_status(f"Added: {prog['name']}")

    def edit_card(self, card):
        dlg = ProgramDialog(self, program=card.program.copy())
        self.wait_window(dlg)
        if dlg.result:
            old = card.program["name"]
            new = dlg.result
            for i, p in enumerate(self.config_data["programs"]):
                if p["name"] == old:
                    self.config_data["programs"][i] = new
                    break
            save_config(self.config_data)
            if old != new["name"]:
                self.cards.pop(old, None)
                self.cards[new["name"]] = card
            card.refresh_program(new)
            self._apply_filter()
            self._set_status(f"Updated: {new['name']}")

    def remove_card(self, card):
        name = card.program["name"]
        if not messagebox.askyesno("Remove", f"Remove '{name}'?\n(Files are not deleted.)", parent=self):
            return
        if self.manager.is_running(name):
            self.manager.stop(name)
        self.config_data["programs"] = [p for p in self.config_data["programs"] if p["name"] != name]
        save_config(self.config_data)
        self.cards.pop(name, None)
        card.destroy()
        self._apply_filter()
        self._set_status(f"Removed: {name}")

    def _start_all(self):
        for prog in self.config_data.get("programs", []):
            card = self.cards.get(prog["name"])
            if card and not self.manager.is_running(prog["name"]):
                card._start()
        self._set_status("Started all")

    def _stop_all(self):
        for prog in self.config_data.get("programs", []):
            if self.manager.is_running(prog["name"]):
                self.manager.stop(prog["name"])
        self._set_status("Stopped all")

    def _restart_all(self):
        for prog in self.config_data.get("programs", []):
            card = self.cards.get(prog["name"])
            if card:
                card._restart()
        self._set_status("Restarting all")

    def _diagnose_all(self):
        self._set_status("Running diagnostics…")
        self.update_idletasks()
        sections = []
        for prog in self.config_data.get("programs", []):
            sections.append((prog["name"], program_diagnostics(prog)))
        if not sections:
            sections = [("No programs", [("nothing to check", INFO, "add a program first")])]
        ChecklistWindow(self, "Diagnostics — all programs", sections)
        self._set_status("Diagnostics complete")

    def _system_check(self):
        self._set_status("Checking system…")
        self.update_idletasks()
        tools = system_check()
        # ngrok configs referenced by programs
        cfg_rows = []
        seen = set()
        for prog in self.config_data.get("programs", []):
            for cmd in (prog.get("extra_cmd", ""), prog.get("extra_cmd2", ""), prog.get("extra_cmd3", "")):
                m = re.search(r'--config=(?:"([^"]+)"|(\S+))', cmd or "")
                if m:
                    cfg = m.group(1) or m.group(2)
                    if cfg and cfg not in seen:
                        seen.add(cfg)
                        cfg_rows.append((os.path.basename(cfg), OK if os.path.isfile(cfg) else FAIL,
                                         cfg if os.path.isfile(cfg) else f"missing: {cfg}"))
        # ngrok runtime
        tun = get_ngrok_tunnels()
        tun_rows = ([(f"port {k}", OK, v) for k, v in sorted(tun.items())]
                    if tun else [("ngrok tunnels", INFO, "none active (start ngrok to see public URLs)")])
        sections = [
            ("Tools on PATH", tools),
            ("ngrok configs referenced", cfg_rows or [("none", INFO, "no --config= references")]),
            ("Active ngrok tunnels", tun_rows),
            ("Config file", [("launcher_config.json", OK, CONFIG_FILE)]),
        ]
        ChecklistWindow(self, "System Check", sections)
        self._set_status("System check complete")

    def _watchdog(self):
        """Auto-restart crashed programs (auto_restart) and wedged tunnels (tunnel_watch).

        A crashed program is one whose process has exited. A *wedged* tunnel is the
        harder case: the process is still alive but its public tunnel has silently
        died (e.g. ngrok/cloudflared lost the edge and never recovered). For those,
        a program can set  "tunnel_watch": true  and  "tunnel_health_url": <url>
        (a local health endpoint such as cloudflared's metrics /ready), and we
        restart it when that endpoint stays unreachable while the process runs.
        """
        for prog in self.config_data.get("programs", []):
            name = prog["name"]
            if not self.manager.intended.get(name):
                self.tunnel_fail.pop(name, None)
                continue
            running = self.manager.is_running(name)

            # (1) Process crash — restart if auto_restart is enabled.
            if prog.get("auto_restart") and not running:
                cnt = self.manager.restart_counts.get(name, 0)
                if cnt >= 10:
                    self.manager.intended[name] = False
                    card = self.cards.get(name)
                    if card:
                        card._log_sys("Auto-restart disabled after 10 attempts")
                    continue
                self.manager.restart_counts[name] = cnt + 1
                card = self.cards.get(name)
                if card:
                    card._log_sys(f"Crash detected — auto-restarting (#{cnt + 1})")
                    card._start()
                continue

            # (2) Partial failure — the process group is "alive" (e.g. the tunnel
            #     is still up) but the app itself is unhealthy. This catches the
            #     common case where the node server dies behind a live ngrok /
            #     cloudflared tunnel, which is_running() cannot see.
            #     "health_urls" lists endpoints that must all answer 2xx-4xx:
            #     typically the local app AND the tunnel's edge /ready endpoint.
            if prog.get("tunnel_watch") and running:
                urls = prog.get("health_urls")
                if not urls and prog.get("tunnel_health_url"):
                    urls = [prog["tunnel_health_url"]]
                uptime = self.manager.uptime(name) or 0
                # Skip during the first 45s so we don't fight process/tunnel startup.
                if urls and uptime > 45:
                    healthy = True
                    for u in urls:
                        code = probe_url(u)
                        if not (code and 200 <= code < 500):
                            healthy = False
                            break
                    if healthy:
                        self.tunnel_fail[name] = 0
                    else:
                        n = self.tunnel_fail.get(name, 0) + 1
                        self.tunnel_fail[name] = n
                        if n >= 4:   # ~4 checks x 3s ≈ 12s confirmed down
                            self.tunnel_fail[name] = 0
                            card = self.cards.get(name)
                            if card:
                                card._log_sys(f"Health check failing ({u}) — restarting")
                                card._restart()
        self.after(3000, self._watchdog)

    def _set_status(self, msg):
        self.statusbar.configure(text=f"  {msg}")
        self.after(4000, lambda: self.statusbar.configure(text="  Ready"))

    def _on_close(self):
        running = [n for n in self.cards if self.manager.is_running(n)]
        if running:
            if messagebox.askyesno("Running programs",
                                   f"{len(running)} program(s) still running.\nStop them and exit?"):
                for name in running:
                    self.manager.stop(name)
                self.destroy()
        else:
            self.destroy()

# ─── Entry ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = LauncherApp()
    app.mainloop()
