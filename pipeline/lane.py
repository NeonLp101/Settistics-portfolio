"""Lane 1v1 mode (prototype): how a choice changes the gold lead against the lane opponent
while no third champion interferes.

    .venv/Scripts/python pipeline/lane.py
    python pipeline/engine.py export --champion all
    npm run build

Top and mid only (the real 1v1 lanes). Each laning window is measured only until the first interference:
a kill or assist involving either laner in which another champion took part, or a second per-minute
snapshot with another champion near them. Windows that stay 1v1 for under 3 minutes are dropped. The outcome is the change in the lane gold lead over the window; a model
trained on other games predicts that change from the state at the start of the window, and the choice
gets credit for the difference ("Lane Advantage Added", in gold).

Keystone and summoner spells need no item data. Starting items, first item and boots are added once the
patch's Data Dragon item list is available. Requires requirements-model.txt (numpy, scikit-learn).
"""
import argparse
import bisect
import json
import math
import zlib
from collections import Counter

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

from engine import ROOT, USABLE_SQL, connect, extract, opponent_for, utc, valid_match, write_json

FOLDS = 5
ROLES = ("TOP", "MIDDLE")
NEAR = 1500                # map units: "near" the lane duel
MAX_NEAR_FRAMES = 1        # tolerate one per-minute snapshot with a third champion close by
MIN_CLEAN_MS = 180_000     # a window must stay 1v1 for at least 3 minutes to count
EARLY = (60_000, 600_000)  # starting items, keystone, spells: minute 1 to 10
AFTER_BUY_MS = 300_000     # finished items: the 5 minutes after the purchase
LAST_BUY_MS = 840_000      # items only count if finished by minute 14 (laning phase)
LANE_END_MS = 960_000
BASE_FEATURES = ["start", "length", "gold_diff", "level_diff", "mid"]
# Who is in the lane matters most: each champion's usual lane gold rate and the specific pairing, both
# computed out-of-fold, plus CS and XP gaps at the start of the window.
MATCHUP_FEATURES = BASE_FEATURES + ["cs_diff", "xp_diff", "champ_rate", "opp_rate", "pair_rate", "champ_exp", "pair_exp"]
# Current fighting state at the start of the window (all "mine minus theirs"): health, item power, unspent gold,
# trading so far and the gold trend of the last 2 minutes. (Wave position was tested and made predictions worse.)
STATE_FEATURES = ["hp_frac", "hp_max", "ad", "ap", "armor", "mr", "attack_speed", "gold_now", "dmg_dealt", "dmg_taken", "trend"]
FEATURES = MATCHUP_FEATURES + STATE_FEATURES
CHAMP_PRIOR_MIN = 60   # minutes of "average" lane (0 gold/min) blended into each champion's rate
PAIR_PRIOR_MIN = 120   # pairings are rarer, so they are pulled harder toward the champions' rates


