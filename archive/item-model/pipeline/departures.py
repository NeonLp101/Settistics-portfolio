"""Why does the item policy leave the common route so rarely? Development diagnostic, not an evaluation.

    python pipeline/departures.py                       # funnels from the r1/r2 reports, then a refit on training games
    python pipeline/departures.py --reports-only        # only the report funnels (seconds)
    python pipeline/departures.py --backend xgb-cuda

Part 1 reads the saved r1/r2 reports: how many test decisions stopped at each gate (no supported pair, too
little support in an arm, propensity outside the overlap band, predicted gain below the 3-point margin) and
how many departed. It reads no outcomes beyond what the reports already hold.

Part 2 uses only r1's training games (the development games, already inspected during development), never a
test game. It refits the unchanged models on the earlier 75% of those games and predicts on the later 25%:
- the distribution of the decision models' predicted win-chance difference between route B and route A;
- how much of the decision models' total gain comes from the route features at all (the arm is one feature
  among ~40 in a heavily regularized final-win model);
- the funnel at the current gate, and departures at smaller margins with their doubly robust gain on these
  development rows. Those gains are exploratory: they may guide the next method, and any changed method needs
  a new frozen round on games collected after it.

Output: text summary and data/research/departures/diagnostics.json (private).
"""
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np

import recommender
from engine import ROOT

REPORTS = {"r1": ROOT / "data" / "research" / "prospective" / "r1" / "report.json",
           "r2": ROOT / "data" / "research" / "prospective" / "r2" / "report.json"}
BRANCHES = ROOT / "data" / "prospective-r1-branches.sqlite"
OUT = ROOT / "data" / "research" / "departures"
MARGINS = (0.03, 0.02, 0.01, 0.005, 0.0)
GATE_ORDER = ("unsupported_pair", "arm_support_below_min", "model_unavailable", "outside_overlap",
              "below_preference_margin", "depart")


def pct(n, d):
    return f"{100 * n / d:5.1f}%" if d else "  n/a"


def report_funnels(paths=REPORTS, log=print):
    out = {}
    for name, path in paths.items():
        if not Path(path).exists():
            log(f"{name}: {path} not found, skipped")
            continue
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        ev = report["evaluation"]
        out[name] = {}
        log(f"\n{name}: {ev['rows']:,} test decisions in {ev['matches']:,} games")
        for policy, section in ev["policies"].items():
            if "reasons" not in section:
                continue
            reasons = section["reasons"]
            out[name][policy] = reasons
            log(f"  {policy} policy, where decisions stop:")
            for gate in GATE_ORDER:
                log(f"    {gate:<24} {reasons.get(gate, 0):>9,}  {pct(reasons.get(gate, 0), ev['rows'])}")
        prop = ev.get("propensity", {})
        log(f"  propensity outside the {recommender.GATE['overlap']} band: {prop.get('outside_overlap_share', 0):.1%} "
            f"of rows; route B share {prop.get('mean_propensity', 0):.1%} on average")
    return out


def load_training(path):
    """r1's training rows only; the query never selects a test row."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    db.row_factory = sqlite3.Row
    rows = [recommender.parse(dict(r)) for r in db.execute(
        "SELECT * FROM branch_comparison WHERE fold='heldout' AND split_role='train'")]
    db.close()
    return rows


def time_split(rows, share=0.75):
    starts = sorted({(r["started_at"], r["match_id"]) for r in rows})
    cut = starts[int(len(starts) * share)][0]
    early = [r for r in rows if r["started_at"] + r["duration_ms"] < cut]
    late = [r for r in rows if r["started_at"] >= cut]
    return early, late


def route_gain_share(fitted_model):
    """Share of the model's total split gain that comes from the two route features (arm_b, candidate_route)."""
    if fitted_model.model is None:
        return None
    gains = fitted_model.model.get_booster().get_score(importance_type="total_gain")
    total = sum(gains.values())
    arm = len(recommender.CONTEXT_NAMES)
    route = sum(gains.get(f"f{i}", 0.0) for i in (arm, arm + 1))
    return route / total if total else 0.0


