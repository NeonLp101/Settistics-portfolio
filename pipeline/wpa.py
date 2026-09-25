"""Win Probability Added (prototype) for build decisions: items 1-5 and boots.

    python pipeline/wpa.py                      # train, score every build decision, store predictions
    python pipeline/engine.py export --champion all
    npm run build

For every player-game the model sees the game state just before a decision (gold, XP and level gaps,
objectives, side) and predicts the chance of winning. WPA of a choice is the average of
(won - predicted) over everyone who made it. This is an adjusted association, not the causal
effect of purchasing the item: the choice can still reflect unobserved player and game context.

Every prediction is cross-fitted: the model that scores a game was trained on other games only, and
probabilities are recalibrated the same way. Requires requirements-model.txt (numpy, scikit-learn).
"""
import argparse
import bisect
import time

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from engine import ROOT, connect, utc, write_json
from backends import BACKENDS, outcome_model, resolve, verify_device

FOLDS = 5
SNAPSHOT_STEP_MS = 120_000
STATE = ["minute", "team_gold", "team_xp", "lane_gold", "lane_level",
         "kills", "towers", "inhibs", "dragons", "barons", "heralds", "grubs"]
DRAFT = ["blue"]  # outcome-derived draft strength would leak scored-fold labels into training features
FEATURES = STATE + DRAFT
HORIZON = 10  # minutes after each purchase for which the win chance is tracked
MONSTERS = {"DRAGON": "dragons", "BARON_NASHOR": "barons", "RIFTHERALD": "heralds", "HORDE": "grubs"}


class Game:
    """Game state from one player's side at any moment, using only information from before that moment."""

    def __init__(self, match, timeline):
        info = match["info"]
        self.players = {p["participantId"]: p for p in info["participants"]}
        self.team = {pid: p["teamId"] for pid, p in self.players.items()}
        frames = timeline["info"]["frames"]
        self.frame_ts = [f["timestamp"] for f in frames]
        pf = lambda f, pid, key: f["participantFrames"].get(str(pid), {}).get(key, 0)
        self.gold = {pid: [pf(f, pid, "totalGold") for f in frames] for pid in self.players}
        self.xp = {pid: [pf(f, pid, "xp") for f in frames] for pid in self.players}
        self.level = {pid: [pf(f, pid, "level") for f in frames] for pid in self.players}
        self.duration = info["gameDuration"] * 1000
        self.scores = {}
        for f in frames:
            for e in f.get("events", []):
                self._event(e)
        for times in self.scores.values():
            times.sort()

    def _score(self, kind, team, ts):
        self.scores.setdefault((kind, team), []).append(ts)

    def _event(self, e):
        kind, ts = e.get("type"), e.get("timestamp", 0)
        if kind == "CHAMPION_KILL" and e.get("victimId") in self.team:
            self._score("kills", 300 - self.team[e["victimId"]], ts)
        elif kind == "BUILDING_KILL" and e.get("teamId") in (100, 200):  # teamId lost the building
            building = {"TOWER_BUILDING": "towers", "INHIBITOR_BUILDING": "inhibs"}.get(e.get("buildingType"))
            if building:
                self._score(building, 300 - e["teamId"], ts)
        elif kind == "ELITE_MONSTER_KILL" and e.get("monsterType") in MONSTERS and e.get("killerTeamId") in (100, 200):
            self._score(MONSTERS[e["monsterType"]], e["killerTeamId"], ts)

    def state(self, pid, opp, t):
        fi = max(0, bisect.bisect_right(self.frame_ts, t - 1) - 1)  # last frame strictly before t
        team, enemy = self.team[pid], 300 - self.team[pid]
        side = lambda arr, tm: sum(arr[q][fi] for q in self.players if self.team[q] == tm)
        count = lambda kind, tm: bisect.bisect_left(self.scores.get((kind, tm), []), t)
        s = {"minute": t / 60000,
             "team_gold": side(self.gold, team) - side(self.gold, enemy),
             "team_xp": side(self.xp, team) - side(self.xp, enemy),
             "lane_gold": self.gold[pid][fi] - self.gold[opp][fi],
             "lane_level": self.level[pid][fi] - self.level[opp][fi]}
        for kind in ("kills", "towers", "inhibs", "dragons", "barons", "heralds", "grubs"):
            s[kind] = count(kind, team) - count(kind, enemy)
        return s


