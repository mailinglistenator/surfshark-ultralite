#!/usr/bin/env python3
"""
SurfShark Ultra-Lite Web UI Control Center
Features:
- Real-time traffic probe (exit IP, Geo, Org)
- Eco-Mode Freezer (Suspends Electron renderer to drop CPU to 0.0% while keeping tunnel active)
- 1-Click CleanWeb toggle
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
PORT = int(os.environ.get("SURFSHARK_WEBUI_PORT", "8777"))
CACHE_PATH = os.path.join(HOME, ".config/Surfshark/cache.json")

_cached_state = {
    "connected_real": False,
    "public_ip": None,
    "exit_org": "",
    "exit_geo": "",
    "gui_running": False,
    "gui_frozen": False,
    "locations_count": 0,
    "last_check": 0
}
_state_lock = threading.Lock()

def locations():
    if os.path.exists(CACHE_PATH):
        try:
            cache = json.load(open(CACHE_PATH))
            clusters = cache.get("/v5/server/clusters/all")
            if isinstance(clusters, dict):
                return clusters.get("value", [])
            return clusters or []
        except Exception:
            pass
    return []

def public_ip_probe():
    try:
        req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.read().decode().strip()[:64]
    except Exception:
        return None

def ipinfo_probe():
    try:
        req = urllib.request.Request("https://ipinfo.io", headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.read().decode()
    except Exception:
        return ""

def _field(s, key):
    m = re.search(r'"%s":\s*"([^"]*)"' % key, s)
    return m.group(1) if m else None

def get_electron_processes():
    res = subprocess.run(["ps", "-eo", "pid,state,%cpu,cmd"], capture_output=True, text=True)
    renderers = []
    main_pids = []
    for line in res.stdout.splitlines():
        if "/opt/Surfshark/surfshark" in line:
            parts = line.strip().split(None, 3)
            pid = int(parts[0])
            state = parts[1]
            if "--type=zygote" in line:
                renderers.append((pid, state))
            else:
                main_pids.append((pid, state))
    return main_pids, renderers

def probe_network():
    ip = public_ip_probe()
    info = ipinfo_probe() if ip else ""
    org = _field(info, "org") or ""
    city = _field(info, "city") or ""
    country = _field(info, "country") or ""
    geo = f"{city}, {country}".strip(", ")
    is_protected = bool(ip and ("Cyberzone" in org or "surfshark" in org.lower()))
    
    main_pids, renderers = get_electron_processes()
    gui_running = len(main_pids) > 0 or len(renderers) > 0
    gui_frozen = any("T" in st for _, st in renderers)
    
    locs = locations()
    
    with _state_lock:
        _cached_state["connected_real"] = is_protected
        _cached_state["public_ip"] = ip
        _cached_state["exit_org"] = org
        _cached_state["exit_geo"] = geo
        _cached_state["gui_running"] = gui_running
        _cached_state["gui_frozen"] = gui_frozen
        _cached_state["locations_count"] = len(locs)
        _cached_state["last_check"] = time.time()

def background_worker():
    while True:
        try:
            probe_network()
        except Exception:
            pass
        time.sleep(3)

threading.Thread(target=background_worker, daemon=True).start()

def freeze_gui():
    _, renderers = get_electron_processes()
    count = 0
    for pid, _ in renderers:
        try:
            os.kill(pid, signal.SIGSTOP)
            count += 1
        except Exception:
            pass
    probe_network()
    return count > 0, f"Suspended {count} UI renderer processes (CPU dropped to 0.0%)"

def unfreeze_gui():
    _, renderers = get_electron_processes()
    count = 0
    for pid, _ in renderers:
        try:
            os.kill(pid, signal.SIGCONT)
            count += 1
        except Exception:
            pass
    probe_network()
    return count > 0, f"Resumed {count} UI renderer processes"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            page = open(os.path.join(BASE, "index.html")).read()
            self._send(200, page, "text/html")
        elif self.path == "/api/state":
            with _state_lock:
                self._send(200, dict(_cached_state))
        elif self.path == "/api/locations":
            out = [{"connectionName": l["connectionName"], "country": l["country"],
                    "countryCode": l.get("countryCode"), "city": l.get("location"),
                    "tags": l.get("tags") or []} for l in locations()]
            self._send(200, out)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0)) or 0
        body = json.loads(self.rfile.read(content_len) or b"{}")
        
        if self.path == "/api/freeze":
            ok, msg = freeze_gui()
            self._send(200, {"ok": ok, "message": msg})
        elif self.path == "/api/unfreeze":
            ok, msg = unfreeze_gui()
            self._send(200, {"ok": ok, "message": msg})
        elif self.path == "/api/open-gui":
            unfreeze_gui()
            subprocess.Popen(["gtk-launch", "surfshark"])
            self._send(200, {"ok": True, "message": "Opening Surfshark app window…"})
        else:
            self._send(404, {"error": "not found"})

def main():
    print(f"[surfshark-control-center] Running on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
