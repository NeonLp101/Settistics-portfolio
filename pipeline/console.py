"""Local crawler console: live collection statistics for the operator. Never deployed.

    python pipeline/console.py          # then open http://127.0.0.1:8899

Listens on 127.0.0.1 only and reads the database read-only. No player identifiers are shown,
and the API key is never displayed; only whether Riot currently accepts it.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from engine import ROOT, USER_AGENT, load_env

SECONDS_PER_REQUEST = 1.35   # engine.Client pacing; the dev key allows 100 requests per 2 minutes
DEFAULT_PAUSE = 1800         # older loop lines do not state their pause
DB = ROOT / "data" / "settistics.sqlite"
LOG = ROOT / "data" / "collect.log"          # EUW; other regions log to collect-<platform>.log
_cache = {}
# Restarts append to the same logs the crawlers always use, so the console keeps one row per region.
# One crawler per match routing cluster (europe, asia, americas, sea): Riot limits each cluster separately,
# so a second crawler in the same cluster would only split its budget and hit 429s.
CRAWLER_CONFIG = {
    "euw1": {"label": "EUW", "log": ROOT / "data" / "collect.log"},
    "kr": {"label": "KR", "log": ROOT / "data" / "collect-kr.log"},
    "na1": {"label": "NA", "log": ROOT / "data" / "collect-na1.log"},
    "vn2": {"label": "VN", "log": ROOT / "data" / "collect-vn2.log"},
}
# General collection across every champion, with one worker per routing cluster.
CRAWL_ARGS = ["--snowball", "--champion", "all", "--patch", "16.19",
              "--recheck-hours", "2", "--refresh", "--per-player", "100", "--since", "2026-09-23"]


def cached(key, seconds, compute):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = compute()
    _cache[key] = (time.time(), value)
    return value


def key_status():
    load_env()
    key = os.getenv("RIOT_API_KEY", "")
    if not key.startswith("RGAPI-"):
        return {"state": "missing", "text": "No key in .env"}
    try:
        req = Request("https://euw1.api.riotgames.com/lol/status/v4/platform-data",
                      headers={"X-Riot-Token": key, "User-Agent": USER_AGENT})
        with urlopen(req, timeout=15):
            return {"state": "ok", "text": "Accepted by Riot"}
    except HTTPError as e:
        return {"state": "bad", "text": f"Rejected by Riot (HTTP {e.code}): replace the key in .env"} if e.code in (401, 403) \
            else {"state": "warn", "text": f"Riot answered HTTP {e.code}"}
    except (URLError, TimeoutError):
        return {"state": "warn", "text": "Riot unreachable"}


def ddragon_latest():
    try:
        with urlopen(Request("https://ddragon.leagueoflegends.com/api/versions.json",
                             headers={"User-Agent": USER_AGENT}), timeout=15) as r:
            return json.load(r)[0]
    except (URLError, HTTPError, TimeoutError, ValueError):
        return None


_heavy = {"patches": [], "champions": [], "at": None}


def heavy_stats():
    """Patches and top champions read every stored game (GBs of JSON), so a background thread refreshes them."""
    while True:
        try:
            if DB.exists():
                db = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
                patches = db.execute("SELECT substr(json_extract(detail,'$.info.gameVersion'),1,5) p, SUM(status='done'), COUNT(*) "
                                     "FROM matches WHERE detail IS NOT NULL GROUP BY p ORDER BY p DESC").fetchall()
                champs = db.execute("SELECT json_extract(p.value,'$.championName') c, json_extract(p.value,'$.teamPosition') r, COUNT(*) n "
                                    "FROM matches m, json_each(m.detail,'$.info.participants') p WHERE m.status='done' "
                                    "GROUP BY c, r ORDER BY n DESC LIMIT 12").fetchall()
                db.close()
                _heavy.update(patches=[{"patch": p, "done": d, "seen": n} for p, d, n in patches],
                              champions=[{"champion": c, "role": r or "?", "games": n} for c, r, n in champs],
                              at=datetime.now(timezone.utc).isoformat())
        except sqlite3.Error:
            pass  # try again next cycle
        time.sleep(600)


def database():
    """Cheap counts only: every query here is answered by the matches_status index (engine.connect creates it)."""
    if not DB.exists():
        return None
    db = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    q = lambda sql, *a: db.execute(sql, a).fetchall()
    status = dict(q("SELECT status, COUNT(*) FROM matches GROUP BY status"))
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=6)).isoformat()
    # games saved per 10-minute bucket over the last 6 hours
    buckets = [0] * 36
    for (ts,) in q("SELECT collected_at FROM matches WHERE status='done' AND collected_at >= ?", since):
        age = (now - datetime.fromisoformat(ts)).total_seconds()
        i = 35 - int(age // 600)
        if 0 <= i < 36:
            buckets[i] += 1
    last_hour = sum(buckets[-6:])
    regions = q("SELECT platform, COUNT(*) FROM matches WHERE status='done' GROUP BY platform")
    catalogs = [r[0] for r in q("SELECT patch FROM catalog ORDER BY patch DESC")]
    newest = q("SELECT MAX(collected_at) FROM matches WHERE status='done'")[0][0]
    db.close()
    return {"status": status, "buckets": buckets, "lastHour": last_hour,
            "patches": _heavy["patches"], "champions": _heavy["champions"], "heavyAt": _heavy["at"],
            "regions": dict(regions), "catalogs": catalogs, "lastSaved": newest,
            "sizeMb": round(DB.stat().st_size / 1e6, 1)}


def seeds():
    if not DB.exists():
        return 0
    db = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    n = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    db.close()
    return n


def pool():
    """Crawl players per platform: all, added by snowballing, and not checked yet."""
    if not DB.exists():
        return {}
    db = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    rows = db.execute("SELECT platform, COUNT(*), SUM(source='snowball'), SUM(last_checked IS NULL) FROM players GROUP BY platform").fetchall()
    db.close()
    return {p: {"total": n, "snowball": s or 0, "unchecked": u or 0} for p, n, s, u in rows}


def crawler_logs():
    """(label, path) for every crawler log: collect.log is EUW, collect-kr.log is KR, and so on."""
    out = []
    for path in sorted(LOG.parent.glob("collect*.log")):
        suffix = path.stem[len("collect"):].lstrip("-")
        out.append(((suffix or "euw").upper(), path, suffix or "euw1"))
    return out


def all_crawler_processes():
    """{platform: [pid, ...]} for every running engine crawler. One PowerShell call, cached briefly."""
    def compute():
        command = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                   "Where-Object { $_.CommandLine -like '*engine.py crawl*' } | "
                   "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                 capture_output=True, text=True, timeout=15, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        found = {}
        for line in out.splitlines():
            pid, _, cmd = line.partition("|")
            named = re.search(r"--platform (\w+)", cmd)
            if pid.strip().isdigit():
                found.setdefault(named.group(1) if named else "euw1", []).append(int(pid))
        return found
    return cached("procs", 10, compute)


def crawler_processes(platform):
    """Return PIDs of this platform's engine crawler (a crawl without --platform is EUW)."""
    return list(all_crawler_processes().get(platform, []))