class Lane:
    """Per-minute gold, level and position of every player, plus who took part in each kill."""

    def __init__(self, match, timeline):
        frames = timeline["info"]["frames"]
        self.ts = [f["timestamp"] for f in frames]
        self.pids = [p["participantId"] for p in match["info"]["participants"]]
        self.gold, self.level, self.pos, self.xp, self.cs, self.stats = {}, {}, {}, {}, {}, {}
        for pid in self.pids:
            pf = [f["participantFrames"].get(str(pid), {}) for f in frames]
            self.gold[pid] = [x.get("totalGold", 0) for x in pf]
            self.xp[pid] = [x.get("xp", 0) for x in pf]
            self.cs[pid] = [x.get("minionsKilled", 0) + x.get("jungleMinionsKilled", 0) for x in pf]
            self.stats[pid] = [self._stats(x) for x in pf]
            self.level[pid] = [x.get("level", 1) for x in pf]
            self.pos[pid] = [(x["position"]["x"], x["position"]["y"]) if x.get("position") else None for x in pf]
        self.kills = [(e.get("timestamp", 0), {e.get("killerId"), e.get("victimId"), *e.get("assistingParticipantIds", [])} - {0, None})
                      for f in frames for e in f.get("events", []) if e.get("type") == "CHAMPION_KILL"]

    @staticmethod
    def _stats(x):
        c, d = x.get("championStats", {}), x.get("damageStats", {})
        hp_max = c.get("healthMax") or 1
        return {"hp_frac": c.get("health", 0) / hp_max, "hp_max": hp_max, "ad": c.get("attackDamage", 0), "ap": c.get("abilityPower", 0),
                "armor": c.get("armor", 0), "mr": c.get("magicResist", 0), "attack_speed": c.get("attackSpeed", 0),
                "gold_now": x.get("currentGold", 0), "dmg_dealt": d.get("totalDamageDoneToChampions", 0), "dmg_taken": d.get("totalDamageTaken", 0)}

    def state(self, a, b, t):
        fi = self.frame(t)
        sa, sb = self.stats[a][fi], self.stats[b][fi]
        out = {k: sa[k] - sb[k] for k in sa}
        before = self.frame(t - 120_000)
        out["trend"] = (self.gold[a][fi] - self.gold[b][fi]) - (self.gold[a][before] - self.gold[b][before])
        return out

    def frame(self, t):
        return max(0, bisect.bisect_right(self.ts, t) - 1)

    def clean_until(self, a, b, t0, t1):
        """Last snapshot time before anyone but a and b influenced their duel (at most t1), or None if under 3 min.

        Interference: a kill involving a or b in which a third champion took part (killer, victim or assist),
        or a second per-minute snapshot with a third champion near either laner. Nothing from the
        interfering moment onward is counted, so a gank's gold never enters the measurement."""
        end = min([ts for ts, who in self.kills if t0 <= ts <= t1 and (a in who or b in who) and who - {a, b}] + [t1 + 1])
        near, last = 0, None
        for fi in range(self.frame(t0), len(self.ts)):
            if self.ts[fi] < t0:
                continue
            if self.ts[fi] >= end:
                break
            duel = [p for p in (self.pos[a][fi], self.pos[b][fi]) if p]
            if any(self.pos[q][fi] and any(math.dist(self.pos[q][fi], p) < NEAR for p in duel)
                   for q in self.pids if q not in (a, b)):
                near += 1
                if near > MAX_NEAR_FRAMES:
                    break
            last = self.ts[fi]
        return last if last is not None and last - t0 >= MIN_CLEAN_MS else None

    def lead(self, a, b, t):
        fi = self.frame(t)
        return self.gold[a][fi] - self.gold[b][fi], self.level[a][fi] - self.level[b][fi]

    def gaps(self, a, b, t):
        fi = self.frame(t)
        return self.cs[a][fi] - self.cs[b][fi], self.xp[a][fi] - self.xp[b][fi]


def windows(match, timeline, items):
    """(player, choice kind, choice id, window start, window end) for every top/mid laner."""
    info = match["info"]
    records = {r["playerId"]: r for r in extract(match, timeline, items)} if items else {}
    for p in info["participants"]:
        opp = opponent_for(info["participants"], p)
        if p.get("teamPosition") not in ROLES or opp is None:
            continue
        choices = []
        styles = p.get("perks", {}).get("styles", [])
        if styles and styles[0].get("selections"):
            choices.append(("keystone", str(styles[0]["selections"][0]["perk"]), *EARLY))
        choices.append(("spells", "+".join(map(str, sorted([p.get("summoner1Id", 0), p.get("summoner2Id", 0)]))), *EARLY))
        rec = records.get(p.get("playerRef", ""))
        if rec:
            if rec.get("package"):
                choices.append(("packages", rec["package"], *EARLY))
            for kind, bought in (("slot1", rec["build"][0] if rec.get("build") else None), ("boots", rec.get("boots"))):
                if bought and bought[2] * 60000 <= LAST_BUY_MS:
                    t0 = int(bought[2] * 60000)
                    choices.append((kind, bought[0], t0, min(t0 + AFTER_BUY_MS, LANE_END_MS)))
        for kind, item, t0, t1 in choices:
            yield p, opp, kind, item, t0, t1


def load(db):
    catalogs = {r["patch"]: json.loads(r["items"]) for r in db.execute("SELECT patch, items FROM catalog")}
    rows, counts = [], Counter()
    for row in db.execute(f"SELECT id, detail, timeline FROM matches WHERE {USABLE_SQL} ORDER BY id"):
        match, timeline = json.loads(row["detail"]), json.loads(row["timeline"])
        if not valid_match(match["info"]):
            continue
        lane, fold = Lane(match, timeline), zlib.crc32(row["id"].encode()) % FOLDS
        items = catalogs.get(".".join(match["info"]["gameVersion"].split(".")[:2]))
        for p, opp, kind, item, t0, t1 in windows(match, timeline, items):
            a, b = p["participantId"], opp["participantId"]
            counts[(kind, "all")] += 1
            end = lane.clean_until(a, b, t0, t1)
            if end is None:
                continue
            counts[(kind, "clean")] += 1
            g0, l0 = lane.lead(a, b, t0)
            g1, _ = lane.lead(a, b, end)
            cs0, xp0 = lane.gaps(a, b, t0)
            fight = lane.state(a, b, t0)
            t1 = end
            rows.append({"match": row["id"], "player": p.get("playerRef", ""), "fold": fold, "kind": kind, "item": item,
                         "start": t0 / 60000, "length": (t1 - t0) / 60000, "gold_diff": g0, "level_diff": l0,
                         "mid": int(p["teamPosition"] == "MIDDLE"), "delta": g1 - g0,
                         "cs_diff": cs0, "xp_diff": xp0, "champ": p["championName"], "opp": opp["championName"], **fight})
    return rows, counts


