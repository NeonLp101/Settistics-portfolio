"""One-time migration: add enemy_ap_share to an existing branch_comparison table.

    python pipeline/backfill_enemy_ap_share.py --branches data/fulltrain-r3-branches.sqlite

`recommender.py`'s Encoder now reads `enemy_ap_share` as a context feature (see engine.enemy_ap_share),
but that column did not exist when data/fulltrain-r3-branches.sqlite was built. Re-running the full
decisions.py -> branches.py extraction would re-derive every other column too and risks not reproducing
exactly the same frozen row population. Instead this computes the one new value per (match_id, team_id)
straight from the raw match JSON already cached in the main database and updates it in place -- nothing
else in the file changes. Read-only against the main database; only ADD COLUMN and UPDATE against the
target file, which is never the sealed cohort (the branches file already excludes it).
"""
import argparse
import json
import sqlite3
from pathlib import Path

from engine import ROOT, enemy_ap_share


def build(branches_path, db_path, batch=500, log=print):
    branches = sqlite3.connect(branches_path, timeout=60)
    cols = [r[1] for r in branches.execute("PRAGMA table_info(branch_comparison)")]
    if "enemy_ap_share" not in cols:
        branches.execute("ALTER TABLE branch_comparison ADD COLUMN enemy_ap_share REAL")
        branches.commit()
    # Without an index, each UPDATE below is a full scan of branch_comparison (2M+ rows) repeated once
    # per (match_id, team_id) pair (~260K times) -- effectively quadratic and impractically slow. A
    # temporary index makes each one a lookup instead.
    branches.execute("CREATE INDEX IF NOT EXISTS tmp_match_team ON branch_comparison(match_id, team_id)")
    branches.commit()
    pairs = [tuple(r) for r in branches.execute("SELECT DISTINCT match_id, team_id FROM branch_comparison")]
    match_ids = sorted({m for m, _ in pairs})
    main = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    shares = {}  # (match_id, team_id) -> enemy_ap_share
    done = 0
    for i in range(0, len(match_ids), batch):
        chunk = match_ids[i:i + batch]
        rows = main.execute(f"SELECT id, detail FROM matches WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        for mid, detail in rows:
            participants = json.loads(detail)["info"]["participants"]
            for team in (100, 200):
                shares[(mid, team)] = enemy_ap_share(participants, team)
        done += len(chunk)
        if done % 5000 < batch:
            log(f"read {done:,} of {len(match_ids):,} matches", flush=True)
    main.close()
    updates = [(shares.get((m, t)), m, t) for m, t in pairs]
    branches.executemany("UPDATE branch_comparison SET enemy_ap_share=? WHERE match_id=? AND team_id=?", updates)
    branches.commit()
    branches.execute("DROP INDEX tmp_match_team")
    branches.commit()
    missing = sum(1 for v, _, _ in updates if v is None)
    log(f"{len(pairs):,} (match_id, team_id) pairs updated across {len(match_ids):,} matches; "
        f"{missing:,} left NULL (unknown champion or not exactly 5 enemies)")
    branches.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branches", required=True, help="branch_comparison sqlite file to update in place")
    parser.add_argument("--db", default=str(ROOT / "data" / "settistics.sqlite"))
    args = parser.parse_args()
    build(args.branches, args.db)


if __name__ == "__main__":
    main()
