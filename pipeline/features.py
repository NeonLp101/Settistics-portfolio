"""Fast match features: vectorized per match, parallel across CPU cores, cached between runs.

Model fitting runs on the GPU. Parsing match JSON and rebuilding game state cannot, so this
module makes that CPU stage cheap: numpy replaces the per-moment Python loop, a process pool
uses every core, and each match is computed once and reused until the extraction code or the
patch's item catalog changes. One JSON parse per match serves both WPA training (arrays)
and the site export (engine.extract records).

The cache sits next to the database it derives from (data/settistics.features.sqlite) and is private. It holds pseudonymous player refs,
exactly like the main database, and is never published. Rows of matches that left the main
database (retention purge, erasure) are deleted on every read; purge, forget and anonymize
also drop the whole file (engine.drop_feature_cache).
"""
import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sqlite3
import zlib

import numpy as np

from engine import ROOT, USABLE_SQL, build_path, extract, opponent_for, purchases, valid_match
from wpa import Game, STATE, FEATURES, FOLDS, HORIZON, SNAPSHOT_STEP_MS

# Any edit to the extraction code invalidates every cached match; no manual version bumps.
CODE_VERSION = hashlib.sha256(b"".join((ROOT / "pipeline" / f).read_bytes()
                                       for f in ("engine.py", "wpa.py", "features.py"))).hexdigest()[:16]
COUNTS = ("kills", "towers", "inhibs", "dragons", "barons", "heralds", "grubs")


class FastGame(Game):
    """Game.state for many moments at once. Same rule: only the last frame strictly before t."""

    def __init__(self, match, timeline):
        super().__init__(match, timeline)
        self.ts = np.asarray(self.frame_ts, dtype=np.int64)
        pids = list(self.players)
        team = np.array([self.team[p] for p in pids])
        sums = lambda arr: {tm: np.asarray([arr[p] for p in pids], dtype=np.int64)[team == tm].sum(0) for tm in (100, 200)}
        self.team_gold, self.team_xp = sums(self.gold), sums(self.xp)
        self.arr = {k: {p: np.asarray(v[p], dtype=np.int64) for p in pids} for k, v in (("gold", self.gold), ("level", self.level))}
        self.sorted_scores = {k: np.asarray(v, dtype=np.int64) for k, v in self.scores.items()}

    def states(self, pid, opp, times):
        """(len(times), len(STATE)) float64 matrix, column order STATE."""
        t = np.asarray(times, dtype=np.int64)
        fi = np.maximum(0, np.searchsorted(self.ts, t - 1, side="right") - 1)
        team = self.team[pid]
        enemy = 300 - team
        zero = np.zeros(len(t), dtype=np.int64)
        count = lambda kind, tm: np.searchsorted(self.sorted_scores[(kind, tm)], t, side="left") if (kind, tm) in self.sorted_scores else zero
        cols = [t / 60000,
                self.team_gold.get(team, zero)[fi] - self.team_gold.get(enemy, zero)[fi] if team in (100, 200) else zero,
                self.team_xp.get(team, zero)[fi] - self.team_xp.get(enemy, zero)[fi] if team in (100, 200) else zero,
                self.arr["gold"][pid][fi] - self.arr["gold"][opp][fi],
                self.arr["level"][pid][fi] - self.arr["level"][opp][fi]]
        cols += [count(kind, team) - count(kind, enemy) for kind in COUNTS]
        return np.column_stack(cols).astype(np.float64)


