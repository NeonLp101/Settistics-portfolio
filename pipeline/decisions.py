"""Generic purchase decisions with fixed-window outcomes, for every champion and role.

    python pipeline/decisions.py                       # general-source development games -> data/decisions.sqlite
    python pipeline/decisions.py --include-focus       # also focus-discovered games (training/sensitivity only)
    python pipeline/decisions.py --limit 500           # quick sample: the earliest 500 selected games
    python pipeline/decisions.py --win-model m.json    # also score win-chance change with a frozen model

One row per observable purchase during the item slot 1-3 stages and the boots stage. A decision is dated
at the purchase itself, not at the later completion of an item: buying a component is its own action,
recorded as that component, whether or not the player ever completed anything with it. Shared components
stay ambiguous; no target item is inferred from what the player built later.

Columns come in four groups (KEYS, CONTEXT, ACTION, OUTCOME). Only CONTEXT and ACTION may enter a model's
decision features: they use information from before the purchase (the last frame strictly before it,
events strictly before it, purchase history, reconstructed inventory). OUTCOME holds the final result,
the game length, a whole-game quality check and the fixed windows (t, t+5 min] and (t, t+10 min].

Terminal handling: the game ends at its GAME_END event (Riot's gameDuration is truncated to whole seconds
and ends up to a second earlier). Windows are never discarded: a window cut short by the game end keeps
its partial counts, records its exposure and has complete_w = 0; windows without a fight report zeros.
The win chance at the end of a window that reaches the game end is the final result.

- Kills, deaths, takedowns and time alive count exactly over (t, min(t+w, end)].
- Time alive subtracts estimated death timers: base respawn wait by level at death, plus the time factor
  after 15 minutes (respawn_ms); a death still running at the game end counts as dead until the end.
  The timers matched totalTimeSpentDead of single-death players exactly; revive and passive effects
  (Sion, Guardian Angel...) are not modelled, so every row carries the player's whole-game error
  dead_time_error_s against Riot's total (deaths that respawned before the end) as a quality flag: an
  outcome column, never a feature.
- Champion damage dealt and damage taken come from cumulative per-minute frame counters, so their window
  is frame-aligned: from the first frame at or after the purchase (damage_lag_ms later, under a minute)
  to the last frame no later than t+w, or the final frame if the game ended within the window. Damage
  before the purchase and after the requested endpoint is never counted; damage_exposure_ms_w is the
  span actually covered. Damage taken is from all sources: frames do not split out champions.

Selection: engine.USABLE_SQL (site and public export) is unchanged. The withdrawn Kai'Sa study's sealed
period is released for development here and marked sealed_period. General-collection games are the
default; include_focus adds focus-discovered games, marked focus_source. Every selected game is assigned
in a forward chronological, match-grouped split manifest (split_manifest): development folds and a later
held-out group, validated and evaluated on general-source games only. The main database is opened
read-only; the tables go to a separate private file holding pseudonymous player refs.
"""
import argparse
import json
import math
import os
import sqlite3
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from engine import ROOT, SEALED_FROM_MS, item_class, opponent_for, purchases, valid_match
from features import FastGame
from wpa import FEATURES

WINDOWS = (5, 10)  # minutes
SLOTS = 3
START_MS = 60000  # purchases up to here are the starting package, a separate decision
# Base respawn wait in seconds by champion level 1-18.
RESPAWN_S = (10, 10, 12, 12, 14, 16, 20, 25, 28, 32.5, 35, 37.5, 40, 42.5, 45, 47.5, 50, 52.5)

# After the Kai'Sa study's withdrawal (2026-09-25) its sealed period is ordinary development data here.
DEVELOPMENT_SQL = "status='done' AND timeline IS NOT NULL"
FOCUS_SQL = "source LIKE 'focus %'"

KEYS = ["match_id", "source", "focus_source", "sealed_period", "player_ref", "participant_id", "team_id"]
CONTEXT = ["region", "patch", "started_at", "champion", "role", "opponent", "stage", "stage_step", "t_ms",
           "completed_before", "inventory_before", "boots_before", "snapshot_age_ms", "current_gold_snapshot",
           "budget_exact", "dead_at_decision", "state_pre", "win_chance_pre"]
