"""Agent driver for the Croissant Stock Analyser dashboard (Dash web app).

Stdlib-only launch / smoke / screenshot / stop harness. Dash is a React SPA,
so `GET /` only proves the shell serves. The real health signal is the JSON
API Dash exposes:

  GET  /_dash-layout            -> serialized component tree (200 = booted)
  GET  /_dash-dependencies      -> callback graph (non-empty = callbacks registered)
  POST /_dash-update-component  -> fires a server callback; we fire an i18n
                                   callback with user-language="zh" and assert
                                   CJK characters come back, proving the
                                   callback graph actually executes.

Usage (from repo root, using the project venv python):

  venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py            # full cycle
  venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py launch
  venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py smoke
  venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py screenshot
  venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py stop

Defaults to port 8051 so agent smoke runs never collide with a manually
running dashboard on 8050. Exit code 0 = pass, 1 = fail.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

SKILL_DIR = Path(__file__).resolve().parent
REPO_ROOT = SKILL_DIR.parents[2]          # .claude/skills/run-*/ -> repo root
PID_FILE = SKILL_DIR / ".driver.pid"
LOG_FILE = SKILL_DIR / ".driver.log"
DEFAULT_PORT = 8051
BOOT_TIMEOUT_S = 90

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _get(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    req = Request(url, headers={"User-Agent": "run-skill-driver/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _venv_python() -> str:
    exe = REPO_ROOT / "venv" / "Scripts" / "python.exe"
    return str(exe) if exe.exists() else sys.executable


# ---------------------------------------------------------------- launch ---

def cmd_launch(port: int) -> int:
    base = f"http://localhost:{port}"
    # Refuse to double-launch
    try:
        status, _ = _get(f"{base}/_dash-layout", timeout=2)
        print(f"Already serving on {port} (HTTP {status}) — not relaunching.")
        return 0
    except (URLError, TimeoutError, ConnectionError, OSError):
        pass

    env = dict(os.environ)
    env["SKIP_DASHBOARD_PREWARM"] = "true"   # skip Supabase warm-up: faster boot
    env["PYTHONIOENCODING"] = "utf-8"        # Chinese strings vs cp1252 console

    log = open(LOG_FILE, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_venv_python(), "main.py", "dashboard", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    PID_FILE.write_text(str(proc.pid), encoding="ascii")
    print(f"Spawned dashboard PID {proc.pid} on port {port}; polling /_dash-layout ...")

    deadline = time.time() + BOOT_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"FAIL: dashboard exited early (code {proc.returncode}). "
                  f"Last log lines:")
            _tail_log()
            return 1
        try:
            status, _ = _get(f"{base}/_dash-layout", timeout=3)
            if 200 <= status < 300:
                print(f"READY in {BOOT_TIMEOUT_S - (deadline - time.time()):.1f}s "
                      f"-> {base}")
                return 0
        except (URLError, TimeoutError, ConnectionError, OSError):
            pass
        time.sleep(1.0)

    print(f"FAIL: not ready within {BOOT_TIMEOUT_S}s. Last log lines:")
    _tail_log()
    return 1


def _tail_log(n: int = 30) -> None:
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-n:]:
            print("  |", line)
    except OSError:
        print("  | (no log)")


# ----------------------------------------------------------------- smoke ---

def _has_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


def cmd_smoke(port: int) -> int:
    base = f"http://localhost:{port}"
    failures = 0

    # 1. Layout serves and is JSON
    status, body = _get(f"{base}/_dash-layout")
    layout_ok = status == 200 and body.lstrip()[:1] in (b"{", b"[")
    print(f"[{'ok' if layout_ok else 'FAIL'}] GET /_dash-layout "
          f"-> {status}, {len(body):,} bytes")
    failures += 0 if layout_ok else 1

    # 2. Callback graph is registered and non-trivial
    status, body = _get(f"{base}/_dash-dependencies")
    deps = json.loads(body)
    deps_ok = status == 200 and isinstance(deps, list) and len(deps) > 50
    print(f"[{'ok' if deps_ok else 'FAIL'}] GET /_dash-dependencies "
          f"-> {status}, {len(deps)} callbacks")
    failures += 0 if deps_ok else 1

    # 3. Fire a real i18n callback with lang="zh"; assert CJK in the response.
    #    Find a SERVER callback whose inputs are exactly user-language
    #    (+user-market) so we can satisfy it fully without other component
    #    state. Outputs containing '@<hash>' are clientside callbacks — they
    #    run in the browser and 500 if POSTed to _dash-update-component, so
    #    skip them. Among candidates prefer the widest output bundle (the
    #    per-tab i18n callbacks with 30-80 outputs).
    candidates = []
    for dep in deps:
        if "@" in dep["output"]:
            continue                      # clientside — not server-dispatchable
        input_ids = {i["id"] for i in dep.get("inputs", [])}
        if "user-language" in input_ids and input_ids <= {"user-language",
                                                            "user-market"} \
                and not dep.get("state"):
            candidates.append(dep)
    target = max(candidates, key=lambda d: d["output"].count("..."),
                 default=None)
    if target is None:
        print("[FAIL] no pure i18n callback found in /_dash-dependencies")
        return failures + 1

    inputs = []
    for i in target["inputs"]:
        value = "zh" if i["id"] == "user-language" else "HK"
        inputs.append({"id": i["id"], "property": i["property"], "value": value})
    payload = json.dumps({
        "output": target["output"],
        "outputs": _parse_output_spec(target["output"]),
        "inputs": inputs,
        "changedPropIds": ["user-language.data"],
        "state": [],
    }).encode("utf-8")

    req = Request(f"{base}/_dash-update-component", data=payload,
                  headers={"Content-Type": "application/json",
                           "User-Agent": "run-skill-driver/1.0"})
    with urlopen(req, timeout=30) as resp:
        cb_status, cb_body = resp.status, resp.read().decode("utf-8")
    # Dash JSON-escapes non-ASCII (筛...), so decode before scanning.
    try:
        cb_text = json.dumps(json.loads(cb_body), ensure_ascii=False)
    except ValueError:
        cb_text = cb_body
    cb_ok = cb_status == 200 and _has_cjk(cb_text)
    n_outputs = len(target["output"].split("..")) if target["output"].startswith("..") else 1
    print(f"[{'ok' if cb_ok else 'FAIL'}] POST /_dash-update-component "
          f"(i18n callback, {n_outputs} outputs, lang=zh) -> {cb_status}, "
          f"CJK present: {_has_cjk(cb_text)}")
    failures += 0 if cb_ok else 1

    print("SMOKE", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 1 if failures else 0


def _parse_output_spec(output: str) -> list[dict]:
    """Dash encodes multi-output keys as '..id1.prop1...id2.prop2..'."""
    parts = output.strip(".").split("...") if output.startswith("..") else [output]
    specs = []
    for part in parts:
        comp_id, _, prop = part.rpartition(".")
        specs.append({"id": comp_id, "property": prop})
    return specs


# ------------------------------------------------------------ screenshot ---
#
# Chrome's one-shot `--headless --screenshot` modes DON'T work on this app:
#   --timeout=N            captures at the load event -> "Loading..." shell,
#                          React hasn't hydrated yet.
#   --virtual-time-budget  hangs indefinitely: page-load fires ~100 Dash
#                          callbacks, each pending XHR pauses virtual time.
# So we drive Chrome via CDP instead: launch with --remote-debugging-port,
# wait (real time) until the dashboard's h5 brand title exists in the DOM,
# then Page.captureScreenshot. The tiny WebSocket client below is stdlib-only.

import base64
import secrets
import socket
import struct


class _WS:
    """Minimal RFC6455 client for localhost CDP (text frames only)."""

    def __init__(self, ws_url: str):
        # ws://127.0.0.1:PORT/devtools/page/ID
        rest = ws_url.split("://", 1)[1]
        hostport, _, self.path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((
            f"GET /{self.path} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WS handshake failed: {resp[:120]!r}")

    def send_json(self, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        mask = secrets.token_bytes(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WS closed")
            buf += chunk
        return buf

    def recv_json(self) -> dict:
        message = b""
        while True:
            b1, b2 = self._read_exact(2)
            opcode, ln = b1 & 0x0F, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read_exact(8))[0]
            payload = self._read_exact(ln)          # server frames unmasked
            if opcode == 0x9:                        # ping -> pong
                mask = secrets.token_bytes(4)
                pong = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                self.sock.sendall(struct.pack("!BB", 0x8A, 0x80 | len(payload))
                                  + mask + pong)
                continue
            message += payload
            if b1 & 0x80:                            # FIN
                return json.loads(message)

    def call(self, msg_id: int, method: str, params: dict | None = None) -> dict:
        self.send_json({"id": msg_id, "method": method, "params": params or {}})
        while True:
            msg = self.recv_json()
            if msg.get("id") == msg_id:
                return msg

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def cmd_screenshot(port: int, out: str) -> int:
    import tempfile
    from urllib.parse import quote

    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    if browser is None:
        print("FAIL: no Edge/Chrome found in standard locations.")
        return 1
    out_path = Path(out).resolve()
    profile_dir = tempfile.mkdtemp(prefix="driver-shot-")
    # --user-data-dir is REQUIRED: without it headless attaches to the user's
    # running browser profile and hangs. --remote-debugging-port=0 auto-picks
    # a free port and writes it to <profile>/DevToolsActivePort.
    proc = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu",
         f"--user-data-dir={profile_dir}", "--no-first-run",
         "--disable-extensions", "--remote-debugging-port=0",
         "--window-size=1600,1200", "--hide-scrollbars", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for the DevTools port file
        port_file = Path(profile_dir) / "DevToolsActivePort"
        deadline = time.time() + 30
        while not port_file.exists() and time.time() < deadline:
            time.sleep(0.3)
        cdp_port = int(port_file.read_text().splitlines()[0])

        # Open the dashboard in a new target (PUT required on Chrome 114+)
        req = Request(f"http://127.0.0.1:{cdp_port}/json/new?"
                      f"{quote(f'http://localhost:{port}', safe='')}",
                      method="PUT")
        with urlopen(req, timeout=10) as resp:
            target = json.loads(resp.read())
        ws = _WS(target["webSocketDebuggerUrl"])

        # Poll until React has hydrated: the header brand text appears.
        # (No h1-h5 element wraps it — check innerText, not a selector.)
        ready_expr = ("document.body && "
                      "document.body.innerText.includes('Croissant') && "
                      "document.body.innerText.length > 500")
        ws.call(1, "Runtime.enable")
        hydrated = False
        deadline = time.time() + 60
        msg_id = 10
        while time.time() < deadline:
            result = ws.call(msg_id, "Runtime.evaluate",
                             {"expression": ready_expr, "returnByValue": True})
            msg_id += 1
            if result.get("result", {}).get("result", {}).get("value") is True:
                hydrated = True
                break
            time.sleep(1.0)
        if not hydrated:
            print("[FAIL] page never hydrated within 60s")
            return 1

        # First visit shows a "Welcome" modal (market + language picker) that
        # covers the dashboard. Click Confirm to dismiss before capturing.
        ws.call(msg_id, "Runtime.evaluate", {
            "expression": "document.getElementById('startup-confirm-btn')"
                          "?.click()"})
        msg_id += 1
        # Confirm triggers a Market-tab reload (price fetch + KPI recompute).
        # Poll for the chart header ("... trading days") rather than sleeping
        # a fixed amount; fall through and capture anyway on timeout.
        deadline = time.time() + 45
        while time.time() < deadline:
            result = ws.call(msg_id, "Runtime.evaluate", {
                "expression": "document.body.innerText.includes('trading days')",
                "returnByValue": True})
            msg_id += 1
            if result.get("result", {}).get("result", {}).get("value") is True:
                break
            time.sleep(1.5)
        time.sleep(2.0)   # final paint settle

        shot = ws.call(msg_id, "Page.captureScreenshot", {"format": "png"})
        ws.close()
        out_path.write_bytes(base64.b64decode(shot["result"]["data"]))
    finally:
        proc.kill()

    ok = out_path.exists() and out_path.stat().st_size > 20_000
    print(f"[{'ok' if ok else 'FAIL'}] screenshot -> {out_path} "
          f"({out_path.stat().st_size:,} bytes)")
    return 0 if ok else 1


# ------------------------------------------------------------------ stop ---

def cmd_stop(port: int) -> int:
    if not PID_FILE.exists():
        print("No pidfile — nothing to stop.")
        return 0
    pid = PID_FILE.read_text(encoding="ascii").strip()
    subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                   capture_output=True)
    PID_FILE.unlink(missing_ok=True)
    time.sleep(1.0)
    try:
        _get(f"http://localhost:{port}/_dash-layout", timeout=2)
        print(f"FAIL: port {port} still serving after kill of PID {pid}.")
        return 1
    except (URLError, TimeoutError, ConnectionError, OSError):
        print(f"Stopped PID {pid}; port {port} free.")
        return 0


# ------------------------------------------------------------------ main ---

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", nargs="?", default="cycle",
                   choices=["cycle", "launch", "smoke", "screenshot", "stop"])
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--out", default=str(SKILL_DIR / "shot.png"),
                   help="screenshot output path")
    args = p.parse_args()

    if args.command == "launch":
        return cmd_launch(args.port)
    if args.command == "smoke":
        return cmd_smoke(args.port)
    if args.command == "screenshot":
        return cmd_screenshot(args.port, args.out)
    if args.command == "stop":
        return cmd_stop(args.port)

    # Full cycle: launch -> smoke -> screenshot -> stop (always stop).
    rc = cmd_launch(args.port)
    if rc == 0:
        rc = cmd_smoke(args.port)
        rc = cmd_screenshot(args.port, args.out) or rc
    rc = cmd_stop(args.port) or rc
    print("CYCLE", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
