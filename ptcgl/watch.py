"""Live watcher over the files and sockets the running client touches.

Everything here is read-only observation of the local install. It follows the
client's own logs and deck state; it does not attach to or modify the process.
"""

import os
import re
import subprocess
import time

from . import paths

# Lines worth surfacing out of the client's very chatty logs.
INTERESTING = re.compile(
    r"match|opponent|deck|matchmaking|websocket|telemetry|"
    r"gamestate|turn|prize|rank|elo|season",
    re.IGNORECASE,
)
NOISE = re.compile(
    r"referenced script|serialization|unloading|mesh data|"
    r"UnloadTime|DontDestroyOnLoad|Total: ",
    re.IGNORECASE,
)


def find_process():
    """PID of the running client, or None."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Pokemon TCG Live.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().startswith("pokemon tcg live"):
            try:
                return int(parts[1])
            except ValueError:
                pass
    return None


def connections(pid):
    """Remote endpoints the client currently holds open."""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[-1] == str(pid) and f[0] == "TCP":
            rows.append({"local": f[1], "remote": f[2], "state": f[3]})
    return rows


class _Tail:
    """Follows a file across truncation and rotation."""

    def __init__(self, path):
        self.path = path
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0

    def read(self):
        if not os.path.exists(self.path):
            return []
        size = os.path.getsize(self.path)
        if size < self.pos:          # rotated or truncated
            self.pos = 0
        if size == self.pos:
            return []
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.pos)
            chunk = fh.read()
            self.pos = fh.tell()
        return [l for l in chunk.splitlines() if l.strip()]


def _stamp():
    return time.strftime("%H:%M:%S")


def _emit(line):
    print(line, flush=True)


def run(interval=1.0, raw=False, on_event=_emit):
    """Follow the client until interrupted, reporting what changes."""
    pid = find_process()
    on_event(f"[{_stamp()}] client pid: {pid if pid else 'not running'}")

    tails = {os.path.basename(p): _Tail(p) for p in [paths.PLAYER_LOG] if os.path.exists(p)}
    for log in paths.game_logs()[:1]:
        tails[os.path.basename(log)] = _Tail(log)

    decks = {p: 0.0 for p in paths.profile_dirs()}
    known_logs = set(paths.game_logs())
    last_conns = None

    while True:
        # a new Game log means a new session or match context
        for log in paths.game_logs():
            if log not in known_logs:
                known_logs.add(log)
                on_event(f"[{_stamp()}] new game log: {os.path.basename(log)}")
                tails[os.path.basename(log)] = _Tail(log)

        for name, tail in list(tails.items()):
            for line in tail.read():
                if raw or (INTERESTING.search(line) and not NOISE.search(line)):
                    on_event(f"[{_stamp()}] {name}: {line[:400]}")

        # deck state is rewritten when you pick or edit a deck
        for profile in paths.profile_dirs():
            path = os.path.join(profile, "unsaved-deckinfo.json")
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if decks.get(profile) and mtime > decks[profile]:
                on_event(f"[{_stamp()}] deck state changed ({os.path.basename(profile)[:8]})")
            decks[profile] = mtime

        if pid:
            now = [c["remote"] for c in connections(pid) if not c["remote"].startswith("127.")]
            if last_conns is not None and set(now) != set(last_conns):
                opened = set(now) - set(last_conns)
                closed = set(last_conns) - set(now)
                if opened:
                    on_event(f"[{_stamp()}] opened: {', '.join(sorted(opened))}")
                if closed:
                    on_event(f"[{_stamp()}] closed: {', '.join(sorted(closed))}")
            last_conns = now

        time.sleep(interval)