ACTION = ["action_item", "action_kind", "action_cost", "builds_into"]
OUTCOME = ["win", "duration_ms", "dead_time_error_s", "damage_lag_ms"]
for _w in WINDOWS:
    OUTCOME += [f"exposure_ms_{_w}", f"complete_{_w}", f"team_gold_lead_change_{_w}", f"lane_gold_lead_change_{_w}",
                f"kills_{_w}", f"deaths_{_w}", f"takedowns_{_w}", f"time_alive_ms_{_w}",
                f"champion_damage_dealt_{_w}", f"damage_taken_{_w}", f"damage_exposure_ms_{_w}",
                f"state_{_w}", f"win_chance_{_w}", f"win_chance_change_{_w}"]
COLUMNS = KEYS + CONTEXT + ACTION + OUTCOME


def consume(inventory, iid, items):
    """Remove the components a purchase of iid uses up, as the shop does: held parts first, else their parts."""
    for part in items.get(iid, {}).get("from", []):
        if inventory[part] > 0:
            inventory[part] -= 1
        else:
            consume(inventory, part, items)


def classify(iid, items, completed, boots_done):
    """(stage, action_kind) of one purchase after the starting package, or None if it is no slot/boots action."""
    info = items[iid]
    tags = set(info.get("tags", []))
    if info.get("maps", {}).get("11") is False or tags & {"Consumable", "Trinket"} or not info.get("gold", {}).get("total"):
        return None
    if "Boots" in tags:
        if boots_done:
            return None  # tier-3 upgrades and rebuys are later decisions
        if not info.get("from"):
            return "boots", "boots_basic"
        return ("boots", "boots_upgrade") if item_class(info) == "boots" else None
    if len(completed) >= SLOTS:
        return None
    kind = item_class(info)
    if kind == "legendary":
        return (f"slot{len(completed) + 1}", "completion") if iid not in completed else None
    return f"slot{len(completed) + 1}", "component" if info.get("into") else "other"


def player_actions(operations, items):
    """Observable slot/boots actions of one player, each with the inventory state just before it.

    operations is the active net ledger from engine.purchases. Completed-slot counting follows
    engine.build_path: distinct finished items in completion order, sales do not reopen a slot.
    """
    inventory, completed, boots_done, steps, out = Counter(), [], False, Counter(), []
    for o in sorted(operations, key=lambda o: o["time"]):
        iid = str(o["item"])
        if o["kind"] == "ITEM_SOLD":
            if inventory[iid] > 0:
                inventory[iid] -= 1
            continue
        action = classify(iid, items, completed, boots_done) if o["time"] > START_MS else None
        if action:
            stage, kind = action
            steps[stage] += 1
            held_boots = [i for i in inventory.elements() if "Boots" in items.get(i, {}).get("tags", [])]
            out.append(dict(stage=stage, stage_step=steps[stage], action_item=iid, action_kind=kind, t_ms=o["time"],
                            action_cost=items[iid].get("gold", {}).get("total"),
                            builds_into=len(items[iid].get("into", [])) if kind != "completion" else None,
                            completed_before=list(completed),
                            inventory_before=sorted(i for i in inventory.elements()),
                            boots_before=max(held_boots, key=lambda i: items[i].get("gold", {}).get("total", 0)) if held_boots else "none"))
            if kind == "completion":
                completed.append(iid)
            elif kind == "boots_upgrade":
                boots_done = True
        if not set(items[iid].get("tags", [])) & {"Consumable", "Trinket"}:  # used up or swapped: not held
            consume(inventory, iid, items)
            inventory[iid] += 1
    return out


def kill_counts(kills, pid, start, end):
    """Kills, deaths and takedowns of pid in (start, end]."""
    k = d = a = 0
    for e in kills:
        if start < e["timestamp"] <= end:
            k += e.get("killerId") == pid
            d += e.get("victimId") == pid
            a += pid in (e.get("assistingParticipantIds") or [])
    return k, d, k + a


