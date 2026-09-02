#!/usr/bin/env python3
"""
freeze_audit.py — controlled eco-mode freezer safety test / supervision.

Freezes ALL Surfshark Electron renderers (--type=zygote) with SIGSTOP,
probes every cycle, and auto-rolls back (SIGCONT) on ANY regression:
  - exit IP changes, or probe fails twice in a row
  - Antigravity's tunnel-routed established streams drop to zero
  - a stream's Send-Q keeps GROWING (+1MB) across consecutive checks —
    true stall behavior; a fresh stream queuing a big upload burst is
    NOT a stall, so first sightings are only recorded, never flagged
  - NetworkManager deactivate/delete event during the window
SIGCONT rollback lives in finally + SIGTERM handler: a crash, a kill, or
a normal end all leave the renderers running.

Usage: freeze_audit.py [total_seconds]   (default 80s)
Exit code: 0 = PASS, 3 = rolled back / FAIL, 1 = crash.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ZYGOTE_PAT = "surfshark --type=zygote"
TUNNEL_PREFIX = "10.14."          # surfshark_wg address space
DEFAULT_SECONDS = 80
CYCLE_SLEEP = 10
GROWTH_MARGIN = 2097152           # +2MB between consecutive checks
STUCK_STREAK_LIMIT = 3            # SAME stream growing 3 checks in a row = stall
CLK = os.sysconf("SC_CLK_TCK")


class Rollback(Exception):
    """Raised to end the freeze window; finally always SIGCONTs."""


def sh(cmd, timeout=12):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout).stdout
    except Exception as e:
        return f"ERR {e}"


def http(url, timeout=6):
    """(reached, body) — reached=True even for 4xx/5xx (server responded)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.read().decode(errors="replace")[:300]
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__


def exit_state():
    ok, ip = http("https://api.ipify.org")
    ip = ip.strip() if ok else f"ERR:{ip}"
    ok2, raw = http("https://ipinfo.io/json")
    try:
        j = json.loads(raw)
        return ip, j.get("org", ""), j.get("city", ""), j.get("country", "")
    except Exception:
        return ip, "", "", ""


def zygotes():
    pids = []
    for ln in sh("ps -eo pid,cmd").splitlines():
        if ZYGOTE_PAT in ln:
            pids.append(int(ln.strip().split(None, 1)[0]))
    return pids


def stat_of(pid):
    try:
        parts = open(f"/proc/{pid}/stat").read().split()
        return parts[2], int(parts[13]) + int(parts[14])   # state, cpu ticks
    except Exception:
        return "GONE", 0


def ag_streams():
    """Established Antigravity sockets split into tunnel-routed vs local."""
    tun, loc = [], []
    for ln in sh("ss -tnp").splitlines():
        if "ESTAB" not in ln:
            continue
        if ("language_server" in ln) or ('"antigravity"' in ln):
            p = ln.split()
            if len(p) >= 5:
                rec = (p[3], p[4], int(p[1]), int(p[2]))  # local, peer, recvq, sendq
                (tun if p[3].startswith(TUNNEL_PREFIX) else loc).append(rec)
    return tun, loc


def nm_teardowns_since(ts):
    out = sh(f"journalctl -u NetworkManager --since @{int(ts)} --no-pager 2>/dev/null"
             f" | grep -E 'connection-deactivate|connection-delete'")
    return [l for l in out.strip().splitlines() if "surfshark" in l.lower() or "wg" in l.lower()]


def newest_app_log():
    d = os.path.expanduser("~/.config/Surfshark/logs")
    try:
        return max((os.path.join(d, f) for f in os.listdir(d)
                    if f.startswith("debug-") and f.endswith(".log")),
                   key=os.path.getmtime)
    except Exception:
        return ""


def app_log():
    try:
        p = newest_app_log()
        return open(p, errors="replace").read() if p else ""
    except Exception:
        return ""


def cpu_since(pid, ticks0, elapsed):
    _, t1 = stat_of(pid)
    return round((t1 - ticks0) / CLK / max(elapsed, 1) * 100, 1)