def refit_diagnostics(rows, backend="xgb-cpu", log=print):
    early, late = time_split(rows)
    log(f"\nrefit: {len({r['match_id'] for r in early}):,} earlier training games fit, "
        f"{len({r['match_id'] for r in late}):,} later ones ({len(late):,} decisions) are scored")
    fitted = recommender.fit_models(early, backend=backend, seed=0)
    pred = recommender.candidates(fitted, late)
    e, support, n = pred["propensity"], fitted["support"], len(late)
    y = np.asarray([r["win"] for r in late], dtype=float)
    a = np.asarray([r["arm"] for r in late], dtype=int)
    g = recommender.dr_scores(y, a, pred["win"], e, recommender.GATE["weight_clip"])
    clusters = [r["match_id"] for r in late]
    out = dict(fit_games=len({r["match_id"] for r in early}), scored_rows=n, policies={})
    minority = [min(support.get((r["pair_id"], 0), 0), support.get((r["pair_id"], 1), 0)) /
                max(1, support.get((r["pair_id"], 0), 0) + support.get((r["pair_id"], 1), 0)) for r in late]
    out["rare_route_b"] = {f"<{t:.0%}": float(np.mean(np.asarray(minority) < t)) for t in (0.05, 0.1, 0.2)}
    log(f"decisions whose pair's rarer route has under 10% of training buyers: {out['rare_route_b']['<10%']:.1%}")
    for name in ("base", "enriched"):
        if np.isnan(pred[name]).all():
            continue
        delta = pred[name][:, 1] - pred[name][:, 0]
        q = {k: float(np.quantile(delta, k)) for k in (0.01, 0.1, 0.5, 0.9, 0.99, 0.999)}
        above = {m: float(np.mean(delta > m)) for m in MARGINS}
        section = dict(delta_quantiles=q, share_above=above,
                       share_near_zero=float(np.mean(np.abs(delta) < 0.001)),
                       route_gain_share=route_gain_share(fitted["models"][f"decision_{name}"]), margins={})
        _, reasons = recommender.choose(late, delta, e, support, recommender.GATE)
        section["reasons"] = dict(Counter(reasons))
        log(f"\n  {name} decision model")
        log(f"    share of split gain from the route features: {section['route_gain_share']:.2%}"
            if section["route_gain_share"] is not None else "    not fitted")
        log(f"    predicted B-minus-A win chance, points: " +
            ", ".join(f"p{int(k * 1000) / 10:g} {100 * v:+.2f}" for k, v in q.items()))
        log(f"    |difference| under 0.1 point: {section['share_near_zero']:.1%} of decisions")
        for gate in GATE_ORDER:
            log(f"    {gate:<24} {section['reasons'].get(gate, 0):>9,}  {pct(section['reasons'].get(gate, 0), n)}")
        log("    margin   departures   gain per departure (pts, exploratory)")
        for m in MARGINS:
            arms, _ = recommender.choose(late, delta, e, support, dict(recommender.GATE, preference_margin=m))
            sel = arms.astype(bool)
            gain = recommender.cluster_mean((g[:, 1] - g[:, 0])[sel], [c for c, s in zip(clusters, sel) if s])
            section["margins"][str(m)] = dict(departures=int(sel.sum()), share=float(sel.mean()), gain_per_departure=gain)
            fmt = lambda v: "n/a" if v is None else f"{100 * v:+.2f}"
            log(f"    {100 * m:4.1f}pt  {int(sel.sum()):>9,} ({sel.mean():.2%})   {fmt(gain['mean'])} "
                f"[{fmt(gain['ci_low'])}, {fmt(gain['ci_high'])}]")
        out["policies"][name] = section
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branches", default=str(BRANCHES))
    parser.add_argument("--backend", default="xgb-cpu", choices=("auto", "xgb-cpu", "xgb-cuda"))
    parser.add_argument("--reports-only", action="store_true")
    args = parser.parse_args()
    result = dict(reports=report_funnels())
    if not args.reports_only:
        result["refit"] = refit_diagnostics(load_training(args.branches), recommender.resolve(args.backend))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "diagnostics.json").write_text(json.dumps(recommender.clean(result), indent=1), encoding="utf-8")
    print(f"\n-> {OUT / 'diagnostics.json'}")


if __name__ == "__main__":
    main()