def respawn_ms(level, t_ms):
    """Estimated death timer for a death at t_ms at this level: base wait times the time factor after 15 min."""
    m = t_ms / 60000
    if m <= 15:
        factor = 0.0
    elif m <= 30:
        factor = math.ceil(2 * (m - 15)) * 0.00425
    elif m <= 45:
        factor = 0.1275 + math.ceil(2 * (m - 30)) * 0.003
    else:
        factor = min(0.5, 0.2175 + math.ceil(2 * (m - 45)) * 0.0145)
    return RESPAWN_S[min(max(int(level), 1), len(RESPAWN_S)) - 1] * (1 + factor) * 1000


def dead_intervals(events, pid):
    """[(death, respawn)] of pid, respawn estimated from the level reached before the death."""
    levels = sorted((e["timestamp"], e.get("level", 1)) for e in events if e.get("type") == "LEVEL_UP" and e.get("participantId") == pid)
    out = []
    for e in events:
        if e.get("type") == "CHAMPION_KILL" and e.get("victimId") == pid:
            d = e["timestamp"]
            level = max([lv for ts, lv in levels if ts <= d], default=1)
            out.append((d, d + respawn_ms(level, d)))
    return out


def time_dead(intervals, start, stop):
    """Milliseconds of (start, stop] spent dead."""
    return sum(max(0.0, min(r, stop) - max(d, start)) for d, r in intervals)


def game_end(match, frames):
    """GAME_END time; else the later of the final frame and gameDuration (which is truncated to seconds)."""
    ends = [e["timestamp"] for f in frames for e in f.get("events", []) if e.get("type") == "GAME_END"]
    return ends[-1] if ends else max(frames[-1].get("timestamp", 0), match["info"]["gameDuration"] * 1000)


def damage_windows(frames, frame_ts, pid, t, end):
    """damage_lag_ms and, per window, (champion damage dealt, damage taken, covered ms); None if frames lack it."""
    stats = [f["participantFrames"].get(str(pid), {}).get("damageStats") for f in frames]
    a = int(np.searchsorted(frame_ts, t, side="left"))  # first frame at or after the purchase
    if a >= len(frames) or stats[a] is None:
        return None, {w: (None, None, None) for w in WINDOWS}
    out = {}
    for w in WINDOWS:
        target = t + w * 60000
        b = len(frames) - 1 if target >= end else int(np.searchsorted(frame_ts, target, side="right")) - 1
        if b < a or stats[b] is None or any(k not in stats[a] or k not in stats[b]
                                              for k in ("totalDamageDoneToChampions", "totalDamageTaken")):
            out[w] = (None, None, None)
        else:
            out[w] = (stats[b]["totalDamageDoneToChampions"] - stats[a]["totalDamageDoneToChampions"],
                      stats[b]["totalDamageTaken"] - stats[a]["totalDamageTaken"],
                      int(frame_ts[b] - frame_ts[a]))
    return int(frame_ts[a] - t), out


def match_meta(match_id, source, match, frames):
    """Provenance and timing of one valid match, as the split manifest needs it."""
    info = match["info"]
    start = info["gameStartTimestamp"]
    end = game_end(match, frames)
    return dict(match_id=match_id, source=source, focus_source=int(str(source or "").startswith("focus ")),
                region=info.get("platformId", match_id.split("_")[0]), patch=".".join(info["gameVersion"].split(".")[:2]),
                started_at=start, ended_at=info.get("gameEndTimestamp") or start + end)