def matrix(rows):
    return np.array([[r[f] for f in FEATURES] for r in rows], dtype=float)


def cross_fit(train, targets, backend="cpu"):
    """Row-dict wrapper around cross_fit_arrays. A target is a list of row dicts or an (X, folds) pair."""
    tX = [t[0] if isinstance(t, tuple) else matrix(t) for t in targets]
    tfold = [t[1] if isinstance(t, tuple) else np.array([r["fold"] for r in t]) for t in targets]
    return cross_fit_arrays(matrix(train), np.array([r["win"] for r in train]), np.array([r["fold"] for r in train]),
                            list(zip(tX, tfold)), backend)


def cross_fit_arrays(X, y, folds, targets, backend="cpu"):
    """Three-way cross-fitting: each scored fold is absent from both model and calibrator training.

    targets is a list of (X, folds) pairs. For outer fold k, fold (k+1) is reserved for Platt
    calibration and the other three fit the base model.
    """
    calibrated = [np.zeros(len(x)) for x, _ in targets]
    z = lambda p: np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4))).reshape(-1, 1)
    for k in range(FOLDS):
        calibration_fold = (k + 1) % FOLDS
        fit = (folds != k) & (folds != calibration_fold)
        calibrate = folds == calibration_fold
        model = outcome_model(backend, k)
        model.fit(X[fit], y[fit])
        verify_device(model, backend)
        # Calibration labels never appear in the base model's training set; scored labels
        # never appear in either stage. This avoids the indirect leakage from fitting a
        # calibrator on out-of-fold predictions made by models that saw the scored fold.
        lr = LogisticRegression().fit(z(model.predict_proba(X[calibrate])[:, 1]), y[calibrate])
        for out, (x, f) in zip(calibrated, targets):
            if (f == k).any():
                out[f == k] = lr.predict_proba(z(model.predict_proba(x[f == k])[:, 1]))[:, 1]
    return calibrated