def match_arrays(match_id, match, timeline, items):
    """Everything WPA and the comparison need from one match, as arrays. None if the match is unusable.

    items is the patch's catalog or None; without it the match still yields snapshots (used by the
    forward comparison) but no build decisions, and WPA skips it exactly as before.
    """
    info = match["info"]
    if not valid_match(info) or not timeline["info"]["frames"]:
        return None
    game = FastGame(match, timeline)
    players, snap_X, snap_p, dec_X, dec_p, dec_kind, dec_item, after_X = [], [], [], [], [], [], [], []
    for p in info["participants"]:
        opp = opponent_for(info["participants"], p)
        if opp is None:
            continue
        pid, oid, n = p["participantId"], opp["participantId"], len(players)
        blue = int(p["teamId"] == 100)
        players.append((p.get("playerRef", ""), int(bool(p["win"])), blue))
        times = np.arange(0, game.duration, SNAPSHOT_STEP_MS, dtype=np.int64)
        snap_X.append(np.column_stack([game.states(pid, oid, times), np.full(len(times), blue)]))
        snap_p.append(np.full(len(times), n, dtype=np.int16))
        if items is None:
            continue
        operations, uncertain = purchases(timeline, pid)
        if uncertain:
            continue
        buys = [o for o in operations if o["active"] and o["kind"] == "ITEM_PURCHASED"]
        if any(str(o["item"]) not in items for o in buys):
            continue
        build, boots = build_path(buys, items)
        moments = [(f"slot{k}", iid, int(minute * 60000)) for k, (iid, _, minute) in enumerate(build, 1)]
        if boots:
            moments.append(("boots", boots[0], int(boots[2] * 60000)))
        for kind, iid, t in moments:
            dec_kind.append(kind); dec_item.append(str(iid)); dec_p.append(n)
            dec_X.append(np.append(game.states(pid, oid, [t])[0], blue))
            later = t + 60000 * np.arange(1, HORIZON + 1, dtype=np.int64)
            after = game.states(pid, oid, later)
            after[later >= game.duration] = np.nan  # the game is over: no state
            after_X.append(after)
    if not players:
        return None
    width = len(FEATURES)
    return dict(
        snap_X=np.concatenate(snap_X).astype(np.float32), snap_p=np.concatenate(snap_p),
        dec_X=np.asarray(dec_X, dtype=np.float32).reshape(-1, width), dec_p=np.asarray(dec_p, dtype=np.int16),
        after_X=np.asarray(after_X, dtype=np.float32).reshape(-1, HORIZON, len(STATE)),
        dec_kind=np.asarray(dec_kind, dtype="<U8"), dec_item=np.asarray(dec_item, dtype="<U12"),
        player_ref=np.asarray([r for r, _, _ in players], dtype="<U80"),
        player_win=np.asarray([w for _, w, _ in players], dtype=np.int8),
        meta=np.asarray(json.dumps({"startedAt": info["gameStartTimestamp"], "hasCatalog": items is not None,
                                    "patch": ".".join(info["gameVersion"].split(".")[:2]),
                                    "region": match_id.split("_")[0], "duration": game.duration})))


def _pack(arrays):
    """JSON header + raw array buffers: no pickle, and unpacking is a zero-copy view (npz costs ~1 ms per match)."""
    arrays = dict(arrays, meta=np.frombuffer(str(arrays["meta"]).encode(), dtype=np.uint8))
    header = json.dumps([[k, a.dtype.str, a.shape] for k, a in arrays.items()]).encode()
    body = b"".join(np.ascontiguousarray(a).tobytes() for a in arrays.values())
    return zlib.compress(len(header).to_bytes(4, "little") + header + body, 1)


def _unpack(blob):
    raw = zlib.decompress(blob)
    offset = 4 + int.from_bytes(raw[:4], "little")
    out = {}
    for name, dtype, shape in json.loads(raw[4:offset]):
        dt = np.dtype(dtype)
        count = int(np.prod(shape, dtype=np.int64))
        out[name] = np.frombuffer(raw, dt, count, offset).reshape(shape)
        offset += count * dt.itemsize
    out["meta"] = json.loads(out["meta"].tobytes())
    return out


_worker_db, _worker_catalogs = None, None


def _init_worker(db_path, catalogs):
    global _worker_db, _worker_catalogs
    _worker_db = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    _worker_catalogs = catalogs  # sent once per worker, not once per batch


def _compute(ids):
    """Worker: parse each match once and derive both products, training arrays and export records."""
    out = []
    for mid in ids:
        row = _worker_db.execute("SELECT detail, timeline FROM matches WHERE id=?", (mid,)).fetchone()
        if row is None or row[0] is None or row[1] is None:
            continue  # deleted since the id list was read
        match, timeline = json.loads(row[0]), json.loads(row[1])
        patch = ".".join(match["info"]["gameVersion"].split(".")[:2])
        version, items = _worker_catalogs.get(patch, (None, None))
        arrays = match_arrays(mid, match, timeline, items)
        records = extract(match, timeline, items or {})
        out.append((mid, patch, version, None if arrays is None else _pack(arrays),
                    zlib.compress(json.dumps(records).encode(), 1) if records else None))
    return out


def cache_for(db_path):
    """Each database has its own cache: a test or copy can never prune another database's cache."""
    db_path = Path(db_path)
    return db_path.with_name(db_path.stem + ".features.sqlite")


def open_cache(path):
    cache = sqlite3.connect(path, timeout=60)
    cache.executescript("PRAGMA journal_mode=WAL; DROP TABLE IF EXISTS wpa; CREATE TABLE IF NOT EXISTS matches "
                        "(match_id TEXT PRIMARY KEY, code TEXT, patch TEXT, catalog TEXT, arrays BLOB, records BLOB)")
    return cache