def match_decisions(match_id, source, match, timeline, items, win_chance=None):
    """(rows, skipped Counter) for one match. win_chance maps an (n, len(FEATURES)) array to probabilities."""
    info = match["info"]
    skipped = Counter()
    if not valid_match(info) or not timeline["info"]["frames"]:
        skipped["invalid match"] += 1
        return [], skipped
    if items is None:
        skipped["no item catalog"] += 1
        return [], skipped
    game = FastGame(match, timeline)
    frames = sorted(timeline["info"]["frames"], key=lambda f: f.get("timestamp", 0))
    frame_ts = np.asarray([f.get("timestamp", 0) for f in frames])
    end = game_end(match, frames)
    events = sorted((e for f in frames for e in f.get("events", [])), key=lambda e: e.get("timestamp", 0))
    kills = [e for e in events if e.get("type") == "CHAMPION_KILL"]
    meta = match_meta(match_id, source, match, frames)
    base = dict(match_id=match_id, source=source, focus_source=meta["focus_source"],
                sealed_period=int(info["gameStartTimestamp"] >= SEALED_FROM_MS), region=meta["region"],
                patch=meta["patch"], started_at=info["gameStartTimestamp"], duration_ms=end)
    rows = []
    for p in info["participants"]:
        opp = opponent_for(info["participants"], p)
        if opp is None:
            skipped["no lane opponent"] += 1
            continue
        operations, uncertain = purchases(timeline, p["participantId"])
        active = [o for o in operations if o["active"]]
        if uncertain:
            skipped["uncertain ledger"] += 1
            continue
        if any(str(o["item"]) not in items for o in active):
            skipped["item missing from catalog"] += 1
            continue
        pid, oid = p["participantId"], opp["participantId"]
        blue = float(p["teamId"] == 100)
        dead = dead_intervals(events, pid)
        # Riot adds a death to totalTimeSpentDead at the respawn: a death still running at the end is not in it.
        dead_error = (sum(r - d for d, r in dead if r <= end) / 1000 - p["totalTimeSpentDead"]) if "totalTimeSpentDead" in p else None
        for a in player_actions(active, items):
            t = a["t_ms"]
            fi = int(np.searchsorted(frame_ts, t - 1, side="right")) - 1  # last frame strictly before t
            own = frames[fi]["participantFrames"].get(str(pid), {}) if fi >= 0 else {}
            times = [t] + [t + w * 60000 for w in WINDOWS]
            states = game.states(pid, oid, times)  # past the end: the last frame, i.e. the final state
            lag, damage = damage_windows(frames, frame_ts, pid, t, end)
            row = dict(base, player_ref=p.get("playerRef", ""), participant_id=pid, team_id=p["teamId"],
                       champion=p["championName"], role=p["teamPosition"], opponent=opp["championName"],
                       win=int(bool(p["win"])), **a,
                       snapshot_age_ms=t - frames[fi]["timestamp"] if fi >= 0 else None,
                       current_gold_snapshot=own.get("currentGold"), budget_exact=0,
                       # deaths strictly before t; a respawn timer is known at the death, so no later event is used
                       dead_at_decision=int(any(d < t < r for d, r in dead)),
                       state_pre=[float(v) for v in states[0]] + [blue],
                       dead_time_error_s=dead_error, damage_lag_ms=lag)
            for k, w in enumerate(WINDOWS, 1):
                stop = min(t + w * 60000, end)
                gold = states[k] - states[0]
                dealt, taken, covered = damage[w]
                row.update({f"exposure_ms_{w}": max(0, stop - t), f"complete_{w}": int(t + w * 60000 <= end),
                            f"team_gold_lead_change_{w}": float(gold[FEATURES.index("team_gold")]),
                            f"lane_gold_lead_change_{w}": float(gold[FEATURES.index("lane_gold")]),
                            f"time_alive_ms_{w}": max(0, stop - t) - time_dead(dead, t, stop),
                            f"champion_damage_dealt_{w}": dealt, f"damage_taken_{w}": taken,
                            f"damage_exposure_ms_{w}": covered,
                            f"state_{w}": [float(v) for v in states[k]] + [blue] if t + w * 60000 < end else None})
                row[f"kills_{w}"], row[f"deaths_{w}"], row[f"takedowns_{w}"] = kill_counts(kills, pid, t, stop)
            rows.append(row)
    if win_chance is not None:
        score_win_chance(rows, win_chance)
    return rows, skipped


def score_win_chance(rows, win_chance):
    """Fill win-chance columns from a frozen model; a window that reaches the game end takes the result."""
    if not rows:
        return rows
    pre = win_chance(np.asarray([r["state_pre"] for r in rows], dtype=float))
    for r, p in zip(rows, pre):
        r["win_chance_pre"] = float(p)
    for w in WINDOWS:
        live = [r for r in rows if r[f"state_{w}"] is not None]
        after = win_chance(np.asarray([r[f"state_{w}"] for r in live], dtype=float)) if live else []
        for r, p in zip(live, after):
            r[f"win_chance_{w}"] = float(p)
        for r in rows:
            if r[f"state_{w}"] is None:
                r[f"win_chance_{w}"] = float(r["win"])
            r[f"win_chance_change_{w}"] = r[f"win_chance_{w}"] - r["win_chance_pre"]
    return rows