def add_matchup_strength(rows):
    """Out-of-fold lane gold per minute for each champion (per role) and each pairing; a game never informs its own features."""
    for k in range(FOLDS):
        champ, pair = {}, {}
        for r in rows:
            if r["fold"] == k:
                continue
            for table, key in ((champ, (r["champ"], r["mid"])), (pair, (r["champ"], r["opp"], r["mid"]))):
                d, m = table.get(key, (0.0, 0.0))
                table[key] = (d + r["delta"], m + r["length"])
        rate = lambda table, key, prior, base=0.0: (table.get(key, (0, 0))[0] + prior * base) / (table.get(key, (0, 0))[1] + prior)
        for r in rows:
            if r["fold"] != k:
                continue
            own, other = rate(champ, (r["champ"], r["mid"]), CHAMP_PRIOR_MIN), rate(champ, (r["opp"], r["mid"]), CHAMP_PRIOR_MIN)
            r["champ_rate"], r["opp_rate"] = own, other
            r["pair_rate"] = rate(pair, (r["champ"], r["opp"], r["mid"]), PAIR_PRIOR_MIN, (own - other))
            r["champ_exp"], r["pair_exp"] = (own - other) * r["length"], r["pair_rate"] * r["length"]

def cross_fit(rows, features=FEATURES):
    X = np.array([[r[f] for f in features] for r in rows], dtype=float)
    y = np.array([r["delta"] for r in rows], dtype=float)
    folds = np.array([r["fold"] for r in rows])
    pred = np.zeros(len(rows))
    for k in range(FOLDS):
        model = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, min_samples_leaf=50, random_state=k)
        model.fit(X[folds != k], y[folds != k])
        pred[folds == k] = model.predict(X[folds == k])
    return y, pred


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(ROOT / "data" / "settistics.sqlite"))
    args = parser.parse_args()
    db = connect(args.db)
    print("Finding clean 1v1 laning windows...", flush=True)
    rows, counts = load(db)
    if len(rows) < 100:
        raise SystemExit(f"Only {len(rows)} clean laning windows; collect more games first.")
    add_matchup_strength(rows)
    y, base_pred = cross_fit(rows, BASE_FEATURES)
    y, matchup_pred = cross_fit(rows, MATCHUP_FEATURES)
    y, pred = cross_fit(rows)
    r2 = lambda p: 1 - float(((y - p) ** 2).sum() / ((y - y.mean()) ** 2).sum())
    db.execute("DROP TABLE IF EXISTS lane_predictions")
    db.execute("CREATE TABLE lane_predictions (match_id TEXT, player_ref TEXT, kind TEXT, item TEXT, delta REAL, predicted REAL, "
               "PRIMARY KEY(match_id, player_ref, kind, item))")
    db.executemany("INSERT OR REPLACE INTO lane_predictions VALUES (?,?,?,?,?,?)",
                   [(r["match"], r["player"], r["kind"], r["item"], float(r["delta"]), float(p)) for r, p in zip(rows, pred)])
    db.commit()
    kinds = sorted({k for k, _ in counts})
    report = {"generatedAt": utc(), "windows": len(rows),
              "cleanShare": {k: round(counts[(k, "clean")] / counts[(k, "all")], 3) for k in kinds if counts[(k, "all")]},
              "maeGold": round(float(np.abs(y - pred).mean()), 1), "baselineMaeGold": round(float(np.abs(y - y.mean()).mean()), 1),
              "stateOnlyMaeGold": round(float(np.abs(y - base_pred).mean()), 1),
              "r2": round(r2(pred), 3), "stateOnlyR2": round(r2(base_pred), 3), "matchupR2": round(r2(matchup_pred), 3), "features": FEATURES}
    write_json(ROOT / "data" / "public" / "lane_model.json", report)
    shares = ", ".join(f"{k} {v:.0%}" for k, v in report["cleanShare"].items())
    print(f"Done. {len(rows):,} clean windows (clean share: {shares}). Prediction error {report['maeGold']} gold "
          f"vs {report['baselineMaeGold']} without the model and {report['stateOnlyMaeGold']} with lane state only. "
          f"Explains {report['r2']:.1%} of the gold swing (lane state only: {report['stateOnlyR2']:.1%}).")


if __name__ == "__main__":
    main()
