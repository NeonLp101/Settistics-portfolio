"""Local, resumable Riot collection and descriptive statistics. Python 3.10+."""
import argparse
import hashlib
import hmac
import secrets
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import statistics
import zlib
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
# Riot's edge (Cloudflare) rejects urllib's default "Python-urllib/x.y" agent with 403 "error code: 1010".
USER_AGENT = "settistics/0.4 (local collector)"
PLATFORMS = dict(euw1="europe", eun1="europe", tr1="europe", ru="europe",
                 na1="americas", br1="americas", la1="americas", la2="americas",
                 kr="asia", jp1="asia", oc1="sea", sg2="sea", tw2="sea", vn2="sea")
ROLES = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}
FIRSTS = ("firstBloodKill", "firstBloodAssist", "firstTowerKill", "firstTowerAssist")  # Riot's participant flags
# Games the site, its model and its exports may use. Games that started at or after the research seal are
# reserved for one-time confirmation runs (docs/current-state.md), and focused crawls ('focus <champion>:<role>')
# oversample one champion, which would skew every other champion's numbers.
FUTURE_SEAL_MS = 1790400420000  # 2026-09-26 05:27 UTC; set for the archived item model's evaluation, still held back
USABLE_SQL = ("status='done' AND timeline IS NOT NULL AND (source IS NULL OR source NOT LIKE 'focus %') "
              f"AND json_extract(detail,'$.info.gameStartTimestamp') < {FUTURE_SEAL_MS}")

def utc():
    return datetime.now(timezone.utc).isoformat()

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    temp.replace(path)

# Data minimisation (GDPR Art. 5(1)(c)): stored match data never keeps names or Riot account IDs.
# Each account ID is replaced by a keyed pseudonym. The key stays in data/ (never published or committed),
# so the pseudonym cannot be reversed without it, but a deletion request can still be matched by pseudonymising
# the PUUIDs it lists with the same key.
IDENTITY_FIELDS = ("puuid", "riotIdGameName", "riotIdTagline", "summonerName", "summonerId", "profileIcon")
PSEUDONYM_KEY = ROOT / "data" / "pseudonym.key"
_pseudonym_key = None

def pseudonym(puuid):
    global _pseudonym_key
    if _pseudonym_key is None:
        if not PSEUDONYM_KEY.exists():
            PSEUDONYM_KEY.parent.mkdir(parents=True, exist_ok=True)
            PSEUDONYM_KEY.write_text(secrets.token_hex(32), encoding="utf-8")
        _pseudonym_key = bytes.fromhex(PSEUDONYM_KEY.read_text(encoding="utf-8").strip())
    return hmac.new(_pseudonym_key, puuid.encode(), hashlib.sha256).hexdigest()[:32]

def minimize(document):
    """Strip player identity from a match or timeline document in place; returns it for chaining."""
    meta = document.get("metadata", {})
    if isinstance(meta.get("participants"), list):
        meta["participants"] = [p if not p or p.startswith("p:") else "p:" + pseudonym(p) for p in meta["participants"]]
    for player in document.get("info", {}).get("participants", []):
        if player.get("puuid"):
            player["playerRef"] = "p:" + pseudonym(player["puuid"])
        for field in IDENTITY_FIELDS:
            player.pop(field, None)
    return document

def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in {"RIOT_API_KEY", "SETTISTICS_ADMIN_TOKEN"}:
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # EUW and KR write to the same WAL database. Wait for a short competing write
    # rather than ending a crawler at SQLite's five-second default timeout.
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    db.executescript('''
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS players (
        puuid TEXT, platform TEXT, source TEXT, observed_at TEXT,
        cursor INTEGER DEFAULT 0, PRIMARY KEY(puuid,platform));
      CREATE TABLE IF NOT EXISTS matches (
        id TEXT PRIMARY KEY, platform TEXT, source TEXT,
        detail TEXT, timeline TEXT, status TEXT DEFAULT 'queued', collected_at TEXT);
      CREATE TABLE IF NOT EXISTS catalog (patch TEXT PRIMARY KEY, version TEXT, items TEXT);
      -- status sits after the large detail/timeline columns, so without this every count reads the whole file
      CREATE INDEX IF NOT EXISTS matches_status ON matches(status, platform, collected_at);
    ''')
    columns = {r[1] for r in db.execute("PRAGMA table_info(players)")}
    for column in ("last_checked", "last_active", "focus"):
        if column not in columns:
            db.execute(f"ALTER TABLE players ADD COLUMN {column} TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS players_focus ON players(platform, focus, last_checked)")
    return db

class RiotError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Riot HTTP {status}. No key or response contents logged.")

class Client:
    """One worker per routing bucket; conservative pacing plus header-driven limits per host/method."""
    def __init__(self, key, interval=1.35):
        if not key or not key.startswith("RGAPI-"):
            raise ValueError("Set RIOT_API_KEY in .env (never upload that file).")
        self.key, self.interval, self.last = key, max(1.35, interval), 0
        self.limits, self.calls = {}, defaultdict(deque)

    def pace(self, host, method):
        wait = max(0, self.last + self.interval - time.monotonic())
        for scope in (host, (host, method)):
            now = time.monotonic()
            history = self.calls[scope]
            for limit, window in self.limits.get(scope, [(100, 120)]):
                recent = [t for t in history if t > now - window]
                if len(recent) >= limit:
                    wait = max(wait, recent[-limit] + window - now + .1)
        if wait > 0:
            self.pause(wait)
        now = time.monotonic()
        self.last = now
        for scope in (host, (host, method)):
            self.calls[scope].append(now)
            horizon = max([w for _, w in self.limits.get(scope, [(100,120)])] + [120])
            while self.calls[scope] and self.calls[scope][0] < now - horizon:
                self.calls[scope].popleft()

    @staticmethod
    def pause(seconds):
        if seconds > 5:
            print(f"Rate pacing: waiting {seconds:.0f}s; Ctrl+C safely stops.", flush=True)
        while seconds > 0:
            chunk = min(seconds, 30)
            time.sleep(chunk)
            seconds -= chunk

    def get(self, host, path, method):
        if host not in {p + ".api.riotgames.com" for p in PLATFORMS} | {c + ".api.riotgames.com" for c in PLATFORMS.values()}:
            raise ValueError("Unexpected Riot host")
        for attempt in range(6):
            self.pace(host, method)
            request = Request("https://" + host + path, headers={"X-Riot-Token": self.key, "Accept": "application/json", "User-Agent": USER_AGENT})
            try:
                with urlopen(request, timeout=30) as response:
                    for header, scope in (("X-App-Rate-Limit", host), ("X-Method-Rate-Limit", (host,method))):
                        value = response.headers.get(header)
                        if value:
                            self.limits[scope] = [(int(n),float(w)) for n,w in (v.split(":") for v in value.split(","))]
                    return json.load(response)
            except HTTPError as error:
                if error.code == 429:
                    # Never cap Retry-After or retry before the server permits it.
                    seconds = float(error.headers.get("Retry-After", 120))
                    self.pause(max(1, seconds) + 1)
                elif error.code >= 500:
                    self.pause(2 ** attempt)
                else:
                    raise RiotError(error.code) from None
            except (URLError, TimeoutError):
                self.pause(2 ** attempt)
        raise RuntimeError("Riot unavailable after bounded retries; rerun to resume.")

def static_json(url):
    if urlparse(url).hostname != "ddragon.leagueoflegends.com":
        raise ValueError("Unexpected static host")
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
        return json.load(response)

PROVISIONAL = "provisional:"   # catalog.version prefix: item list borrowed from the previous patch
_catalog_checked = {}          # patch -> time Data Dragon was last asked for the real list

def patch_key(patch):
    """(16, 19) for "16.19" or "16.19.1"; None for legacy entries such as "lolpatch_3.7"."""
    parts = patch.split(".")[:2]
    return tuple(int(x) for x in parts) if len(parts) == 2 and all(x.isdigit() for x in parts) else None