def restart_crawler(platform):
    """Restart one regional snowball crawler; only local console requests can call this."""
    if platform not in CRAWLER_CONFIG:
        raise ValueError("Unsupported crawler platform")
    killed = []
    for pid in crawler_processes(platform):
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0:
            killed.append(pid)
    cfg = CRAWLER_CONFIG[platform]
    cfg["log"].parent.mkdir(parents=True, exist_ok=True)
    stdout = cfg["log"].open("a", encoding="utf-8")
    stderr = cfg["log"].open("a", encoding="utf-8")
    args = [sys.executable, "-u", "pipeline/engine.py", "crawl", "--platform", platform, *CRAWL_ARGS]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    process = subprocess.Popen(args, cwd=ROOT, stdout=stdout, stderr=stderr,
                               creationflags=flags, close_fds=True)
    # The child owns the duplicated handles; closing the console copies avoids leaks.
    stdout.close()
    stderr.close()
    _cache.clear()
    return {"platform": platform, "pid": process.pid, "killed": killed}


def log_state(path=LOG, platform="euw1"):
    if not path.exists():
        return {"rounds": [], "tail": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    useful = [l for l in lines if not l.startswith("Saved ") and l.strip() and not l.strip() in "{}" and '":' not in l]
    rounds = [i for i, l in enumerate(lines) if l.startswith("== round") or l.startswith("== crawl start")]
    start = rounds[-1] if rounds else 0
    current = lines[start:]
    # The loop logs: "== round" -> discover (silent until done) -> "Match queue saved" -> collect -> "Collection complete".
    crawling = bool(rounds) and lines[start].startswith("== crawl start")
    if crawling:
        last_crawl = next((l for l in reversed(current) if l.startswith("Crawl:")), "")
        phase = "waiting" if last_crawl.startswith("Crawl: nobody due") else "crawling"
    elif any(l.startswith("Collection complete") for l in current) or not rounds:
        phase = "sleeping"
    elif any(l.startswith("Match queue saved") for l in current):
        phase = "collecting"
    else:
        phase = "discovering"
    if any(("failed" in l and "stopping" in l) or l.startswith("== crawl stopped") for l in current):
        phase = "stopped"
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    plan = crawler_plan(lines[start] if rounds else None, current, phase, modified, platform)
    last_saved = next((l for l in reversed(current) if l.startswith("Saved ")), None)
    return {"lastRound": lines[start] if rounds else None, "phase": phase, "progress": last_saved,
            "nextRound": (modified + timedelta(minutes=30)).isoformat() if phase == "sleeping" and rounds else None,
            "tail": useful[-18:], "modified": modified.isoformat(), "plan": plan}


def local_time(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)


def crawler_plan(round_line, current, phase, modified, platform="euw1"):
    """What the loop is doing now and what comes next, with times estimated from the rate limit."""
    now = datetime.now(timezone.utc)
    queued = (database_counts().get("queued", 0))
    if phase in ("crawling", "waiting"):
        return crawl_plan(round_line, current, phase, platform)
    m = re.match(r"== round (\d+) (\S+ \S+)(?: \((\d+) seeds\))?", round_line or "")
    number = int(m.group(1)) if m else 0
    started = local_time(m.group(2)) if m else None
    players = int(m.group(3)) if m and m.group(3) else seeds()
    pause_match = re.search(r"pause (\d+) min", round_line or "")
    pause = int(pause_match.group(1)) * 60 if pause_match else DEFAULT_PAUSE
    checked_line = next((l for l in reversed(current) if l.startswith("Discover: checked")), None)
    if checked_line:
        players = int(re.search(r"checked \d+/(\d+)", checked_line).group(1))
    download = lambda games: timedelta(seconds=games * 2 * SECONDS_PER_REQUEST)
    steps = []
    if phase == "discovering":
        progress = next((l for l in reversed(current) if l.startswith("Discover: checked")), None)
        if progress:
            done = int(re.search(r"checked (\d+)/", progress).group(1))
        else:  # older collector without progress lines: estimate from elapsed time
            done = min(players, int((now - started).total_seconds() / SECONDS_PER_REQUEST)) if started else 0
        end = now + timedelta(seconds=(players - done) * SECONDS_PER_REQUEST)
        steps.append({"label": "Checking seed players for new games", "done": done, "total": players, "until": end.isoformat(),
                      "estimated": progress is None})
        steps.append({"label": "Download new games", "at": end.isoformat(), "note": "count known after the check"})
        pause_from = None
    elif phase == "collecting":
        progress = next((l for l in reversed(current) if l.startswith("Saved ")), None)
        scanned, total = (map(int, re.search(r"scanned (\d+)/(\d+)", progress).groups()) if progress else (0, queued))
        end = now + download(max(0, total - scanned))
        steps.append({"label": "Downloading games", "done": scanned, "total": total, "until": end.isoformat()})
        pause_from = end
    else:
        pause_from = modified
    if phase in ("discovering", "collecting", "sleeping") and m:
        if pause_from is not None:
            resume = pause_from + timedelta(seconds=pause)
            steps.append({"label": f"Pause ({pause // 60} min)", "at": pause_from.isoformat(), "until": resume.isoformat(),
                          "current": phase == "sleeping"})
            steps.append({"label": f"Round {number + 1}: check players who are due, then download", "at": resume.isoformat()})
        else:
            steps.append({"label": f"Pause ({pause // 60} min), then round {number + 1}", "note": "after the download"})
    env = ROOT / ".env"
    key_expiry = (datetime.fromtimestamp(env.stat().st_mtime, timezone.utc) + timedelta(hours=24)).isoformat() if env.exists() else None
    return {"round": number, "phase": phase, "steps": steps, "keyExpires": key_expiry}


def crawl_plan(start_line, current, phase, platform="euw1"):
    """Continuous crawler: players due now, speed, and when the due players will be cleared."""
    hours = float(re.search(r"recheck ([\d.]+) h", start_line).group(1)) if "recheck" in start_line else 2
    started = local_time(re.match(r"== crawl start (\S+ \S+)", start_line).group(1))
    stat = next((l for l in reversed(current) if re.match(r"Crawl: [\d,]+ players checked", l)), None)
    checked, saved, rate, due = (0, 0, 0, None)
    if stat:
        n = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", stat)]
        checked, saved, rate, due = n[0], n[1], n[2], n[3]
    named = re.search(r"platform (\w+)", start_line)
    platform = named.group(1) if named else platform
    if due is None or phase == "waiting":
        due = due_players(hours, platform)
    players = dict(cached("pool", 15, pool).get(platform, {"total": 0, "snowball": 0, "unchecked": 0}))
    players["on"] = ", snowball" in start_line
    now = datetime.now(timezone.utc)
    env = ROOT / ".env"
    key_expiry = (datetime.fromtimestamp(env.stat().st_mtime, timezone.utc) + timedelta(hours=24)).isoformat() if env.exists() else None
    if phase == "waiting":
        wait = next((l for l in reversed(current) if l.startswith("Crawl: nobody due")), "")
        at = re.search(r"due at (\d\d:\d\d)", wait)
        steps = [{"label": "Waiting: every player checked in the last " + f"{hours:g} h", "current": True,
                  "sub": f"next player due at {at.group(1)}" if at else "waiting for the next player to become due"}]
    else:
        clear = now + timedelta(seconds=due * SECONDS_PER_REQUEST * 1.05)  # ~5% extra for game downloads
        steps = [{"label": f"Crawling: {due:,} players due", "current": True, "until": clear.isoformat(),
                  "sub": f"{checked:,} players checked, {saved:,} games saved since {started.astimezone().strftime('%H:%M')}"
                         f"{f' · {rate:,} games per hour' if rate else ''} · due players cleared ~{clear.astimezone().strftime('%H:%M')}"}]
    if players["unchecked"]:
        steps.append({"label": f"New players first: {players['unchecked']:,} not checked yet, then rechecks"})
    steps.append({"label": f"Every player is rechecked {hours:g} h after their last check; new games download right away"})
    return {"round": None, "phase": phase, "mode": "crawl", "steps": steps, "keyExpires": key_expiry, "pool": players}


def due_players(hours, platform):
    if not DB.exists():
        return 0
    db = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    n = db.execute("SELECT COUNT(*) FROM players WHERE platform=? AND (last_checked IS NULL OR last_checked<=?)",
                   (platform, cutoff)).fetchone()[0]
    db.close()
    return n


def database_counts():
    db = cached("db", 8, database)
    return (db or {}).get("status", {})


def exports():
    out = {}
    for name, path in (("export", ROOT / "data" / "public" / "stats.json"), ("model", ROOT / "data" / "public" / "wpa_model.json"),
                       ("site", ROOT / "public" / "data" / "index.json")):
        out[name] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if path.exists() else None
    model = ROOT / "data" / "public" / "wpa_model.json"
    if model.exists():
        m = json.loads(model.read_text(encoding="utf-8"))
        out["wpa"] = {"games": m.get("games"), "auc": m.get("inGame", {}).get("auc"),
                      "calibration": m.get("inGame", {}).get("calibrationErrorPp")}
    return out


def crawler_row(name, path, platform):
    row = {"name": name, "platform": platform, "processes": crawler_processes(platform), **log_state(path, platform)}
    # A crash can end the log mid-line with no "crawl stopped" marker; no process means it is not running.
    if row.get("phase") in ("crawling", "waiting") and not row["processes"]:
        row["phase"] = "stopped"
    return row


def snapshot():
    return {"now": datetime.now(timezone.utc).isoformat(),
            "db": cached("db", 8, database), "seeds": cached("seeds", 30, seeds), "pool": cached("pool", 15, pool), "log": log_state(),
            "crawlers": [crawler_row(name, path, platform) for name, path, platform in crawler_logs()],
            "processState": {platform: crawler_processes(platform) for platform in CRAWLER_CONFIG},
            "key": cached("key", 300, key_status), "ddragon": cached("dd", 600, ddragon_latest), "exports": exports()}


PAGE = (Path(__file__).with_name("console.html")).read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            body, kind = json.dumps(snapshot()).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            body, kind = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Any website open in the same browser could send a simple POST to 127.0.0.1; only this page may.
        host = self.server.server_address[1]
        if self.headers.get("Origin") not in (f"http://127.0.0.1:{host}", f"http://localhost:{host}"):
            self.send_error(403)
            return
        if self.path.startswith("/api/crawler/") and self.path.endswith("/restart"):
            platform = self.path[len("/api/crawler/"):-len("/restart")].strip("/")
            try:
                body = restart_crawler(platform)
                payload = json.dumps({"ok": True, **body}).encode()
                self.send_response(200)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                payload = json.dumps({"ok": False, "message": str(error)}).encode()
                self.send_response(400 if isinstance(error, ValueError) else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, *args):
        pass  # keep the terminal quiet


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    print(f"Settistics console: http://127.0.0.1:{args.port}")
    threading.Thread(target=heavy_stats, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
