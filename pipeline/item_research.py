"""Research-only, forward-held-out comparison of two finished-item choices.

Example:
  .venv/Scripts/python pipeline/item_research.py --champion Sett --role TOP \
      --slot 1 --item-a 6631 --item-b 3153

The output is an observational comparison. It is never exported to the site as
an item effect or used to label a build "best".
"""
import argparse
import json
import sqlite3
from pathlib import Path

from engine import ROOT, USABLE_SQL, extract
from model import evaluate


def cached_records(db_path, champion="all", role=None):
    """Use a read-only snapshot of already collected matches; never touch the collector."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    catalogs = {row["patch"]: json.loads(row["items"]) for row in db.execute("SELECT patch,items FROM catalog")}
    for row in db.execute(f"SELECT id,detail FROM matches WHERE {USABLE_SQL}"):
        match = json.loads(row["detail"])
        patch = ".".join(match["info"]["gameVersion"].split(".")[:2])
        if champion != "all" and not any(p.get("championName", "").lower() == champion.lower()
                                         and p.get("teamPosition") == role for p in match["info"]["participants"]):
            continue
        if patch in catalogs:
            stored = db.execute("SELECT timeline FROM matches WHERE id=?", (row["id"],)).fetchone()
            if stored:
                yield from extract(match, json.loads(stored["timeline"]), catalogs[patch])


def decision_rows(records, champion, role, slot, item_a, item_b, opponent="all"):
    """Construct decisions using only features recorded before item completion."""
    for row in records:
        if (champion != "all" and row["champion"].lower() != champion.lower()) or row["role"] != role:
            continue
        if opponent != "all" and row["opponent"].lower() != opponent.lower():
            continue
        if row.get("ledgerUncertain") or len(row.get("build") or []) < slot:
            continue
        item_id, _, minute = row["build"][slot - 1]
        item_id = str(item_id)
        if item_id not in (item_a, item_b):
            continue
        events = [e for e in row.get("purchases", []) if str(e["item"]) == item_id]
        if not events:
            continue
        event = min(events, key=lambda e: abs(e["time"] / 60000 - minute))
        pre = event.get("pre") or {}
        # The timeline has minute snapshots, not exact pre-purchase gold. Exclude
        # stale/missing snapshots and retain age so the learner can adjust for it.
        if not pre.get("snapshotAvailable") or pre.get("snapshotAgeMs", 10**9) > 120000:
            continue
        features = dict(row["features"])
        features.update(purchaseMinute=round(event["time"] / 60000, 2),
                        snapshotAgeSeconds=round(pre["snapshotAgeMs"] / 1000, 1),
                        currentGoldSnapshot=pre.get("currentGoldSnapshot"),
                        levelSnapshot=pre.get("levelSnapshot"),
                        teamGoldDifferenceSnapshot=pre.get("teamGoldDifferenceSnapshot"))
        if any(v is None for v in (features["currentGoldSnapshot"], features["levelSnapshot"],
                                   features["teamGoldDifferenceSnapshot"])):
            continue
        yield {"package": item_id, "win": row["win"], "features": features,
               "opponent": row["opponent"], "startedAt": row["startedAt"],
               "matchId": row["matchId"], "playerId": row["playerId"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "settistics.sqlite"))
    parser.add_argument("--champion", required=True)
    parser.add_argument("--role", required=True, choices=["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"])
    parser.add_argument("--opponent", default="all")
    parser.add_argument("--slot", type=int, required=True, choices=range(1, 6))
    parser.add_argument("--item-a", required=True)
    parser.add_argument("--item-b", required=True)
    parser.add_argument("--learner", choices=["logistic", "trees"], default="logistic")
    args = parser.parse_args()
    if args.item_a == args.item_b:
        parser.error("Choose two different items")
    rows = list(decision_rows(cached_records(args.db, args.champion, args.role), args.champion, args.role, args.slot,
                              args.item_a, args.item_b, args.opponent))
    report = evaluate(rows, args.item_a, args.item_b, args.learner)
    report.update(champion=args.champion, opponent=args.opponent, role=args.role,
                  slot=args.slot, itemA=args.item_a, itemB=args.item_b,
                  eligibleDecisions=len(rows), causalValidationPassed=False,
                  interpretation="research-only adjusted association; exact affordability and player skill remain unobserved")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