def catalog(db, patch):
    """Item list for a patch from Data Dragon.

    Data Dragon is updated by hand and can lag a new patch by a day or two. Until then the previous patch's
    list is used provisionally; extract() drops item statistics for any player who bought an item that list
    does not contain (new items), so nothing gets misclassified. The real list replaces it once published."""
    cached = db.execute("SELECT * FROM catalog WHERE patch=?", (patch,)).fetchone()
    if cached and not cached["version"].startswith(PROVISIONAL):
        return json.loads(cached["items"])
    if cached and time.time() - _catalog_checked.get(patch, 0) < 1800:
        return json.loads(cached["items"])
    _catalog_checked[patch] = time.time()
    versions = static_json("https://ddragon.leagueoflegends.com/api/versions.json")
    version = next((v for v in versions if v.startswith(patch + ".")), None)
    if version:
        data = static_json(f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json")["data"]
        db.execute("INSERT OR REPLACE INTO catalog VALUES (?,?,?)", (patch, version, json.dumps(data)))
        db.commit()
        if cached:
            print(f"Patch {patch}: Data Dragon published {version}; replaced the provisional item list. Re-run export.")
        return data
    if cached:
        return json.loads(cached["items"])
    earlier = [v for v in versions if patch_key(v) and patch_key(v) < patch_key(patch)]
    if not earlier:
        raise ValueError(f"No Data Dragon item list for {patch} or any earlier patch.")
    data = static_json(f"https://ddragon.leagueoflegends.com/cdn/{earlier[0]}/data/en_US/item.json")["data"]
    db.execute("INSERT OR REPLACE INTO catalog VALUES (?,?,?)", (patch, PROVISIONAL + earlier[0], json.dumps(data)))
    db.commit()
    print(f"Patch {patch}: not on Data Dragon yet; using {earlier[0]} items provisionally (new items are left out).")
    return data

# --focus: crawl only players of one champion in one role (e.g. Kaisa BOTTOM). Their games are what we want,
# and most of them are on that champion, so a small pool of them yields far more target games per API call
# than snowballing everyone. Ladder seeds are checked once to find such players, then drop out.
def focus_key(args):
    if not getattr(args, "focus", False):
        return None
    if args.champion == "all":
        raise ValueError("--focus needs --champion (and usually --role)")
    return f"{args.champion.lower()}:{args.role}"

def plays_focus(player, args):
    return player.get("championName","").lower() == args.champion.lower() and (args.role == "ALL" or player.get("teamPosition") == args.role)

def promote(db, puuids, args):
    """Mark these players as focus players (inserting unknown ones), up to --snowball-cap per platform."""
    key = focus_key(args)
    room = getattr(args, "snowball_cap", SNOWBALL_CAP) - db.execute(
        "SELECT COUNT(*) FROM players WHERE platform=? AND focus=?", (args.platform, key)).fetchone()[0]
    added, now = 0, utc()
    for puuid in dict.fromkeys(puuids):
        if added >= room:
            break
        row = db.execute("SELECT focus FROM players WHERE puuid=? AND platform=?", (puuid, args.platform)).fetchone()
        if row is None:
            db.execute("INSERT INTO players(puuid,platform,source,observed_at,focus) VALUES(?,?,'focus',?,?)", (puuid, args.platform, now, key))
        elif row[0] != key:
            db.execute("UPDATE players SET focus=? WHERE puuid=? AND platform=?", (key, puuid, args.platform))
        else:
            continue
        added += 1
    return added

def focus_from_games(db, args):
    """Known players who played the focus champion and role in stored games, most recent game first."""
    refs = {"p:" + pseudonym(r[0]): r[0] for r in db.execute("SELECT puuid FROM players WHERE platform=?", (args.platform,))}
    rows = db.execute(
        "SELECT json_extract(p.value,'$.playerRef'), MAX(json_extract(m.detail,'$.info.gameStartTimestamp')) "
        "FROM matches m, json_each(json_extract(m.detail,'$.info.participants')) p "
        "WHERE m.platform=? AND m.status='done' AND lower(json_extract(p.value,'$.championName'))=? "
        "AND (?='ALL' OR json_extract(p.value,'$.teamPosition')=?) GROUP BY 1 ORDER BY 2 DESC",
        (args.platform, args.champion.lower(), args.role, args.role)).fetchall()
    return [refs[ref] for ref, _ in rows if ref in refs]

def seed(db, client, args):
    host = args.platform + ".api.riotgames.com"
    key = focus_key(args)
    if key and getattr(args, "from_games", False):
        found = focus_from_games(db, args)[:args.players]
        added = promote(db, found, args)
        db.commit()
        print(f"Focus {key}: {added} of {len(found)} known players from stored games added to the focus pool.")
        return
    players = []
    if args.riot_id:
        name, sep, tag = args.riot_id.rpartition("#")
        if not sep or not name or not tag:
            raise ValueError("Riot ID must be Game Name#TAG")
        account = client.get(PLATFORMS[args.platform]+".api.riotgames.com",
                             f"/riot/account/v1/accounts/by-riot-id/{quote(name,safe='')}/{quote(tag,safe='')}", "account")
        players = [{"puuid": account["puuid"]}]
        source = "manual Riot ID; convenience sample"
    else:
        for page in range(args.page, args.page + args.pages):
            rows = client.get(host, f"/lol/league/v4/entries/RANKED_SOLO_5x5/{args.tier}/{args.division}?page={page}", "ladder")
            players.extend(rows)
            if not rows:
                break
        random.Random(args.random_seed).shuffle(players)
        source = f"ladder {args.tier} {args.division} pages {args.page}-{args.page+args.pages-1}; rank at discovery only"
    added = 0
    for player in players[:args.players]:
        puuid = player.get("puuid")
        if not puuid:
            raise RuntimeError("Ladder response has no PUUID. Use --riot-id seeds; do not guess identities.")
        db.execute("INSERT OR IGNORE INTO players(puuid,platform,source,observed_at) VALUES(?,?,?,?)",
                   (puuid,args.platform,source,utc()))
        added += db.execute("SELECT changes()").fetchone()[0]
        if key:
            db.execute("UPDATE players SET focus=? WHERE puuid=? AND platform=? AND focus IS NULL", ("seed:" + key, puuid, args.platform))
    db.commit()
    print(f"Added {added} player seeds. Rank filters remain unavailable: these are discovery snapshots.")

def check_player(db, client, player, args, since):
    """Ask Riot for one player's recent ranked games, queue the unknown ones, record the check. Returns new match ids."""
    start = 0 if args.refresh else player["cursor"]
    query = dict(queue=420,start=start,count=args.per_player)
    if since:
        query["startTime"] = since
    path = f"/lol/match/v5/matches/by-puuid/{quote(player['puuid'],safe='')}/ids?{urlencode(query)}"
    ids = client.get(PLATFORMS[args.platform]+".api.riotgames.com", path, "match-list")
    found, key = [], focus_key(args)
    # Focused games are tagged, so an export can tell a targeted sample from the general crawl.
    source = f"focus {key}" if key else player["source"]
    for match_id in ids:
        if re.fullmatch(r"[A-Z0-9]+_\d+", match_id) and db.execute(
                "INSERT OR IGNORE INTO matches(id,platform,source) VALUES(?,?,?)", (match_id,args.platform,source)).rowcount:
            found.append(match_id)
    if not args.refresh:
        db.execute("UPDATE players SET cursor=? WHERE puuid=? AND platform=?", (start+len(ids),player["puuid"],args.platform))
    checked = utc()
    db.execute("UPDATE players SET last_checked=?, last_active=CASE WHEN ? THEN ? ELSE last_active END WHERE puuid=? AND platform=?",
               (checked, bool(found), checked, player["puuid"], args.platform))
    db.commit()
    return found

def discover(db, client, args):
    if args.smart:
        # Check players who recently had new games every round; everyone else only every --recheck-hours.
        now = datetime.now(timezone.utc)
        players = db.execute(
            "SELECT * FROM players WHERE platform=? AND (last_checked IS NULL OR last_active>=? OR last_checked<=?) "
            "ORDER BY last_active IS NULL, last_active DESC, last_checked LIMIT ?",
            (args.platform, (now - timedelta(hours=args.active_hours)).isoformat(),
             (now - timedelta(hours=args.recheck_hours)).isoformat(), args.players)).fetchall()
        if not players:
            print("Discover: no player is due for a check yet.")
            return
    else:
        players = db.execute("SELECT * FROM players WHERE platform=? ORDER BY observed_at,puuid LIMIT ?", (args.platform,args.players)).fetchall()
    if not players:
        raise ValueError("No player seeds. Run seed first.")
    since = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()) if args.since else None
    new = 0
    for n, player in enumerate(players, 1):
        new += len(check_player(db, client, player, args, since))
        if n % 25 == 0 or n == len(players):
            print(f"Discover: checked {n}/{len(players)} players, {new} new games", flush=True)
    print("Match queue saved. Repeated IDs are deduplicated.")

