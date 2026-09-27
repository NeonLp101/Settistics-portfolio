"""One-time prospective test of the frozen item recommender on games it has never seen (round r1).

    python pipeline/prospective.py check                       # frozen-code status, test game count
    python pipeline/prospective.py run [--backend xgb-cuda]    # builds the data, fits, tests once
    python pipeline/prospective.py freeze                      # maintainers only: rewrites the lock file

Training: exactly the development dataset's games, the 50,077 games in data/decisions.sqlite whose last one
started 2026-09-25 05:32:18 UTC. Test: every finished general-source game that is not among them and was
collected after the last of them, whenever it was played. None of these games was used or looked at while the
method was developed, so the test is fair for unseen games. Many were played on the same days as training
games, so it does not show whether the method holds up on later patches or weeks; later rounds should test
forward in time. `check` copies the development game list to data/research/prospective/r1/ the first time,
so rebuilding data/decisions.sqlite afterwards cannot move it.

The frozen method is the code whose hashes are in prospective-r1.lock.json, GATE and the XGBoost settings
in recommender.py, seed 0 and the pass rules in CRITERIA below. All of them were fixed before any test game
was read. `run` refuses if the frozen code changed and refuses a second run: the ledger in
data/research/prospective/r1/ is written before the first test outcome is read. After the run, every game
may train the next round, which needs new test games collected after this run, new rules and a new lock.

Outcome, for the primary policy (the enriched decision model; base is reported, never judged):
- inconclusive: the recommender gate cannot judge (under 1,000 test games, under 200 departures from the
  common choice, or too many extreme propensities), or the primary policy was not fitted.
- pass: the gate's verdict is supported_improvement (95% lower bound of the overall gain above zero), the
  mean gain per departed decision is at least 1 point, at least 5% of test decisions have supported pairs,
  and no region, patch or gold-state slice with at least 50 departures has a gain-per-departure 95% upper
  bound below -1 point.
- fail: anything else. A failure is a reason to improve the method on all games, then test on new ones.

Observational limits of recommender.py apply unchanged. A pass supports showing model leans as tested
advice for the supported decisions only; it proves no counterfactual outcome for an individual game.
"""
import argparse
import hashlib
import inspect
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import branches
import decisions
import engine
import recommender
from engine import ROOT

ROUND = "r1"
DESIGN = "development games train; games collected after them test"
DEVELOPMENT_GAMES = 50_077
DEVELOPMENT_LAST_START_MS = 1_790_314_338_284  # 2026-09-25 05:32:18.284 UTC
DEVELOPMENT_DB = ROOT / "data" / "decisions.sqlite"
SEED = 0
PRIMARY = "enriched"
CRITERIA = dict(min_gain_per_departure=0.01,   # 1 point of final-win chance per changed decision
                min_supported_share=0.05,       # test decisions whose pair has enough training support
                slice_min_departures=50,        # slices smaller than this are reported, not judged
                slice_max_harm=0.01,            # a slice fails if its 95% upper bound is below -1 point
                gold_band=1000)                 # team gold lead at purchase: behind < -1000 <= even <= 1000 < ahead
FROZEN_FILES = ("pipeline/prospective.py", "pipeline/recommender.py", "pipeline/decisions.py", "pipeline/branches.py",
                "pipeline/features.py", "pipeline/wpa.py", "pipeline/backends.py")
FROZEN_ENGINE = ("valid_match", "opponent_for", "purchases", "item_class")
LOCK = ROOT / "pipeline" / f"prospective-{ROUND}.lock.json"
WORK = ROOT / "data" / "research" / "prospective" / ROUND
DECISIONS = ROOT / "data" / f"prospective-{ROUND}-decisions.sqlite"
BRANCHES = ROOT / "data" / f"prospective-{ROUND}-branches.sqlite"
GOLD = recommender.FEATURES.index("team_gold")


