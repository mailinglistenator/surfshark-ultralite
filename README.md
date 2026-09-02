# Surfshark Ultra-Lite

Ultra-lite web control panel for the official Surfshark Linux app, plus a
zero-CPU **eco-mode freezer** — with a supervision harness that **proves**
the VPN tunnel and your local AI-agent/IDE sessions stay alive while the
UI is frozen.

## The problem

The official Surfshark Linux app's Electron renderer runs a canvas/radar
animation loop that burns **30–55% of a CPU core** even when idle or
minimized (Chromium software compositing is forced in this build — GPU
flags and window minimization don't help). Meanwhile the actual tunnel is
kernel WireGuard (<0.3% CPU) and the session keepalive lives in the
Electron **main** process — neither runs in the renderers.

## The fix

`SIGSTOP` every `--type=zygote` renderer → renderer CPU drops to **0.0%**
while the tunnel, NetworkManager profile, and local IPC stay alive.
`SIGCONT` restores the UI instantly, on demand.

## What's here

- `app.py` + `index.html` — ultra-lite web panel on `127.0.0.1:8777`:
  real-probe status (ipify/ipinfo), freeze/unfreeze endpoints, CleanWeb
  toggle, open-GUI action
- `launch.sh` — starts the panel (if not running) and opens it
- `freeze_audit.py` — supervised freeze test: probes every 10s (exit IP,
  tunnel-routed agent sockets, send-queue stall heuristics, NetworkManager
  journal events, API reachability) and **auto-rolls back (SIGCONT) on any
  regression**. The rollback runs in `finally` and on SIGTERM — a crashed
  or killed audit can never leave your renderers frozen.

## Safety design (earned the hard way)

- Freeze **only renderers** (`--type=zygote`), never the Electron main
  process — the main holds the NM connection and the preshared-key
  keepalive. Freezing or killing it tears the tunnel down.
- Only freeze **after** a real traffic probe confirms a good exit
  (`api.ipify.org` / `ipinfo.io` org check), never mid-connect or
  mid-switch.
- Unfreeze before clicking anything in the GUI — a stopped renderer can't
  process input. Renderers that respawn later start unfrozen; re-freeze.
- **Multi-MB Send-Q bursts are normal** on a 1280-MTU tunnel over Wi-Fi
  and drain within seconds; a real stall = the SAME stream growing +2MB
  across 3 consecutive checks. First sightings are recorded, not flagged.
- Agent gRPC channels (e.g. to Google frontends) rotate routinely —
  a stream count dropping is churn if fresh streams appear and others ACK.
- "App says connected" ≠ connected. Only real traffic probes are ground
  truth; the daemon's `getState` can report connected while blackholed.

## Requirements

Linux + NetworkManager, Surfshark 3.12.x, Python 3 (stdlib only).

## Usage

```bash
python3 app.py            # panel at http://127.0.0.1:8777
python3 freeze_audit.py 300   # 5-minute supervised freeze window
                           # exit code 0 = PASS, 3 = rolled back / FAIL
```

## Disclaimer

Not affiliated with Surfshark. SIGSTOP is a blunt instrument: run the
audit on your own machine/workload before adopting eco-mode, and never
freeze the main process.