def valid_match(info):
    players = info.get("participants",[])
    teams = Counter(p.get("teamId") for p in players)
    return (info.get("queueId") == 420 and info.get("mapId") == 11
            and info.get("gameDuration",0) >= 300 and len(info.get("participants",[])) == 10
            and teams == Counter({100:5,200:5}) and len({p.get("participantId") for p in players})==10
            and all("win" in p and "championName" in p for p in players)
            and len({p["win"] for p in players if p["teamId"]==100})==1
            and len({p["win"] for p in players if p["teamId"]==200})==1
            and sum(bool(p["win"]) for p in players)==5
            and bool(info.get("gameVersion")) and bool(info.get("gameStartTimestamp"))
            and not any(p.get("gameEndedInEarlySurrender",False) for p in players))

SNOWBALL_CAP = 100000  # players kept per platform; the pool stops growing here

def snowball(db, puuids, args):
    """--snowball: every player in a kept game becomes a crawl player too, so the pool grows with the games.

    Players who never check out are pruned by purge() after PLAYER_IDLE_DAYS."""
    if not getattr(args, "snowball", False) or not puuids:
        return 0
    room = getattr(args, "snowball_cap", SNOWBALL_CAP) - db.execute("SELECT COUNT(*) FROM players WHERE platform=?", (args.platform,)).fetchone()[0]
    if room <= 0:
        return 0
    new = [p for p in dict.fromkeys(puuids)
           if not db.execute("SELECT 1 FROM players WHERE puuid=? AND platform=?", (p, args.platform)).fetchone()][:room]
    now = utc()
    db.executemany("INSERT INTO players(puuid,platform,source,observed_at) VALUES(?,?,'snowball',?)",
                   [(p, args.platform, now) for p in new])
    return len(new)

def collect_match(db, client, match_id, detail, args):
    """Download one queued game: details, then the timeline if it passes the patch/champion filters. True if saved."""
    path = f"/lol/match/v5/matches/{match_id}"
    host = PLATFORMS[args.platform]+".api.riotgames.com"
    try:
        match = json.loads(detail) if detail else client.get(host,path,"match")
        info = match.get("info",{})
        # the other nine players, read before minimize() replaces their IDs with pseudonyms
        others = [p["puuid"] for p in info.get("participants",[]) if p.get("puuid")]
        wanted = args.champion == "all" or any(plays_focus(p, args) for p in info.get("participants",[]))
        # Stored details are already pseudonymous; only a fresh download still has PUUIDs to promote.
        focused = [p["puuid"] for p in info.get("participants",[]) if p.get("puuid") and args.champion != "all" and plays_focus(p, args)]
        db.execute("UPDATE matches SET detail=?,collected_at=? WHERE id=?", (json.dumps(minimize(match)),utc(),match_id))
        if not valid_match(info):
            db.execute("UPDATE matches SET status='invalid' WHERE id=?", (match_id,))
            db.commit()
            return False
        db.commit()
        patch = ".".join(info["gameVersion"].split(".")[:2])
        if args.patch and patch != args.patch:
            return False
        if not wanted:
            return False
        if focus_key(args):
            promote(db, focused, args)
        else:
            snowball(db, others, args)
        # Snowball inserts start a write transaction. Never hold that lock while
        # waiting on the Riot timeline request: the other region must keep writing.
        db.commit()
        timeline = client.get(host,path+"/timeline","timeline")
        if timeline.get("metadata",{}).get("matchId") != match_id or not timeline.get("info",{}).get("frames"):
            raise ValueError("Timeline missing or mismatched; refusing ingestion.")
        try:
            catalog(db,patch)
        except ValueError:
            pass  # no item list for this or any earlier patch; export handles it
        db.execute("UPDATE matches SET timeline=?,status='done',collected_at=? WHERE id=?", (json.dumps(minimize(timeline)),utc(),match_id))
        db.commit()
        return True
    except RiotError as error:
        if error.status != 404:
            raise
        db.execute("UPDATE matches SET status='missing' WHERE id=?", (match_id,))
        db.commit()
        return False

def queued(db, args, limit):
    # A focused crawl leaves the general crawl's queue alone; it would spend the key on untargeted games.
    key = focus_key(args)
    return db.execute("SELECT id, detail FROM matches WHERE platform=? AND timeline IS NULL AND status NOT IN ('invalid','missing') "
                      f"{'AND source=? ' if key else ''}ORDER BY collected_at IS NOT NULL,collected_at,id DESC LIMIT ?",
                      (args.platform, *([f"focus {key}"] if key else []), limit)).fetchall()

def collect(db, client, args):
    purge(db, client, args, quiet=True)
    rows = queued(db, args, args.max_scan)
    saved = 0
    for i,row in enumerate(rows):
        if collect_match(db, client, row["id"], row["detail"], args):
            saved += 1
            print(f"Saved {saved}/{args.max_matches} timelines; scanned {i+1}/{len(rows)}", flush=True)
            if saved >= args.max_matches:
                break
    print(f"Collection complete: {saved} new timelines. Re-run to continue queued work.")

def crawl(db, client, args):
    """Continuous crawler: check the player who has waited longest, download their new games at once, repeat.

    Players are rechecked every --recheck-hours; with nobody due it waits a minute. Stops only when
    interrupted or when Riot rejects the key (which also ends the enclosing shell loop)."""
    purge(db, client, args, quiet=True)
    since = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()) if args.since else None
    key = focus_key(args)
    print(f"== crawl start {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (recheck {args.recheck_hours:g} h, patch {args.patch or 'any'}, platform {args.platform}{', snowball' if getattr(args, 'snowball', False) else ''}{f', focus {key}' if key else ''})", flush=True)
    backlog = queued(db, args, 100000)
    if backlog:
        got = sum(collect_match(db, client, r["id"], r["detail"], args) for r in backlog)
        print(f"Crawl: queue cleared, {got} of {len(backlog)} waiting games saved", flush=True)
    checked = saved = 0
    began, last_purge, idle = time.monotonic(), time.monotonic(), False
    pool_sql, pool_args = ("platform=? AND focus IN (?,?)", (args.platform, key, "seed:" + key)) if key else ("platform=?", (args.platform,))
    due_sql = f"FROM players WHERE {pool_sql} AND (last_checked IS NULL OR last_checked<=?)"
    while True:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.recheck_hours)).isoformat()
        player = db.execute(f"SELECT * {due_sql} ORDER BY last_checked IS NOT NULL, last_checked LIMIT 1", (*pool_args, cutoff)).fetchone()
        if player is None:
            if not idle:
                oldest = db.execute(f"SELECT MIN(last_checked) FROM players WHERE {pool_sql}", pool_args).fetchone()[0]
                at = (datetime.fromisoformat(oldest) + timedelta(hours=args.recheck_hours)).astimezone().strftime("%H:%M") if oldest else "?"
                print(f"Crawl: nobody due; next player due at {at}", flush=True)
                idle = True
            time.sleep(60)
            continue
        idle = False
        new_ids = check_player(db, client, player, args, since)
        checked += 1
        got = sum(collect_match(db, client, mid, None, args) for mid in new_ids)
        saved += got
        if key:
            # A ladder seed has done its job once checked; it stays only if its own games promoted it.
            db.execute("UPDATE players SET focus=NULL WHERE puuid=? AND platform=? AND focus=?", (player["puuid"], args.platform, "seed:" + key))
            db.commit()
        if new_ids:
            print(f"Crawl: player {checked:,}: {len(new_ids)} new game{'s' if len(new_ids) != 1 else ''}, {got} saved", flush=True)
        if checked % 50 == 0:
            due = db.execute(f"SELECT COUNT(*) {due_sql}", (*pool_args, cutoff)).fetchone()[0]
            pool = db.execute(f"SELECT COUNT(*) FROM players WHERE {pool_sql}", pool_args).fetchone()[0]
            hours = max((time.monotonic() - began) / 3600, 1 / 60)
            print(f"Crawl: {checked:,} players checked, {saved:,} games saved this session ({saved / hours:.0f}/h), {due:,} of {pool:,} players due now", flush=True)
        if time.monotonic() - last_purge > 6 * 3600:
            purge(db, client, args, quiet=True)
            last_purge = time.monotonic()