def load_win_model(path):
    """A frozen XGBoost classifier saved with save_model; it must take FEATURES in order.

    For development scores it must not have been fitted on any game of the fold it scores."""
    from xgboost import XGBClassifier
    model = XGBClassifier()
    model.load_model(path)
    return lambda X: model.predict_proba(X)[:, 1]


def split_manifest(matches, heldout_fraction=0.2, folds=4):
    """Forward chronological, match-grouped splits. matches: dicts with match_id, started_at, ended_at, focus_source.

    The latest heldout_fraction of general-source games (by start time) opens the held-out period; everything
    before it is development, cut into folds + 1 blocks of equal general-source count. Fold k validates on
    block k's general games and trains on earlier blocks' games that had ended before block k began. The
    held-out group trains on development games that ended before it began. Focus games only ever appear as
    'train_focus' (a sensitivity addition); focus games from the held-out period are left unused. Every
    boundary comes from general-source games, so adding focus games never moves a split.

    Returns (assignments {match_id: (period, block)}, memberships [(fold, role, match_id)], meta dict).
    """
    ordered = sorted(matches, key=lambda m: (m["started_at"], m["match_id"]))
    general = [m for m in ordered if not m["focus_source"]]
    if len(general) < folds + 2:
        raise ValueError(f"{len(general)} general-source games cannot fill {folds} folds and a held-out group")
    heldout_start = general[len(general) - max(1, math.ceil(heldout_fraction * len(general)))]["started_at"]
    dev = [m for m in general if m["started_at"] < heldout_start]
    if len(dev) < folds + 1:
        raise ValueError(f"{len(dev)} development games cannot fill {folds} folds")
    bounds = [dev[len(dev) * k // (folds + 1)]["started_at"] for k in range(1, folds + 1)]
    assignments = {}
    for m in ordered:
        if m["started_at"] >= heldout_start:
            assignments[m["match_id"]] = ("late_focus_unused" if m["focus_source"] else "heldout", None)
        else:
            assignments[m["match_id"]] = ("development", sum(m["started_at"] >= b for b in bounds))
    memberships = []
    for fold, start, stop in [(str(k), bounds[k - 1], bounds[k] if k < folds else heldout_start) for k in range(1, folds + 1)] \
            + [("heldout", heldout_start, None)]:
        for m in ordered:
            period, block = assignments[m["match_id"]]
            if period == "late_focus_unused":
                continue
            if m["started_at"] >= start and (stop is None or m["started_at"] < stop) and not m["focus_source"]:
                memberships.append((fold, "evaluate" if fold == "heldout" else "validate", m["match_id"]))
            elif m["started_at"] < start and m["ended_at"] < start:
                memberships.append((fold, "train_focus" if m["focus_source"] else "train", m["match_id"]))
    roles = Counter((f, r) for f, r, _ in memberships)
    meta = dict(heldout_fraction=heldout_fraction, folds=folds, heldout_start=heldout_start, block_starts=bounds,
                periods=dict(Counter(p for p, _ in assignments.values())),
                roles={f"{f}:{r}": n for (f, r), n in sorted(roles.items())})
    return assignments, memberships, meta


def open_output(path):
    out = sqlite3.connect(path, timeout=60)
    for table in ("purchase_decisions", "split_matches", "split_folds", "split_meta"):
        out.execute(f"DROP TABLE IF EXISTS {table}")
    out.execute(f"CREATE TABLE purchase_decisions ({', '.join(COLUMNS)})")
    out.execute("CREATE TABLE split_matches (match_id TEXT PRIMARY KEY, started_at INTEGER, ended_at INTEGER, region TEXT, "
                "patch TEXT, source TEXT, focus_source INTEGER, period TEXT, block INTEGER)")
    out.execute("CREATE TABLE split_folds (fold TEXT, role TEXT, match_id TEXT, PRIMARY KEY (fold, match_id))")
    out.execute("CREATE TABLE split_meta (key TEXT PRIMARY KEY, value TEXT)")
    return out


def to_db(row):
    return [json.dumps(v) if isinstance(v, list) else v for v in (row.get(c) for c in COLUMNS)]


def selection_sql(include_focus):
    return DEVELOPMENT_SQL if include_focus else f"{DEVELOPMENT_SQL} AND (source IS NULL OR NOT {FOCUS_SQL})"


_worker = {}


def _init(db_path, catalogs, win_model):
    _worker.update(db=sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60),
                   catalogs=catalogs, win=load_win_model(win_model) if win_model else None)


def _batch(ids):
    rows, metas, skipped = [], [], Counter()
    for mid in ids:
        found = _worker["db"].execute("SELECT source, detail, timeline FROM matches WHERE id=?", (mid,)).fetchone()
        if found is None:
            continue
        source, detail, timeline = found
        match, timeline = json.loads(detail), json.loads(timeline)
        patch = ".".join(match["info"]["gameVersion"].split(".")[:2])
        r, s = match_decisions(mid, source, match, timeline, _worker["catalogs"].get(patch), _worker["win"])
        if not s["invalid match"]:
            metas.append(match_meta(mid, source, match, sorted(timeline["info"]["frames"], key=lambda f: f.get("timestamp", 0))))
        rows += [to_db(x) for x in r]
        skipped += s
    return rows, metas, skipped


def build(db_path, out_path, limit=None, workers=None, win_model=None, include_focus=False,
          heldout_fraction=0.2, folds=4, log=print):
    main = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    sql = (f"SELECT id FROM matches WHERE {selection_sql(include_focus)} "
           f"ORDER BY json_extract(detail,'$.info.gameStartTimestamp'), id" + (f" LIMIT {int(limit)}" if limit else ""))
    ids = [r[0] for r in main.execute(sql)]
    catalogs = {r[0]: json.loads(r[1]) for r in main.execute("SELECT patch, items FROM catalog")}
    main.close()
    out = open_output(out_path)
    workers = workers or max(1, min(len(ids) // 50 + 1, (os.cpu_count() or 4) - 2))
    total, metas, skipped = 0, [], Counter()
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(str(db_path), catalogs, win_model)) as pool:
        for rows, m, s in pool.map(_batch, [ids[i:i + 40] for i in range(0, len(ids), 40)]):
            out.executemany(f"INSERT INTO purchase_decisions VALUES ({','.join('?' * len(COLUMNS))})", rows)
            out.commit()
            total += len(rows)
            metas += m
            skipped += s
    assignments, memberships, meta = split_manifest(metas, heldout_fraction, folds)
    meta.update(include_focus=include_focus, limit=limit, games=len(metas))
    out.executemany("INSERT INTO split_matches VALUES (?,?,?,?,?,?,?,?,?)",
                    [(m["match_id"], m["started_at"], m["ended_at"], m["region"], m["patch"], m["source"], m["focus_source"],
                      *assignments[m["match_id"]]) for m in metas])
    out.executemany("INSERT INTO split_folds VALUES (?,?,?)", memberships)
    out.executemany("INSERT INTO split_meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in meta.items()])
    out.execute("CREATE INDEX IF NOT EXISTS decisions_by_match ON purchase_decisions(match_id)")
    out.commit()
    out.close()
    log(f"{total:,} decisions from {len(ids):,} games -> {out_path}. Skipped: "
        + (", ".join(f"{k} {v:,}" for k, v in sorted(skipped.items())) or "none"))
    log(f"Splits: {meta['roles']}; periods {meta['periods']}")
    return total, skipped, meta


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(ROOT / "data" / "settistics.sqlite"))
    parser.add_argument("--out", default=str(ROOT / "data" / "decisions.sqlite"))
    parser.add_argument("--limit", type=int, help="only the earliest N selected games")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--win-model", help="frozen XGBoost model (save_model JSON) over wpa.FEATURES")
    parser.add_argument("--include-focus", action="store_true",
                        help="add focus-discovered games; they only ever train (sensitivity), never validate or evaluate")
    parser.add_argument("--heldout-fraction", type=float, default=0.2, help="latest share of general-source games held out")
    parser.add_argument("--folds", type=int, default=4, help="forward development folds before the held-out group")
    args = parser.parse_args()
    if Path(args.out).resolve() == Path(args.db).resolve():
        parser.error("--out must not be the main database")
    build(args.db, args.out, args.limit, args.workers, args.win_model, args.include_focus, args.heldout_fraction, args.folds)


if __name__ == "__main__":
    main()