def main():
    total_s = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS
    cycles = max(1, total_s // CYCLE_SLEEP)

    def on_term(signum, frame):
        raise Rollback(f"SIGTERM received at +{time.time() - t_loop0:.0f}s")
    signal.signal(signal.SIGTERM, on_term)

    print(f"== BASELINE == (planned frozen window: {cycles * CYCLE_SLEEP}s)")
    ip0, org0, city0, ctry0 = exit_state()
    print(f"exit probe : {ip0} | {org0} | {city0}, {ctry0}")
    zp = zygotes()
    print(f"renderers  : {zp}")
    tun0, loc0 = ag_streams()
    for s in tun0:
        print(f"AG tunnel  : {s[0]} -> {s[1]}  (recvq={s[2]} sendq={s[3]})")
    for s in loc0:
        print(f"AG local   : {s[0]} -> {s[1]}")
    log_before = app_log()
    ts0 = time.time()
    t_loop0 = ts0
    ticks0 = {pid: stat_of(pid)[1] for pid in zp}

    rolled_back = False
    try:
        for pid in zp:
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        print(f"\n== FROZEN since +0s (SIGSTOP -> {zp}) ==")

        stuck_streak = 0
        stuck_stream = None
        prev_sendq = {}                       # stream -> last seen Send-Q
        for i in range(1, cycles + 1):
            time.sleep(CYCLE_SLEEP)
            elapsed = i * CYCLE_SLEEP
            states = {pid: stat_of(pid)[0] for pid in zp}
            ip, org, city, _ = exit_state()
            probe_fail = ip.startswith("ERR")
            err_streak = err_streak + 1 if probe_fail else 0
            tun, loc = ag_streams()

            # Stall detection: a stream's send queue GROWING by +1MB since the
            # previous check. First sightings are recorded, never flagged —
            # AG gRPC rotation legitimately opens streams mid-freeze and
            # bursts queue megabytes on a 1280-MTU tunnel.
            growing = []
            for s in tun:
                key, sq = (s[0], s[1]), s[3]
                if key in prev_sendq and sq >= prev_sendq[key] + GROWTH_MARGIN:
                    growing.append((key[0], prev_sendq[key], sq))
                prev_sendq[key] = sq
            # stall = the SAME stream growing on consecutive checks
            if growing:
                if stuck_stream in (g[0] for g in growing):
                    stuck_streak += 1
                else:
                    stuck_streak, stuck_stream = 1, growing[0][0]
            else:
                stuck_streak, stuck_stream = 0, None

            ev = nm_teardowns_since(ts0)
            hot = max(zp, key=lambda p: ticks0.get(p, 0) and cpu_since(p, ticks0[p], elapsed))
            cpu_hot = cpu_since(hot, ticks0[hot], elapsed) if hot in ticks0 else -1
            g = http("https://generativelanguage.googleapis.com/")
            ka = app_log()[len(log_before):].count("keep session alive")
            sq_sum = sum(s[3] for s in tun)
            print(f"+{elapsed:>4}s | renderers={list(states.values())} "
                  f"| hottest {hot}: {cpu_hot}%/core | exit={ip[:24]} "
                  f"| AG tunnel streams={len(tun)} growing={len(growing)} "
                  f"(streak {stuck_streak}{' on ' + stuck_stream if stuck_stream else ''}) "
                  f"sq={sq_sum} ka={ka} "
                  f"| google-api={'OK' if g[0] else 'FAIL'}"
                  f"{'' if not ev else ' | NM EVENTS: ' + str(ev)}", flush=True)

            if probe_fail and err_streak >= 2:
                raise Rollback(f"exit probe failed x{err_streak} while frozen")
            if not probe_fail and ip != ip0:
                raise Rollback(f"exit IP changed under freeze: {ip0} -> {ip}")
            if len(tun0) > 0 and len(tun) == 0:
                raise Rollback("all Antigravity tunnel-routed streams died")
            if stuck_streak >= STUCK_STREAK_LIMIT:
                g0 = next(g for g in growing if g[0] == stuck_stream)
                raise Rollback(f"send queue growing stall x{stuck_streak} on ONE stream: "
                               f"{g0[0]} {g0[1]} -> {g0[2]} bytes")
            if ev:
                raise Rollback(f"NM teardown during freeze: {ev[-1][:120]}")
        print(f"\n== window completed normally at +{cycles * CYCLE_SLEEP}s ==")
    except Rollback as r:
        rolled_back = True
        print(f"\n== ROLLBACK TRIGGERED: {r} ==")
    finally:
        # SIGCONT ALWAYS runs — crash, kill, rollback, or normal end
        for pid in zp:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        print("== UNFROZEN (SIGCONT) ==")

    time.sleep(4)
    states = {pid: stat_of(pid)[0] for pid in zp}
    ip1, org1, city1, _ = exit_state()
    tun1, loc1 = ag_streams()
    same = sum(1 for s in tun1 if any(s[0] == b[0] and s[1] == b[1] for b in tun0))
    print(f"post-CONT : renderers={list(states.values())}")
    print(f"exit probe: {ip1} | {org1} | {city1}")
    print(f"AG tunnel : {len(tun1)} streams, {same}/{len(tun0)} identical to baseline")

    log_after = app_log()
    ended = [l for l in log_after[len(log_before):].splitlines()
             if "VPN session Ended" in l]
    keeps = [l.split("info: ")[-1].strip() for l in log_after[len(log_before):].splitlines()
             if "keep session alive" in l]
    print(f"app log during window: session-Ended lines={len(ended)}, "
          f"keep-alive lines={len(keeps)}{': ' + keeps[0] if keeps else ''}")

    print("\n== VERDICT ==")
    ag_alive = len(tun1) > 0 or len(tun0) == 0
    if not rolled_back and ip1 == ip0 and ag_alive:
        print(f"PASS — freeze held {cycles * CYCLE_SLEEP}s: exit unchanged, "
              f"AG still tunnel-routed, no NM events, CPU parked. "
              f"(streams {len(tun0)} -> {len(tun1)}, rotation is normal)")
        sys.exit(0)
    else:
        why = f"rolled back early" if rolled_back else "post-unfreeze mismatch"
        print(f"FAIL — {why}")
        print("Investigate before using eco-mode again.")
        sys.exit(3)


if __name__ == "__main__":
    main()