def opponent_for(participants, player):
    role = player.get("teamPosition")
    if role not in ROLES:
        return None
    opponents = [p for p in participants if p.get("teamId") != player["teamId"] and p.get("teamPosition") == role]
    allies = [p for p in participants if p.get("teamId") == player["teamId"] and p.get("teamPosition") == role]
    return opponents[0] if len(opponents) == len(allies) == 1 else None

def purchases(timeline, pid):
    """Net transaction ledger. Unknown undo mapping makes the ledger ineligible."""
    operations, uncertain = [], False
    events = sorted((e for f in timeline["info"]["frames"] for e in f.get("events",[]) if e.get("participantId") == pid), key=lambda e:e.get("timestamp",0))
    for event in events:
        kind = event.get("type")
        if kind in ("ITEM_PURCHASED","ITEM_SOLD"):
            operations.append(dict(kind=kind,item=event["itemId"],time=event["timestamp"],active=True))
        elif kind == "ITEM_UNDO":
            before, after = event.get("beforeId",0), event.get("afterId",0)
            target_kind, item = ("ITEM_PURCHASED",before) if before else ("ITEM_SOLD",after)
            target = next((o for o in reversed(operations) if o["active"] and o["kind"] == target_kind and o["item"] == item), None)
            if target is None or (before and after):
                uncertain = True
            else:
                target["active"] = False
                # A later undo must not rewrite the first-minute treatment.
                if target["time"] <= 60000 < event["timestamp"]:
                    uncertain = True
    return operations, uncertain

def pre_features(timeline, participants, player, timestamp):
    # Strictly preceding snapshots only. Missing is not treated as zero.
    frame = next((f for f in sorted(timeline["info"]["frames"],key=lambda f:f.get("timestamp",0),reverse=True) if f.get("timestamp",0) < timestamp), None)
    if frame is None:
        return {"snapshotAvailable":False}
    frames = frame.get("participantFrames",{})
    own = frames.get(str(player["participantId"]))
    if own is None:
        return {"snapshotAvailable":False}
    ally = [frames.get(str(p["participantId"]),{}) for p in participants if p["teamId"] == player["teamId"]]
    enemy = [frames.get(str(p["participantId"]),{}) for p in participants if p["teamId"] != player["teamId"]]
    complete = len(ally) == len(enemy) == 5 and all("totalGold" in f for f in ally+enemy)
    return {"snapshotAvailable":True,"snapshotAgeMs":timestamp-frame["timestamp"],
            "currentGoldSnapshot":own.get("currentGold"),"levelSnapshot":own.get("level"),
            "teamGoldDifferenceSnapshot":sum(f["totalGold"] for f in ally)-sum(f["totalGold"] for f in enemy) if complete else None,
            "budgetExact":False}

def item_class(item):
    """'legendary' for finished items, 'boots' for upgraded boots, otherwise None."""
    tags = set(item.get("tags", []))
    if item.get("maps", {}).get("11") is False or tags & {"Consumable", "Trinket"}:
        return None
    if "Boots" in tags:
        return "boots" if item.get("from") else None
    if not item.get("into") and item.get("gold", {}).get("total", 0) >= 2000:
        return "legendary"
    return None

def build_path(buys, items, slots=5):
    """Finished items in completion order, plus the first upgraded boots and how many items came before them."""
    build, boots, seen = [], None, set()
    for o in sorted(buys, key=lambda o: o["time"]):
        iid = str(o["item"])
        kind = item_class(items.get(iid, {}))
        name = items.get(iid, {}).get("name", f"Item {iid}")
        if kind == "legendary" and iid not in seen and len(build) < slots:
            seen.add(iid)
            build.append((iid, name, o["time"] / 60000))
        elif kind == "boots" and boots is None and not any(item_class(items.get(str(part), {})) == "boots" for part in items.get(iid, {}).get("from", [])):
            # tier-3 boot upgrades are built from upgraded boots: a later purchase, not the boots choice
            boots = (iid, name, o["time"] / 60000, len(build))
    return build, boots

def recipe(item_id, items, seen=None):
    """Every component in an item's recipe tree."""
    seen = set() if seen is None else seen
    for part in items.get(str(item_id), {}).get("from", []):
        if part not in seen:
            seen.add(part)
            recipe(part, items, seen)
    return seen

def first_component(buys, build, items, big=700):
    """The first component of the first finished item bought after the start, preferring a big one (e.g. Phage over a Long Sword)."""
    if not build:
        return None
    parts = recipe(build[0][0], items)
    bought = [o for o in sorted(buys, key=lambda o: o["time"]) if o["time"] > 60000 and str(o["item"]) in parts and o["time"] / 60000 <= build[0][2]]
    pick = next((o for o in bought if items.get(str(o["item"]), {}).get("gold", {}).get("total", 0) >= big), bought[0] if bought else None)
    if pick is None:
        return None
    iid = str(pick["item"])
    return (iid, items.get(iid, {}).get("name", f"Item {iid}"), pick["time"] / 60000)

SKILL_KEYS = {1: "Q", 2: "W", 3: "E"}

def skill_order(timeline, pid):
    """(max order, opener) from SKILL_LEVEL_UP events, e.g. ("Q>W>E", "W>E>Q"); None where unknown.

    EVOLVE level-ups (Kha'Zix, Kai'Sa, ...) are not skill points and are ignored, and so is R. The opener is the
    first three basic-ability points. The max order ranks Q/W/E by when each reached rank 5; it needs two of
    them maxed (the third follows), so games that end earlier have an opener but no max order."""
    events = sorted((e for f in timeline["info"]["frames"] for e in f.get("events", [])
                     if e.get("type") == "SKILL_LEVEL_UP" and e.get("participantId") == pid
                     and e.get("levelUpType", "NORMAL") != "EVOLVE" and e.get("skillSlot") in SKILL_KEYS),
                    key=lambda e: e.get("timestamp", 0))
    keys = [SKILL_KEYS[e["skillSlot"]] for e in events]
    opener = ">".join(keys[:3]) if len(keys) >= 3 else None
    ranks, maxed = Counter(), []
    for key in keys:
        ranks[key] += 1
        if ranks[key] == 5:
            maxed.append(key)
    if len(maxed) >= 2:
        maxed += [k for k in "QWE" if k not in maxed][:3 - len(maxed)]
    return (">".join(maxed) if len(maxed) == 3 else None), opener