def digest(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def fingerprint():
    """sha256 of every frozen file (line endings normalized) and of the engine helpers the dataset uses."""
    files = {f: digest((ROOT / f).read_text(encoding="utf-8")) for f in FROZEN_FILES}
    helpers = {f"engine.{n}": digest(inspect.getsource(getattr(engine, n))) for n in FROZEN_ENGINE}
    return dict(files, **helpers)


def lock_problems(lock_path=LOCK):
    if not Path(lock_path).exists():
        return [f"{lock_path} is missing"]
    frozen = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    now = fingerprint()
    if frozen.get("round") != ROUND or frozen.get("design") != DESIGN:
        return ["the lock file belongs to another round or design"]
    return [f"{k} changed since the freeze" for k in sorted(set(now) | set(frozen["hashes"]))
            if now.get(k) != frozen["hashes"].get(k)]


def gold_state(row, band=CRITERIA["gold_band"]):
    lead = row["state_pre"][GOLD]
    return "behind" if lead < -band else "ahead" if lead > band else "even"


def departure_gain(rows, arms, g, mask):
    """Doubly robust gain of taking route B over A, averaged over departed rows in mask, match-clustered."""
    sel = np.asarray(arms, dtype=bool) & np.asarray(mask, dtype=bool)
    gain = g[:, 1] - g[:, 0]
    return recommender.cluster_mean(gain[sel], [r["match_id"] for r, s in zip(rows, sel) if s])


def slices(rows, arms, g):
    out = {}
    keys = dict(region=lambda r: r["region"], patch=lambda r: r["patch"], gold_state=gold_state)
    for kind, key in keys.items():
        labels = [key(r) for r in rows]
        for value in sorted(set(labels)):
            mask = np.asarray([v == value for v in labels])
            out[f"{kind}:{value}"] = dict(rows=int(mask.sum()), departures=int(np.asarray(arms)[mask].sum()),
                                          gain_per_departure=departure_gain(rows, arms, g, mask))
    return out


def judge(section, per_departure, supported_share, slice_table, criteria=CRITERIA):
    """('pass' | 'fail' | 'inconclusive', reasons). Rules fixed before any test game was read."""
    if not section or section.get("status") == "not_fitted":
        return "inconclusive", ["the primary policy's decision model was not fitted"]
    if section["verdict"] in ("insufficient_evidence", "insufficient_overlap"):
        return "inconclusive", [f"recommender gate: {section['verdict']}"]
    reasons = []
    if section["verdict"] != "supported_improvement":
        reasons.append("the overall gain's 95% lower bound is not above zero")
    if per_departure["mean"] is None or per_departure["mean"] < criteria["min_gain_per_departure"]:
        reasons.append("the mean gain per departed decision is below 1 point")
    if supported_share < criteria["min_supported_share"]:
        reasons.append(f"only {supported_share:.1%} of test decisions have supported pairs")
    for name, cell in sorted(slice_table.items()):
        high = cell["gain_per_departure"]["ci_high"]
        if cell["departures"] >= criteria["slice_min_departures"] and high is not None and high < -criteria["slice_max_harm"]:
            reasons.append(f"slice {name} is worse by more than 1 point")
    return ("pass" if not reasons else "fail"), reasons


def read_ledger(work=WORK):
    path = Path(work) / "ledger.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_ledger(entry, work=WORK):
    Path(work).mkdir(parents=True, exist_ok=True)
    (Path(work) / "ledger.json").write_text(json.dumps(entry, indent=1), encoding="utf-8")


def build_data(db, train_ids, test_ids, workers=None, log=print):
    log(f"decision dataset -> {DECISIONS} ({len(train_ids):,} training and {len(test_ids):,} test games listed)")
    decisions.build(db, DECISIONS, workers=workers, fixed=(train_ids, test_ids), log=log)
    catalogs, versions = branches.load_catalogs(None, db)
    log(f"branch comparisons -> {BRANCHES}")
    branches.build(DECISIONS, BRANCHES, catalogs, versions, include_heldout=True, log=log)


def development_games(work=WORK, development_db=DEVELOPMENT_DB):
    """The frozen training list. Read from data/decisions.sqlite once, checked against the recorded size and last
    start, then kept in the round's directory."""
    snapshot = Path(work) / "development-games.json"
    if snapshot.exists():
        ids = json.loads(snapshot.read_text(encoding="utf-8"))
    else:
        if not Path(development_db).exists():
            raise FileNotFoundError(f"{development_db} is missing: the development game list cannot be recovered")
        con = sqlite3.connect(Path(development_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
        found = con.execute("SELECT match_id, started_at FROM split_matches").fetchall()
        con.close()
        last = max((t for _, t in found), default=None)
        if len(found) != DEVELOPMENT_GAMES or last != DEVELOPMENT_LAST_START_MS:
            raise ValueError(f"{development_db} has {len(found):,} games ending {last}; the development dataset had "
                             f"{DEVELOPMENT_GAMES:,} ending {DEVELOPMENT_LAST_START_MS}. It was rebuilt: stop and ask.")
        ids = sorted(m for m, _ in found)
        Path(work).mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(ids), encoding="utf-8")
    if len(ids) != DEVELOPMENT_GAMES:
        raise ValueError(f"{snapshot} lists {len(ids):,} games, not {DEVELOPMENT_GAMES:,}")
    return set(ids)


def test_games(db, train_ids):
    """General-source finished games outside the training list, collected after the last training game was."""
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    rows = con.execute(f"SELECT id, collected_at FROM matches WHERE {decisions.selection_sql(False)}").fetchall()
    con.close()
    built = max((c for i, c in rows if i in train_ids and c), default=None)
    if built is None:
        raise ValueError("no training game found in the database")
    return {i for i, c in rows if i not in train_ids and c and c > built}, built


def blockers(lock_path=LOCK, work=WORK):
    """Reasons `run` must refuse: changed frozen code, or a round that was already opened."""
    problems = [f"frozen code: {p}" for p in lock_problems(lock_path)]
    ledger = read_ledger(work)
    if ledger:
        problems.append(f"round {ROUND} already {ledger['status']}: the same games are never tested twice")
    return problems


def test(branches_path, train_ids, test_ids, backend="xgb-cpu", work=WORK, log=print, _small=None):
    """Fit on the training games, then read the test outcomes once. Returns the report.

    _small is for the unit tests only (a smaller gate and models on synthetic data); `run` never passes it."""
    if read_ledger(work):
        raise RuntimeError(f"round {ROUND} was already opened; the same games are never tested twice")
    small = _small or {}
    gate = small.get("gate", recommender.GATE)
    backend = recommender.resolve(backend)
    rows, pairs, meta = recommender.load(branches_path, "heldout")
    fit_rows = [r for r in rows if r["split_role"] == "train"]
    ev = [r for r in rows if r["split_role"] == "evaluate"]
    if not fit_rows or not ev:
        raise ValueError(f"{len(fit_rows)} training rows, {len(ev)} test rows")
    if any(r["focus_source"] for r in fit_rows + ev):
        raise ValueError("focus-source rows among training or test rows")
    fit_games, ev_games = {r["match_id"] for r in fit_rows}, {r["match_id"] for r in ev}
    if fit_games & ev_games or not fit_games <= set(train_ids) or not ev_games <= set(test_ids):
        raise ValueError("training and test rows do not match the frozen lists: rebuild the data with this command")
    log(f"fitting on {len(fit_rows):,} training rows ({backend})")
    fitted = recommender.fit_models(fit_rows, backend=backend, seed=SEED, params=small.get("params"),
                                    decision_params=small.get("decision_params"),
                                    **({"min_crossfit_rows": small["min_crossfit_rows"]} if "min_crossfit_rows" in small else {}))
    write_ledger(dict(round=ROUND, status="opened", opened_at=int(time.time()), design=DESIGN,
                      hashes=fingerprint(), backend=backend, fit_rows=len(fit_rows), test_rows=len(ev)), work)
    log(f"reading {len(ev):,} test rows once")
    evaluation, choices = recommender.evaluate(fitted, ev, gate)
    pred = recommender.candidates(fitted, ev)
    g = recommender.dr_scores([r["win"] for r in ev], [r["arm"] for r in ev], pred["win"], pred["propensity"],
                              gate["weight_clip"])
    arms = choices.get(PRIMARY, np.zeros(len(ev), dtype=int))
    per_departure = departure_gain(ev, arms, g, np.ones(len(ev), dtype=bool))
    support = fitted["support"]
    supported = sum(min(support.get((r["pair_id"], 0), 0), support.get((r["pair_id"], 1), 0))
                    >= gate["min_arm_train_rows"] for r in ev)
    supported_share = supported / len(ev) if ev else 0.0
    slice_table = slices(ev, arms, g)
    outcome, reasons = judge(evaluation["policies"].get(PRIMARY), per_departure, supported_share, slice_table)
    report = recommender.clean(dict(
        round=ROUND, outcome=outcome, reasons=reasons, primary_policy=PRIMARY, criteria=CRITERIA,
        design=DESIGN, development_games=DEVELOPMENT_GAMES, test_games_listed=len(test_ids), created_at=int(time.time()),
        backend=backend, seed=SEED, gate=gate, xgb=small.get("params", recommender.XGB),
        decision_xgb=small.get("decision_params", recommender.DECISION_XGB), unit_test=bool(_small),
        hashes=fingerprint(), input=dict(branches=str(branches_path), branch_meta=meta),
        fit=dict(rows=fitted["rows"], matches=fitted["matches"], crossfit=fitted["crossfit"]),
        test=dict(rows=len(ev), matches=len({r["match_id"] for r in ev}), supported_rows=supported,
                  supported_share=supported_share),
        gain_per_departure=per_departure, slices=slice_table, evaluation=evaluation,
        caveats=recommender.CAVEATS + ["Rows are branch decisions (first distinguishing purchase or boots upgrade), "
                                       "not every purchase; supported_share counts those rows."]))
    Path(work).mkdir(parents=True, exist_ok=True)
    (Path(work) / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    write_ledger(dict(round=ROUND, status="done", opened_at=read_ledger(work)["opened_at"], finished_at=int(time.time()),
                      design=DESIGN, outcome=outcome, reasons=reasons, hashes=report["hashes"]), work)
    return report


def summary(report):
    p = report["gain_per_departure"]
    pts = lambda v: "n/a" if v is None else f"{100 * v:+.2f}"
    policy = report["evaluation"]["policies"].get(PRIMARY, {})
    overall = policy.get("gain_vs_baseline", {})
    lines = [f"Round {ROUND}: {report['outcome'].upper()}",
             f"test: {report['test']['matches']:,} games, {report['test']['rows']:,} decisions, "
             f"{report['test']['supported_share']:.1%} supported, {policy.get('departures', 0):,} departures",
             f"overall gain {pts(overall.get('mean'))} points [{pts(overall.get('ci_low'))}, {pts(overall.get('ci_high'))}]",
             f"gain per departure {pts(p['mean'])} points [{pts(p['ci_low'])}, {pts(p['ci_high'])}]"]
    lines += [f"- {r}" for r in report["reasons"]]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "run", "freeze"))
    parser.add_argument("--db", default=str(ROOT / "data" / "settistics.sqlite"))
    parser.add_argument("--backend", default="xgb-cpu", choices=("auto", "xgb-cpu", "xgb-cuda"))
    parser.add_argument("--workers", type=int)
    parser.add_argument("--skip-build", action="store_true",
                        help="reuse this round's data files after a crash before the test was opened")
    args = parser.parse_args()
    if args.command == "freeze":
        LOCK.write_text(json.dumps(dict(round=ROUND, design=DESIGN, frozen_at=datetime.now(timezone.utc).isoformat(),
                                        hashes=fingerprint()), indent=1) + "\n", encoding="utf-8")
        print(f"frozen -> {LOCK}")
        return
    if args.command == "check":
        problems, ledger = lock_problems(), read_ledger()
        print(f"round {ROUND}: {DESIGN}")
        print("frozen code: " + ("intact" if not problems else "; ".join(problems)))
        print("ledger: " + (ledger["status"] if ledger else "not run"))
        train_ids = development_games()
        tested, built = test_games(args.db, train_ids)
        print(f"training games: {len(train_ids):,} (development list intact)")
        print(f"test games so far: {len(tested):,} (collected after {built})")
        return
    problems = blockers()
    if problems:
        parser.error("refusing: " + "; ".join(problems))
    train_ids = development_games()
    tested, _ = test_games(args.db, train_ids)
    if args.skip_build:  # reuse the data built for this run: its test list is the one stored with it
        tested = set(json.loads((WORK / "test-games.json").read_text(encoding="utf-8")))
    else:
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "test-games.json").write_text(json.dumps(sorted(tested)), encoding="utf-8")
        build_data(args.db, train_ids, tested, args.workers)
    report = test(BRANCHES, train_ids, tested, args.backend)
    print(summary(report))
    print(f"report -> {WORK / 'report.json'}")


if __name__ == "__main__":
    main()