def sync(db_path, cache_path=None, workers=None, log=print):
    """Bring the cache in line with the main database. Returns (open cache, set of live match ids)."""
    main = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    ids = [r[0] for r in main.execute(f"SELECT id FROM matches WHERE {USABLE_SQL} ORDER BY id")]
    catalogs = {r[0]: (r[1], json.loads(r[2])) for r in main.execute("SELECT patch, version, items FROM catalog")}
    main.close()
    cache = open_cache(cache_path or cache_for(db_path))
    live = set(ids)
    stale = [m for (m,) in cache.execute("SELECT match_id FROM matches") if m not in live]
    cache.executemany("DELETE FROM matches WHERE match_id=?", [(m,) for m in stale])
    cache.commit()
    valid = {mid for mid, code, patch, version in cache.execute("SELECT match_id, code, patch, catalog FROM matches")
             if code == CODE_VERSION and version == catalogs.get(patch, (None,))[0]}
    todo = [m for m in ids if m not in valid]
    log(f"Features: {len(ids) - len(todo):,} cached, {len(todo):,} to compute" + (f", {len(stale):,} removed" if stale else ""), flush=True)
    if todo:
        # Leave headroom for the running collectors and the desktop.
        workers = workers or max(1, min(len(todo) // 50 + 1, (os.cpu_count() or 4) - 2))
        batches = [todo[i:i + 40] for i in range(0, len(todo), 40)]
        done = 0
        with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(str(db_path), catalogs)) as pool:
            for result in pool.map(_compute, batches):
                cache.executemany("INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?)",
                                  [(mid, CODE_VERSION, *rest) for mid, *rest in result])
                cache.commit()
                done += len(result)
                if done % 2000 < 40:
                    log(f"  {done:,}/{len(todo):,} matches featurized", flush=True)
    return cache, live


def load_matches(db_path, cache_path=None, workers=None, log=print):
    """{match_id: arrays} for every usable finished match, computing only what the cache lacks."""
    cache, live = sync(db_path, cache_path, workers, log)
    try:
        return {mid: _unpack(blob) for mid, blob in cache.execute(
            "SELECT match_id, arrays FROM matches WHERE arrays IS NOT NULL ORDER BY match_id") if mid in live}
    finally:
        cache.close()


def load_records(db, db_path, cache_path=None, workers=None, log=print):
    """engine.extract() records of every finished match in id order, as engine.dataset() yields them.

    A patch without an item catalog is fetched once from Data Dragon (writing through db, the
    caller's main connection); the catalog change then recomputes exactly that patch's matches.
    JSON storage turns tuples into lists; every consumer only indexes or unpacks them.
    """
    from urllib.error import URLError
    from engine import _missing_catalogs, catalog
    cache, live = sync(db_path, cache_path, workers, log)
    try:
        have = {r[0] for r in db.execute("SELECT patch FROM catalog")}
        fetched = False
        for (patch,) in cache.execute("SELECT DISTINCT patch FROM matches ORDER BY patch").fetchall():
            if patch in have or patch in _missing_catalogs:
                continue
            try:
                catalog(db, patch)
                fetched = True
            except (ValueError, URLError):
                _missing_catalogs.add(patch)
                log(f"Patch {patch}: Data Dragon item list not published yet; exporting its games without item statistics.")
        if fetched:
            cache.close()
            cache, live = sync(db_path, cache_path, workers, log)
        for mid, blob in cache.execute("SELECT match_id, records FROM matches WHERE records IS NOT NULL ORDER BY match_id"):
            if mid in live:
                yield from json.loads(zlib.decompress(blob))
    finally:
        cache.close()


def fold_of(match_id):
    return zlib.crc32(match_id.encode()) % FOLDS


def wpa_tables(matches):
    """Concatenated training snapshots and build decisions for matches with an item catalog."""
    snap_X, snap_y, snap_f, dec_X, dec_y, dec_f, after_X, meta = [], [], [], [], [], [], [], []
    for mid, a in matches.items():
        if not a["meta"]["hasCatalog"]:
            continue
        f = fold_of(mid)
        snap_X.append(a["snap_X"]); snap_y.append(a["player_win"][a["snap_p"]]); snap_f.append(np.full(len(a["snap_X"]), f, np.int8))
        dec_X.append(a["dec_X"]); dec_y.append(a["player_win"][a["dec_p"]]); dec_f.append(np.full(len(a["dec_X"]), f, np.int8))
        after_X.append(a["after_X"])
        meta += [(mid, a["player_ref"][p], k, i) for p, k, i in zip(a["dec_p"], a["dec_kind"], a["dec_item"])]
    cat = lambda xs, shape: np.concatenate(xs) if xs else np.zeros(shape)
    return dict(games=sum(1 for a in matches.values() if a["meta"]["hasCatalog"]),
                snap_X=cat(snap_X, (0, len(FEATURES))), snap_y=cat(snap_y, 0), snap_f=cat(snap_f, 0),
                dec_X=cat(dec_X, (0, len(FEATURES))), dec_y=cat(dec_y, 0), dec_f=cat(dec_f, 0),
                after_X=cat(after_X, (0, HORIZON, len(STATE))), dec_meta=meta)