def extract(match,timeline,items):
    info = match["info"]
    if not valid_match(info) or match.get("metadata",{}).get("matchId") != timeline.get("metadata",{}).get("matchId"):
        return []
    participants = info["participants"]
    records = []
    for player in participants:
        opponent = opponent_for(participants,player)
        if opponent is None:
            continue
        operations, uncertain = purchases(timeline,player["participantId"])
        uncertain = uncertain or not items  # no item data for this patch yet: no item statistics
        active = [o for o in operations if o["active"]]
        buy = [o for o in active if o["kind"] == "ITEM_PURCHASED"]
        if any(str(o["item"]) not in items for o in buy):
            uncertain = True  # unknown item, e.g. new this patch and missing from a provisional list
        starting = Counter(o["item"] for o in buy if o["time"] <= 60000 and items.get(str(o["item"]),{}).get("gold",{}).get("total",0)>0)
        for sale in active:
            if sale["kind"] == "ITEM_SOLD" and sale["time"] <= 60000:
                starting[sale["item"]] -= 1
        if any(n<0 for n in starting.values()):
            uncertain = True
        # Starting packages with upgrades cannot be reconstructed by net buys alone.
        upgraded_early = any(items.get(str(o["item"]),{}).get("from") for o in buy if o["time"] <= 60000)
        early_combat = any(e.get("type") == "CHAMPION_KILL" and e.get("timestamp",0)<=60000 for f in timeline["info"]["frames"] for e in f.get("events",[]))
        names = lambda item: items.get(str(item),{}).get("name",f"Item {item}")
        build, boots = build_path(buy, items) if not uncertain else ([], None)
        component = first_component(buy, build, items) if not uncertain else None
        package = "+".join(f"{item}x{n}" for item,n in sorted(starting.items()) if n>0)
        package_label = " + ".join(f"{names(item)}"+(f" ×{n}" if n>1 else "") for item,n in sorted(starting.items()) if n>0)
        skill_max, skill_start = skill_order(timeline, player["participantId"])
        rune_ids = sorted(s["perk"] for style in player.get("perks",{}).get("styles",[]) for s in style.get("selections",[]))
        shards = player.get("perks",{}).get("statPerks",{})
        rune_page = ",".join(map(str,rune_ids))+"|"+",".join(f"{k}:{v}" for k,v in sorted(shards.items()))
        features = {"champion":player["championName"],"opponent":opponent["championName"],"role":player["teamPosition"],
                    "side":str(player["teamId"]),"patch":".".join(info["gameVersion"].split(".")[:2]),
                    "spells":"+".join(map(str,sorted([player.get("summoner1Id",0),player.get("summoner2Id",0)]))),"runes":rune_page}
        for p in participants:
            side = "ally" if p["teamId"] == player["teamId"] else "enemy"
            features[f"{side}:{p.get('teamPosition','UNKNOWN')}:{p['championName']}"] = 1
        records.append({"matchId":match["metadata"]["matchId"],"playerId":player.get("playerRef",""),
                        "startedAt":info["gameStartTimestamp"],"champion":player["championName"],"opponent":opponent["championName"],
                        "role":player["teamPosition"],"patch":features["patch"],"region":info.get("platformId","UNKNOWN"),"win":int(player["win"]),
                        **{k:int(bool(player.get(k))) for k in FIRSTS},
                        "features":features,"package":package if package and not uncertain and not upgraded_early and not early_combat else None,
                        "packageLabel":package_label,"runes":rune_page if rune_ids else None,"spells":features["spells"],
                        "keystone":str(player["perks"]["styles"][0]["selections"][0]["perk"]) if player.get("perks",{}).get("styles") and player["perks"]["styles"][0].get("selections") else None,
                        "skillMax":skill_max,"skillStart":skill_start,
                        "ledgerUncertain":uncertain,"build":build,"boots":boots,"component":component,"purchases":[] if uncertain else [dict(o,name=names(o["item"]),pre=pre_features(timeline,participants,player,o["time"])) for o in buy]})
    return records

_missing_catalogs = set()

def dataset(db, champion="all"):
    path = db.execute("PRAGMA database_list").fetchone()[2]
    try:
        from features import load_records  # same records, parsed once per match, parallel and cached
    except ImportError:  # numpy is optional for collection-only installs
        load_records = None
    if path and load_records:
        for record in load_records(db, path):
            if champion == "all" or record["champion"].lower() == champion.lower():
                yield record
        return
    for row in db.execute(f"SELECT * FROM matches WHERE {USABLE_SQL} ORDER BY id"):
        match,timeline = json.loads(row["detail"]),json.loads(row["timeline"])
        patch = ".".join(match["info"]["gameVersion"].split(".")[:2])
        cat = db.execute("SELECT items FROM catalog WHERE patch=?",(patch,)).fetchone()
        if cat is None and patch not in _missing_catalogs:
            try:
                catalog(db,patch)
                cat = db.execute("SELECT items FROM catalog WHERE patch=?",(patch,)).fetchone()
            except (ValueError,URLError):
                _missing_catalogs.add(patch)
                print(f"Patch {patch}: Data Dragon item list not published yet; exporting its games without item statistics.")
        for record in extract(match,timeline,json.loads(cat["items"]) if cat else {}):
            if champion == "all" or record["champion"].lower() == champion.lower():
                yield record

def aggregate(records, include_paths=True):
    buckets = {}
    for r in records:
        key = (r["champion"],r["opponent"],r["role"],r["patch"],r["region"])
        if key not in buckets:
            buckets[key] = dict(zip(("champion","opponent","role","patch","region"),key), games=0,wins=0,**dict.fromkeys(FIRSTS,0),eligible={"packages":0,"items":0,"runes":0,"spells":0,"build":0,"keystone":0,"skills":0},choices={})
        b = buckets[key]
        b["games"] += 1
        b["wins"] += r["win"]
        for k in FIRSTS:
            b[k] += r.get(k,0)
        # Keep timings and comparisons within one observed first-item / boots-order
        # cohort. Never reconstruct path timing by combining unrelated slot averages.
        if include_paths and not r["ledgerUncertain"] and r.get("build") and r.get("boots"):
            first_id = str(r["build"][0][0])
            boots_before = r["boots"][3]
            path_id = f"{first_id}:{boots_before}"
            b.setdefault("_paths", {}).setdefault(path_id, []).append(r)
        if include_paths and not r["ledgerUncertain"] and len(r.get("build") or []) >= 3:
            core = r["build"][:3]
            w = b.setdefault("_builds", {}).setdefault(">".join(i for i,_,_ in core),
                dict(items=[i for i,_,_ in core], names=[n for _,n,_ in core], games=0, wins=0, timeSum=[0,0,0], components={}, boots={}))
            w["games"] += 1
            w["wins"] += r["win"]
            for n,(_,_,minute) in enumerate(core):
                w["timeSum"][n] += minute
            if r.get("component"):
                cid, cname, cmin = r["component"]
                comp = w["components"].setdefault(cid, dict(name=cname, games=0, wins=0, timeSum=0))
                comp["games"] += 1; comp["wins"] += r["win"]; comp["timeSum"] += cmin
            if r.get("boots"):
                bid, bname, bmin, before = r["boots"]
                boot = w["boots"].setdefault(f"{bid}:{min(before,3)}", dict(name=bname, games=0, wins=0, timeSum=0))
                boot["games"] += 1; boot["wins"] += r["win"]; boot["timeSum"] += bmin
            # Route cohort: the same core bought with the same boots at the same position and the same first
            # component. Every time in one route is summed over the same games, so a displayed route never
            # combines averages from different groups of players. "-" marks no boots / no component.
            bid, bname, bmin, before = r["boots"] if r.get("boots") else ("-", "", 0, 0)
            cid, cname, cmin = r["component"] if r.get("component") else ("-", "", 0)
            route = w.setdefault("routes", {}).setdefault(f"{bid}:{min(before,3)}:{cid}",
                dict(bootsName=bname, componentName=cname, games=0, timeSum=[0,0,0], bootsTimeSum=0, componentTimeSum=0))
            route["games"] += 1
            for n,(_,_,minute) in enumerate(core):
                route["timeSum"][n] += minute
            route["bootsTimeSum"] += bmin
            route["componentTimeSum"] += cmin
        entries = []
        if r["package"]:
            b["eligible"]["packages"] += 1
            entries.append(("packages",r["package"],r["packageLabel"],None))
        if not r["ledgerUncertain"]:
            b["eligible"]["items"] += 1
            unique = {}
            for o in r["purchases"]:
                unique.setdefault(o["item"],o)
            entries.extend(("items",str(item),o["name"],o["time"] / 60000) for item,o in unique.items())
            # Build order: slot1..slot5 are finished items in completion order; boots record which pair
            # and how many finished items came before them (bootsTiming).
            b["eligible"]["build"] += 1
            entries.extend((f"slot{n}",iid,name,minute) for n,(iid,name,minute) in enumerate(r.get("build",[]),1))
            if len(r.get("build",[])) >= 3:
                core = r["build"][:3]
                entries.append(("core",">".join(i for i,_,_ in core)," → ".join(n for _,n,_ in core),core[2][2]))
            if r.get("component"):
                entries.append(("component",*r["component"]))
            if r.get("boots"):
                iid,name,minute,before = r["boots"]
                entries.append(("boots",iid,name,minute))
                entries.append(("bootsTiming",str(min(before,3)),str(min(before,3)),minute))
        if r.get("keystone"):
            b["eligible"]["keystone"] += 1
            entries.append(("keystone",r["keystone"],r["keystone"],None))
        # Skill order: eligible once the opener is known; the max order is known only in the subset of those
        # games long enough to max two abilities, so skillMax shares use their own total.
        if r.get("skillStart"):
            b["eligible"]["skills"] += 1
            entries.append(("skillStart",r["skillStart"],r["skillStart"],None))
            if r.get("skillMax"):
                entries.append(("skillMax",r["skillMax"],r["skillMax"],None))
        for kind in ("runes","spells"):
            if r[kind]:
                b["eligible"][kind] += 1
                entries.append((kind,r[kind],r[kind],None))
        for kind,ident,label,minutes in entries:
            c = b["choices"].setdefault(kind+":"+ident,dict(kind=kind,id=ident,label=label,games=0,wins=0,timeSum=0,timeCount=0))
            c["games"] += 1
            c["wins"] += r["win"]
            p = r.get("wpa",{}).get((kind,ident))
            if p is not None:
                resid = r["win"] - p
                c["residSum"] = c.get("residSum",0) + resid
                c["residSq"] = c.get("residSq",0) + resid * resid
                c["residN"] = c.get("residN",0) + 1
            curve = r.get("curve",{}).get((kind,ident))
            if curve is not None:
                c["curveN"] = c.get("curveN",0) + 1
                # curve[0] is the model's raw win chance just before the purchase: "bought when ahead or behind"
                c["preSum"] = c.get("preSum",0.0) + curve[0]
                sums, squares = c.setdefault("curveSum",[0.0]*(len(curve)-1)), c.setdefault("curveSq",[0.0]*(len(curve)-1))
                for k in range(1, len(curve)):
                    d = curve[k] - curve[0]
                    sums[k-1] += d
                    squares[k-1] += d * d
            lane = r.get("lane",{}).get((kind,ident))
            if lane is not None:
                delta, predicted = lane
                c["laneSum"] = c.get("laneSum",0) + delta - predicted
                c["laneSq"] = c.get("laneSq",0) + (delta - predicted) ** 2
                c["laneN"] = c.get("laneN",0) + 1
                c["laneDelta"] = c.get("laneDelta",0) + delta
                c["laneUp"] = c.get("laneUp",0) + (1 if delta > 0 else 0)
            if minutes is not None:
                c["timeSum"] += minutes
                c["timeCount"] += 1
    for b in buckets.values():
        for c in b["choices"].values():
            for key in ("curveSum","curveSq"):
                if key in c:
                    c[key] = [round(v, 5) for v in c[key]]
        b["choices"] = list(b["choices"].values())
        if include_paths:
            b["builds"] = [dict(id=k, **v) for k, v in b.pop("_builds", {}).items()]
            b["paths"] = []
            for path_id, path_records in b.pop("_paths", {}).items():
                sub = aggregate(path_records, include_paths=False)[0]
                first_id, boots_before = path_id.split(":")
                b["paths"].append({"id":path_id, "firstItem":first_id, "bootsBefore":int(boots_before),
                                   "games":sub["games"], "wins":sub["wins"],
                                   "eligible":{"build":sub["eligible"]["build"]},
                                   "choices":[{k:v for k,v in c.items() if k not in ("curveSum","curveSq","curveN")} for c in sub["choices"] if c["kind"] in
                                              {"slot1","slot2","slot3","slot4","slot5","boots","bootsTiming","core"}]})
    return list(buckets.values())