def calibration(y, p, bins=10):
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    which = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, bins - 1)
    table = [{"predicted": round(float(p[which == b].mean()), 4), "actual": round(float(y[which == b].mean()), 4),
              "n": int((which == b).sum())} for b in range(bins) if (which == b).any()]
    ece = sum(abs(r["predicted"] - r["actual"]) * r["n"] for r in table) / len(y)
    return {"auc": round(float(roc_auc_score(y, p)), 4), "logLoss": round(float(log_loss(y, p)), 4),
            "brier": round(float(brier_score_loss(y, p)), 4), "calibrationErrorPp": round(ece * 100, 2), "bins": table}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(ROOT / "data" / "settistics.sqlite"))
    parser.add_argument("--backend", choices=BACKENDS, default="auto",
                        help="auto: the GPU when CUDA trains, else XGBoost on CPU")
    parser.add_argument("--workers", type=int, help="feature processes (default: all cores but two)")
    args = parser.parse_args()
    from features import load_matches, wpa_tables  # features builds on Game from this module
    backend = resolve(args.backend)
    started = time.perf_counter()
    print("Reading games...", flush=True)
    t = wpa_tables(load_matches(args.db, workers=args.workers))
    games = t["games"]
    if games < 200:
        raise SystemExit(f"Only {games} complete games; collect at least 200 before training.")
    featurized = time.perf_counter()
    # Outcome-derived champion win rates must not enter the model unless rebuilt
    # separately for each outer training/calibration split. Side is safe to use.
    decisions = t["dec_meta"]
    print(f"Training on {len(t['snap_X']):,} snapshots from {games:,} games with {backend}; "
          f"scoring {len(decisions):,} build decisions...", flush=True)
    # States after each purchase: side comes from the purchase row (same player, same game).
    alive = ~np.isnan(t["after_X"][:, :, 0])
    after_at = np.argwhere(alive)
    blue = t["dec_X"][:, FEATURES.index("blue")]
    after_X = np.column_stack([t["after_X"][alive], blue[after_at[:, 0]]])
    snap_p, dec_p, after_p = cross_fit_arrays(t["snap_X"], t["snap_y"], t["snap_f"],
        [(t["snap_X"], t["snap_f"]), (t["dec_X"], t["dec_f"]), (after_X, t["dec_f"][after_at[:, 0]])], backend)
    fitted = time.perf_counter()
    # Curve per purchase: win chance just before it, then each minute after; after the game ends, the result itself.
    curves = np.column_stack([dec_p] + [t["dec_y"].astype(float)] * HORIZON)
    curves[after_at[:, 0], after_at[:, 1] + 1] = after_p

    # Purchase moments are not typical game moments: Riot records state once a minute, purchases follow
    # gold income, and only long games reach late slots. Each slot's average offset is removed, so WPA
    # compares an item with the average choice for the same slot.
    kinds = np.array([kind for _, _, kind, _ in decisions])
    won = t["dec_y"]
    offsets = {}
    for kind in sorted(set(kinds)):
        mask = kinds == kind
        offsets[kind] = float((won[mask] - dec_p[mask]).mean())
        dec_p[mask] = np.clip(dec_p[mask] + offsets[kind], 1e-4, 1 - 1e-4)

    db = connect(args.db)
    db.execute("DROP TABLE IF EXISTS wpa_predictions")
    db.execute("CREATE TABLE wpa_predictions (match_id TEXT, player_ref TEXT, kind TEXT, item TEXT, p REAL, "
               "PRIMARY KEY(match_id, player_ref, kind, item))")
    db.executemany("INSERT OR REPLACE INTO wpa_predictions VALUES (?,?,?,?,?)",
                   [(m, player, kind, item, float(p)) for (m, player, kind, item), p in zip(decisions, dec_p)])
    db.execute("DROP TABLE IF EXISTS wpa_curves")
    db.execute("CREATE TABLE wpa_curves (match_id TEXT, player_ref TEXT, kind TEXT, item TEXT, curve TEXT, "
               "PRIMARY KEY(match_id, player_ref, kind, item))")
    db.executemany("INSERT OR REPLACE INTO wpa_curves VALUES (?,?,?,?,?)",
                   [(m, player, kind, item, ",".join(f"{v:.4f}" for v in c)) for (m, player, kind, item), c in zip(decisions, curves)])
    db.commit()

    y = t["snap_y"]
    report = {"generatedAt": utc(), "method": f"three-way cross-fitted {backend} gradient boosting + Platt recalibration",
              "interpretation": "adjusted association, not a causal item effect",
              "games": games, "snapshots": len(y), "decisions": len(decisions), "features": FEATURES,
              "inGame": calibration(y, snap_p),
              "slotOffsetsPp": {k: round(v * 100, 2) for k, v in offsets.items()}, "horizonMinutes": HORIZON}
    write_json(ROOT / "data" / "public" / "wpa_model.json", report)
    m = report["inGame"]
    print(f"Done. AUC {m['auc']:.3f}, calibration error {m['calibrationErrorPp']:.2f} pp. "
          f"Stored {len(decisions):,} predictions; run export and build to publish WPA. "
          f"Time: features {featurized - started:.0f} s, {backend} cross-fit {fitted - featurized:.0f} s, "
          f"total {time.perf_counter() - started:.0f} s.")


if __name__ == "__main__":
    main()