def export(db, args):
    purge(db, None, args, quiet=True)
    return _export(db, args)

def slim(record):
    """Keep only what the export counts. The per-purchase game state ("pre") and the draft features are
    large and only used by the model scripts; holding them for every player-game made the export need
    tens of gigabytes of memory."""
    record.pop("features", None)
    record["purchases"] = [{"item":o["item"],"time":o["time"],"name":o["name"]} for o in record["purchases"]]
    return record

def _export(db, args):
    records = [slim(r) for r in dataset(db,args.champion)]
    # WPA prototype: predictions written by pipeline/wpa.py, matched to each player's build decisions.
    predictions = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wpa_predictions'").fetchone():
        for m,player,kind,item,p in db.execute("SELECT match_id,player_ref,kind,item,p FROM wpa_predictions"):
            predictions.setdefault((m,player),{})[(kind,item)] = p
    curves = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wpa_curves'").fetchone():
        for m,player,kind,item,curve in db.execute("SELECT match_id,player_ref,kind,item,curve FROM wpa_curves"):
            curves.setdefault((m,player),{})[(kind,item)] = [float(v) for v in curve.split(",")]
    lanes = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lane_predictions'").fetchone():
        for m,player,kind,item,delta,pred in db.execute("SELECT match_id,player_ref,kind,item,delta,predicted FROM lane_predictions"):
            lanes.setdefault((m,player),{})[(kind,item)] = (delta,pred)
    for r in records:
        r["wpa"] = predictions.get((r["matchId"],r["playerId"]),{})
        r["lane"] = lanes.get((r["matchId"],r["playerId"]),{})
        r["curve"] = curves.get((r["matchId"],r["playerId"]),{})
    lane_file = ROOT/"data"/"public"/"lane_model.json"
    lane_model = json.loads(lane_file.read_text(encoding="utf-8")) if lanes and lane_file.exists() else None
    model_file = ROOT/"data"/"public"/"wpa_model.json"
    model = json.loads(model_file.read_text(encoding="utf-8")) if predictions and model_file.exists() else None
    source_id = "riot-match-v5"
    buckets = aggregate(records)
    first_objectives = {}
    for bucket in buckets:
        bucket["sourceId"] = source_id
        role = first_objectives.setdefault(bucket["role"], dict.fromkeys(("games",)+FIRSTS, 0))
        for k in ("games",)+FIRSTS:
            role[k] += bucket[k]
    payload = {"schemaVersion":2,"generatedAt":utc(),"status":"observed" if records else "empty",
               "wpaStatus":"prototype" if model else "unavailable",
               "wpaModel":{k:model[k] for k in ("generatedAt","method","games","snapshots","decisions","slotOffsetsPp")}
                          | {k:model["inGame"][k] for k in ("auc","calibrationErrorPp","brier")} if model else None,
               "laneModel":lane_model,
               "itemDataMissing":sorted(_missing_catalogs),
               "itemDataProvisional":{r["patch"]:r["version"][len(PROVISIONAL):] for r in db.execute("SELECT patch,version FROM catalog") if r["version"].startswith(PROVISIONAL)},
               "rankStatus":"unavailable: no historical participant rank snapshots",
               "samplePolicy":"Ladder/manual-seeded convenience sample, not a representative population estimate.",
               "uniqueMatches":len({r['matchId'] for r in records}),
               "firstObjectives":first_objectives,
               "sources":[{"id":source_id,"name":"Riot Match-V5 timelines","type":"riot_match_timelines",
                           "generatedAt":utc(),"supportsWpaResearch":True,
                           "note":"Locally collected match details and timelines; convenience sample."}],
               "buckets":buckets}
    payload["reliability"], payload["pooledEffects"] = reliability(records)
    write_json(ROOT/"data"/"public"/"stats.json",payload)
    for kind, minutes in payload["reliability"].get("curvePooled", {}).items():
        print(f"Reliability pooled curve {kind}: " + ", ".join(f"{k}m r={v['splitHalfR']:+.2f}{'*' if v['pass'] else ''}" for k, v in minutes.items()))
    for kind, minutes in payload["reliability"].get("curve", {}).items():
        print(f"Reliability curve {kind}: " + ", ".join(f"{k}m r={v['splitHalfR']:+.2f}{'*' if v['pass'] else ''}" for k, v in minutes.items()))
    for metric, kinds in payload["reliability"].items():
        if metric in ("curve", "curvePooled"):
            continue
        passed = [k for k, v in kinds.items() if v["pass"]]
        print(f"Reliability {metric}: " + ", ".join(f"{k} r={v['splitHalfR']:+.2f}" for k, v in kinds.items()) + f" -> shown: {', '.join(passed) or 'none'}")
    print(f"Exported {payload['uniqueMatches']} matches, {len(payload['buckets'])} matchup groups; no player IDs published.")

# ---- Reliability: only publish per-item effects that reproduce ----
# Per-item win impact and lane gold are averages over few noisy games. Before the site may show them,
# each slot type must pass a split-half test: estimates from even and odd matches must agree across
# items (correlation >= RELIABILITY_R over at least RELIABILITY_GROUPS items). A placebo (item labels
# shuffled within each slot) shows how many items look "significant" by chance alone.
RELIABILITY_R = 0.4
RELIABILITY_GROUPS = 20
RELIABILITY_MIN_N = 30
TARGET = {"wpa": 0.02, "lane": 100}  # precision the site asks for: +-2 pp win chance, +-100 gold

def _obs_arrays(obs):
    """obs: (slot, item, match, value) tuples -> slot codes, item codes, split half (by match), values, width."""
    import numpy as np
    slots, items, halves = {}, {}, {}
    slot = np.fromiter((slots.setdefault(o[0], len(slots)) for o in obs), np.int64, len(obs))
    item = np.fromiter((items.setdefault(o[1], len(items)) for o in obs), np.int64, len(obs))
    half = np.fromiter((halves.setdefault(o[2], zlib.crc32(o[2].encode()) % 2) for o in obs), np.int8, len(obs))
    value = np.fromiter((o[3] for o in obs), np.float64, len(obs))
    return slot, item, half, value, max(len(items), 1)

def _slot_estimates(slot, item, value, width, min_n):
    """Per (slot, item) group with n >= min_n: sorted keys (slot * width + item), mean minus slot mean,
    95% half-width and n. Vectorized group sums; population SD in two passes for accuracy."""
    import numpy as np
    if not len(value):
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0), np.zeros(0, np.int64)
    keys, g = np.unique(slot * width + item, return_inverse=True)
    n = np.bincount(g)
    mean = np.bincount(g, value) / n
    sd = np.sqrt(np.bincount(g, (value - mean[g]) ** 2) / n)
    ref = np.bincount(slot, value) / np.maximum(np.bincount(slot), 1)
    keep = n >= min_n
    return keys[keep], (mean - ref[keys // width])[keep], (1.96 * sd / np.sqrt(n))[keep], n[keep]

def _reliability(obs, target, shuffles=10, seed=1):
    import numpy as np
    from scipy.stats import rankdata
    slot, item, half, value, width = _obs_arrays(obs)
    keys, effect, width95, _ = _slot_estimates(slot, item, value, width, RELIABILITY_MIN_N)
    halves = [_slot_estimates(slot[half == k], item[half == k], value[half == k], width, 1) for k in (0, 1)]
    common = np.intersect1d(np.intersect1d(keys, halves[0][0]), halves[1][0])
    a = halves[0][1][np.searchsorted(halves[0][0], common)]
    b = halves[1][1][np.searchsorted(halves[1][0], common)]
    # Rank (Spearman) correlation: a single extreme item, e.g. a rare late upgrade bought only when already
    # winning, would otherwise make both halves "agree" while every other item is noise.
    r = float(np.corrcoef(rankdata(a), rankdata(b))[0, 1]) if len(common) >= 3 and len(np.unique(a)) > 1 and len(np.unique(b)) > 1 else 0.0
    # Placebo: item labels permuted within each slot (random order inside each slot's block).
    rng, grouped, placebo = np.random.default_rng(seed), np.argsort(slot, kind="stable"), []
    for _ in range(shuffles):
        fake = np.empty_like(item)
        fake[grouped] = item[np.lexsort((rng.random(len(item)), slot))]
        _, e, h, _ = _slot_estimates(slot, fake, value, width, RELIABILITY_MIN_N)
        placebo.append(float(np.mean(np.abs(e) > h)) if len(e) else 0.0)
    sd = float(value.std()) if len(value) > 1 else 0.0
    return {"splitHalfR": round(r, 3), "groups": int(len(common)),
            "flagged": round(float(np.mean(np.abs(effect) > width95)), 4) if len(effect) else 0.0,
            "placebo": round(float(np.mean(placebo)), 4) if placebo else 0.0,
            "neededGames": int((1.96 * sd / target) ** 2) if sd else None,
            "pass": bool(len(common) >= RELIABILITY_GROUPS and r >= RELIABILITY_R)}

def reliability(records):
    """Reliability report per slot type, per champion and pooled by role, plus the pooled sums the site shows."""
    raw = {"wpa": [], "lane": []}
    for r in records:
        for (kind, item), p in (r.get("wpa") or {}).items():
            raw["wpa"].append(((r["champion"], r["role"], kind), item, r["matchId"], r["win"] - p))
        for (kind, item), (delta, predicted) in (r.get("lane") or {}).items():
            raw["lane"].append(((r["champion"], r["role"], kind), item, r["matchId"], delta - predicted))
    report, pooled = {}, {}
    for metric, obs in raw.items():
        # Pooled: centre each value on its own champion's slot mean, then group by role and slot only.
        slot_mean = {}
        for slot, _, _, v in obs:
            slot_mean.setdefault(slot, []).append(v)
        slot_mean = {s: statistics.fmean(v) for s, v in slot_mean.items()}
        centred = [((slot[1], slot[2]), item, m, v - slot_mean[slot]) for slot, item, m, v in obs]
        for kind in sorted({o[0][2] for o in obs}):
            report.setdefault(metric, {})[kind] = _reliability([o for o in obs if o[0][2] == kind], TARGET[metric])
            report.setdefault(metric + "Pooled", {})[kind] = _reliability([o for o in centred if o[0][1] == kind], TARGET[metric])
        sums = pooled.setdefault(metric, {})
        for (role, kind), item, _, v in centred:
            n, s1, s2 = sums.setdefault(role, {}).setdefault(kind, {}).get(item, (0, 0.0, 0.0))
            sums[role][kind][item] = (n + 1, s1 + v, s2 + v * v)
    # Win chance k minutes after buying, relative to the slot: checked separately for every minute.
    curve_obs = {}
    for r in records:
        for (kind, item), cv in (r.get("curve") or {}).items():
            for k in range(1, len(cv)):
                curve_obs.setdefault(k, []).append(((r["champion"], r["role"], kind), item, r["matchId"], cv[k] - cv[0]))
    curve_pooled = {}
    for k, obs in sorted(curve_obs.items()):
        # Pooled by role: centre on each champion's own slot mean, then group by role and slot.
        slot_mean = {}
        for slot, _, _, v in obs:
            slot_mean.setdefault(slot, []).append(v)
        slot_mean = {s: statistics.fmean(v) for s, v in slot_mean.items()}
        centred = [((slot[1], slot[2]), item, m, v - slot_mean[slot]) for slot, item, m, v in obs]
        for kind in sorted({o[0][2] for o in obs}):
            report.setdefault("curve", {}).setdefault(kind, {})[str(k)] = _reliability([o for o in obs if o[0][2] == kind], TARGET["wpa"], shuffles=5)
            report.setdefault("curvePooled", {}).setdefault(kind, {})[str(k)] = _reliability([o for o in centred if o[0][1] == kind], TARGET["wpa"], shuffles=5)
        for (role, kind), item, _, v in centred:
            entry = curve_pooled.setdefault(role, {}).setdefault(kind, {}).setdefault(item, [0, [0.0] * len(curve_obs), [0.0] * len(curve_obs)])
            if k == 1:
                entry[0] += 1
            entry[1][k - 1] += v
            entry[2][k - 1] += v * v
    pooled = {metric: {role: {kind: {item: [n, round(a, 6), round(b, 6)] for item, (n, a, b) in items.items() if n >= RELIABILITY_MIN_N}
                                  for kind, items in kinds.items()} for role, kinds in roles.items()} for metric, roles in pooled.items()}
    pooled["curve"] = {role: {kind: {item: [n, [round(x, 5) for x in s1], [round(x, 5) for x in s2]] for item, (n, s1, s2) in items.items() if n >= RELIABILITY_MIN_N}
                              for kind, items in kinds.items()} for role, kinds in curve_pooled.items()}
    return report, pooled

RETENTION_DAYS = 730  # promised in datenschutz.html: game data is deleted at the latest 24 months after the game
PLAYER_IDLE_DAYS = 30  # promised in datenschutz.html: account IDs go once no new game was found for this long

def drop_feature_cache(db):
    """The feature cache (pipeline/features.py) derives from stored matches, including player refs.
    Deleting or rewriting matches must not leave copies there. If a running training job holds the
    file, its rows are emptied instead; any leftover is also pruned on the next read."""
    path = db.execute("PRAGMA database_list").fetchone()[2]
    if not path:
        return
    cache = Path(path).with_name(Path(path).stem + ".features.sqlite")
    try:
        for suffix in ("", "-wal", "-shm"):
            Path(str(cache) + suffix).unlink(missing_ok=True)
    except OSError:
        with sqlite3.connect(cache, timeout=60) as held:
            held.execute("DELETE FROM matches")
        print("Feature cache in use: emptied instead of deleted.")

def purge(db, client, args, quiet=False):
    """Retention (GDPR Art. 5(1)(e)): delete games older than --retention-days, and crawl players
    older than that or without a new game for PLAYER_IDLE_DAYS."""
    days = getattr(args, "retention_days", RETENTION_DAYS)
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - days * 86400
    game_cutoff = int(cutoff * 1000)
    seed_cutoffs = (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
                    (now - timedelta(days=PLAYER_IDLE_DAYS)).isoformat())
    # The large JSON scan is read-only when nothing has expired. A no-op DELETE still holds
    # SQLite's single write lock throughout that scan and can stop other regional crawlers.
    expired_game = db.execute("SELECT 1 FROM matches WHERE detail IS NOT NULL "
                              "AND json_extract(detail,'$.info.gameStartTimestamp') < ? LIMIT 1",
                              (game_cutoff,)).fetchone()
    expired_seed = db.execute("SELECT 1 FROM players WHERE observed_at < ? "
                              "OR COALESCE(last_active, observed_at) < ? LIMIT 1", seed_cutoffs).fetchone()
    games = (db.execute("DELETE FROM matches WHERE detail IS NOT NULL "
                        "AND json_extract(detail,'$.info.gameStartTimestamp') < ?",
                        (game_cutoff,)).rowcount if expired_game else 0)
    seeds = (db.execute("DELETE FROM players WHERE observed_at < ? "
                        "OR COALESCE(last_active, observed_at) < ?", seed_cutoffs).rowcount if expired_seed else 0)
    db.commit()
    if games or seeds:
        db.execute("VACUUM")  # do not leave deleted rows readable in free pages
    if games:
        drop_feature_cache(db)
    if games or seeds or not quiet:
        print(f"Retention ({days} days): deleted {games} game(s) and {seeds} crawl player(s) (also removed after {PLAYER_IDLE_DAYS} days without a new game).")

def anonymize(db, client, args):
    """One-off: minimise every stored match and timeline collected before minimisation existed."""
    changed = 0
    for row in db.execute("SELECT id,detail,timeline FROM matches WHERE detail IS NOT NULL OR timeline IS NOT NULL").fetchall():
        detail = json.dumps(minimize(json.loads(row["detail"]))) if row["detail"] else None
        timeline = json.dumps(minimize(json.loads(row["timeline"]))) if row["timeline"] else None
        if detail != row["detail"] or timeline != row["timeline"]:
            db.execute("UPDATE matches SET detail=?,timeline=? WHERE id=?", (detail,timeline,row["id"]))
            changed += 1
    db.commit()
    db.execute("VACUUM")  # rewrite the file so removed values do not linger in free pages
    drop_feature_cache(db)
    print(f"Minimised {changed} stored matches; names and account IDs removed.")

def forget(db, client, args):
    """Erasure requests (e.g. Riot's GDPR deletion lists): one PUUID per line in --puuid-file."""
    puuids = [line.strip() for line in Path(args.puuid_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    refs = {"p:" + pseudonym(p) for p in puuids}
    seeds = sum(db.execute("DELETE FROM players WHERE puuid=?", (p,)).rowcount for p in puuids)
    matches = 0
    for row in db.execute("SELECT id,detail FROM matches WHERE detail IS NOT NULL").fetchall():
        players = json.loads(row["detail"]).get("info",{}).get("participants",[])
        if any(p.get("playerRef") in refs or p.get("puuid") in puuids for p in players):
            db.execute("DELETE FROM matches WHERE id=?", (row["id"],))
            matches += 1
    db.commit()
    db.execute("VACUUM")
    drop_feature_cache(db)
    print(f"Erased {seeds} seed player(s) and {matches} match(es) involving the listed players. Run export and rebuild to update the site.")

def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["seed","discover","collect","crawl","export","status","anonymize","forget","purge"])
    parser.add_argument("--puuid-file",help="forget: text file with one PUUID per line")
    parser.add_argument("--retention-days",type=int,default=RETENTION_DAYS,help="delete games older than this (default 730 = 24 months)")
    parser.add_argument("--db",default=str(ROOT/"data"/"settistics.sqlite"))
    parser.add_argument("--platform",choices=PLATFORMS,default="euw1")
    parser.add_argument("--riot-id")
    parser.add_argument("--tier",choices=["IRON","BRONZE","SILVER","GOLD","PLATINUM","EMERALD","DIAMOND"],default="EMERALD")
    parser.add_argument("--division",choices=["I","II","III","IV"],default="I")
    parser.add_argument("--page",type=int,default=1)
    parser.add_argument("--pages",type=int,default=1)
    parser.add_argument("--players",type=int,default=50)
    parser.add_argument("--random-seed",type=int,default=42)
    parser.add_argument("--per-player",type=int,default=50)
    parser.add_argument("--since",help="UTC YYYY-MM-DD; keep fixed for cursor-based discovery")
    parser.add_argument("--refresh",action="store_true",help="Refresh latest IDs without changing historical cursor")
    parser.add_argument("--smart",action="store_true",help="discover: check recently active players every round, others every --recheck-hours")
    parser.add_argument("--active-hours",type=float,default=6,help="--smart: a player counts as active this long after a new game")
    parser.add_argument("--recheck-hours",type=float,default=3,help="--smart: inactive players are checked again after this long")
    parser.add_argument("--snowball",action="store_true",help="crawl/collect: add the players of every kept game to the crawl pool")
    parser.add_argument("--snowball-cap",type=int,default=SNOWBALL_CAP,help="--snowball: stop growing the pool at this many players per platform (with --focus: focus players)")
    parser.add_argument("--focus",action="store_true",help="seed/crawl with --champion/--role: crawl only players of that champion and role")
    parser.add_argument("--from-games",action="store_true",help="seed --focus: take focus players from stored games instead of the ladder")
    parser.add_argument("--max-scan",type=int,default=1000)
    parser.add_argument("--max-matches",type=int,default=100)
    parser.add_argument("--champion",default="Sett",help="Riot champion ID or all")
    parser.add_argument("--role",choices=["ALL",*sorted(ROLES)],default="TOP")
    parser.add_argument("--patch",help="e.g. 16.18; omitted accepts all discovered patches")
    args = parser.parse_args()
    if args.retention_days < 1:
        parser.error("--retention-days must be at least 1")
    if not 1 <= args.per_player <= 100 or min(args.page,args.pages,args.players,args.max_scan,args.max_matches)<1:
        parser.error("Limits must be positive; --per-player must be 1..100")
    db = connect(args.db)
    try:
        if args.command == "export":
            export(db,args)
        elif args.command == "status":
            print(json.dumps({"players":db.execute("SELECT count(*) FROM players").fetchone()[0],"matches":dict(db.execute("SELECT status,count(*) FROM matches GROUP BY status").fetchall())},indent=2))
        elif args.command in ("anonymize","forget","purge"):
            if args.command == "forget" and not args.puuid_file:
                parser.error("forget needs --puuid-file")
            globals()[args.command](db,None,args)
        else:
            client = Client(os.getenv("RIOT_API_KEY", ""))
            globals()[args.command](db,client,args)
    finally:
        db.close()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped safely. Committed work is retained; re-run to resume.")
    except (ValueError,RuntimeError,URLError) as error:
        raise SystemExit(str(error))
