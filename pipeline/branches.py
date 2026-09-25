"""Observable item-path branch comparisons, derived from purchase_decisions and its split manifest.

    python pipeline/branches.py --decisions data/decisions-sample.sqlite --catalog-json item.json \\
        --out data/branch-comparison-sample.sqlite

For every champion/role and stage (slot1-3 and boots) and every development fold, the two most-completed
finished routes are chosen from that fold's general-source TRAIN games only, each needing min_support
completing player-stages there. The patch's own recipe trees give the parts that distinguish them: a part in
one route's tree and not in the other's. Parts in both trees are shared and never assign anyone.

A player-stage is assigned to a branch at its first observable distinguishing purchase, dated at that
purchase: the row keeps that purchase's pre-purchase context and its fixed-window outcomes. Whatever the
player buys or completes afterwards plays no part, so players who never finish either route stay in the
table. Nothing is inferred from the final item. A player-stage stays unassigned (and is only counted) when:

- route_already_completed: a route item was finished in an earlier slot, so both routes were not open;
- no_distinguishing_purchase: no distinguishing purchase in the stage;
- carried_part: a distinguishing part was already held from an earlier stage, so the branch predates it;
- both_branches_same_time: parts of both branches were bought at the same moment;
- completion_without_part (slots): the first distinguishing purchase was a route item itself;
- ambiguous_part: the first distinguishing part also feeds items outside its route and failed the gate.

Continuation-fidelity gate (slots): a distinguishing part that builds only into its own route is exclusive.
Any other part may assign only if, among the fold's general TRAIN player-stages whose first distinguishing
purchase it was and who then completed some item in that stage, at least fidelity_threshold completed its
route (and at least fidelity_min such player-stages exist). Completion enters only this population-level
gate on training games; it is never a row label or a decision feature, and the gate is still no proof of
individual route intent. A pair is rejected if a branch has no distinguishing part, or none that passes.

Boots: the boots stage has no observable distinguishing component purchase (boot upgrades are built from
basic boots plus parts that feed many items and appear in slot stages), so the branch is the upgraded-boot
purchase itself, scope 'boots_upgrade_purchase'. Boots comparisons therefore condition on buying an upgrade;
players who never upgrade are counted, not compared.

Tables: branch_comparison (fold, split_role, pair and branch columns, then the purchase_decisions columns
of the assigned row in its KEYS/CONTEXT/ACTION/OUTCOME groups), branch_pairs, branch_parts, branch_coverage,
branch_diagnostics (the share of branch buyers who later complete each route: descriptive only) and
branch_meta (provenance). Rows keep their fold role: markers were chosen on 'train' rows, so only
'validate' rows are free of that selection; 'train_focus' rows are focus-discovered sensitivity data.
The held-out group is left out unless --include-heldout. Inputs are opened read-only.
"""
import argparse
import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

from decisions import ACTION, COLUMNS, CONTEXT, KEYS, OUTCOME
from engine import ROOT

STAGES = ("slot1", "slot2", "slot3", "boots")
ROUTE_KINDS = {"completion", "boots_upgrade"}
PAIR_COLUMNS = ["fold", "split_role", "pair_id", "scope", "route_a", "route_b", "branch", "branch_route",
                "assigned_part", "part_exclusive", "part_train_fidelity"]
OUT_COLUMNS = PAIR_COLUMNS + COLUMNS
LIGHT = ["rowid", "match_id", "participant_id", "champion", "role", "patch", "stage", "t_ms",
         "action_item", "action_kind", "inventory_before", "completed_before"]


def on_rift(info):
    return info.get("maps", {}).get("11") is not False and info.get("gold", {}).get("purchasable", True)


def recipe_tree(iid, items):
    """Every component in iid's recipe, at any depth (not iid itself)."""
    out = set()
    for part in items.get(iid, {}).get("from", []):
        out |= {part} | recipe_tree(part, items)
    return out


def feeds(iid, items):
    """Every Summoner's Rift item iid builds into, at any depth."""
    out = set()
    for up in items.get(iid, {}).get("into", []):
        if up in items and on_rift(items[up]):
            out |= {up} | feeds(up, items)
    return out


def distinguishing_parts(a, b, items):
    """{part: (branch, exclusive)} of parts in exactly one route's tree; exclusive if it builds into nothing else."""
    tree_a, tree_b = recipe_tree(a, items), recipe_tree(b, items)
    out = {}
    for branch, route, own, other in (("a", a, tree_a, tree_b), ("b", b, tree_b, tree_a)):
        for part in own - other:
            out[part] = (branch, feeds(part, items) <= own | {route})
    return out


def player_stages(rows):
    """{(match_id, participant_id, stage): rows sorted by time}."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["match_id"], r["participant_id"], r["stage"])].append(r)
    for g in groups.values():
        g.sort(key=lambda r: (r["t_ms"], r["rowid"]))
    return groups


def stage_route(rows):
    """The route this stage ended in (its finished item or upgraded boots), else None. Selection and diagnostics only."""
    return next((r["action_item"] for r in rows if r["action_kind"] in ROUTE_KINDS), None)


def select_pairs(train_groups, min_support):
    """{(champion, role, stage): ((a, support), (b, support)) or reason} from TRAIN player-stages only."""
    counts = defaultdict(Counter)
    for (_, _, stage), rows in train_groups.items():
        route = stage_route(rows)
        if route:
            counts[(rows[0]["champion"], rows[0]["role"], stage)][route] += 1
    out = {}
    for key, c in counts.items():
        top = sorted(((n, i) for i, n in c.items() if n >= min_support), key=lambda x: (-x[0], x[1]))[:2]
        out[key] = ((top[0][1], top[0][0]), (top[1][1], top[1][0])) if len(top) == 2 else "fewer_than_two_supported_routes"
    return out


def markers(a, b, stage, items):
    """{item: (branch, kind, exclusive)} that reveal a branch; kind 'part', 'route' or 'boots_upgrade'."""
    if stage == "boots":
        return {a: ("a", "boots_upgrade", True), b: ("b", "boots_upgrade", True)}
    out = {p: (branch, "part", excl) for p, (branch, excl) in distinguishing_parts(a, b, items).items()}
    out.update({a: ("a", "route", True), b: ("b", "route", True)})
    return out


def first_branch(rows, marks, a, b):
    """(status, row) of one player-stage; status 'assigned' or an unassigned reason. Uses no later purchase.

    marks: {item: (branch, kind, usable)} with kind 'part', 'route' or 'boots_upgrade'."""
    if set(json.loads(rows[0]["completed_before"])) & {a, b}:
        return "route_already_completed", None
    hit = next((r for r in rows if r["action_item"] in marks), None)
    if hit is None:
        return "no_distinguishing_purchase", None
    branch, kind, usable = marks[hit["action_item"]]
    if set(json.loads(hit["inventory_before"])) & {i for i, m in marks.items() if m[1] == "part"}:
        return "carried_part", None
    if any(r["t_ms"] == hit["t_ms"] and r["action_item"] in marks and marks[r["action_item"]][0] != branch for r in rows):
        return "both_branches_same_time", None
    if kind == "route":
        return "completion_without_part", None
    return ("assigned" if usable else "ambiguous_part"), hit


def part_fidelity(train_groups, pair_marks):
    """{(key, patch, part): [completed its route, completed any item]} over TRAIN player-stages whose first
    distinguishing purchase was that part. Population-level gate input only."""
    out = defaultdict(lambda: [0, 0])
    for rows in train_groups.values():
        key, patch = (rows[0]["champion"], rows[0]["role"], rows[0]["stage"]), rows[0]["patch"]
        if (key, patch) not in pair_marks or key[2] == "boots":
            continue
        routes, marks = pair_marks[(key, patch)]
        hit = next((r for r in rows if r["action_item"] in marks), None)
        route = stage_route(rows)
        if hit is None or marks[hit["action_item"]][1] != "part" or route is None:
            continue
        counts = out[(key, patch, hit["action_item"])]
        counts[0] += route == routes[marks[hit["action_item"]][0]]
        counts[1] += 1
    return out


def load_catalogs(catalog_json=None, catalog_db=None):
    """({patch: items}, {patch: version}) from a Data Dragon item.json / {patch: items} file, or a catalog table."""
    if catalog_json:
        data = json.loads(Path(catalog_json).read_text(encoding="utf-8"))
        if "data" in data and "version" in data:
            patch = ".".join(data["version"].split(".")[:2])
            return {patch: data["data"]}, {patch: data["version"]}
        return data, {p: str(catalog_json) for p in data}
    db = sqlite3.connect(Path(catalog_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    found = db.execute("SELECT patch, version, items FROM catalog").fetchall()
    db.close()
    return {p: json.loads(i) for p, _, i in found}, {p: v for p, v, _ in found}


def build(decisions_path, out_path, catalogs, catalog_versions, min_support=30, fidelity_threshold=0.8,
          fidelity_min=10, include_heldout=False, log=print):
    src = sqlite3.connect(Path(decisions_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    src.row_factory = sqlite3.Row
    membership = defaultdict(dict)  # fold -> match_id -> split role
    for fold, role, mid in src.execute("SELECT fold, role, match_id FROM split_folds"):
        if fold != "heldout" or include_heldout:
            membership[fold][mid] = role
    stage_sql = ",".join("?" * len(STAGES))
    rows = [dict(r) for r in src.execute(f"SELECT {', '.join(LIGHT)} FROM purchase_decisions WHERE stage IN ({stage_sql})", STAGES)]
    groups = player_stages(rows)
    by_match = defaultdict(list)
    for k in groups:
        by_match[k[0]].append(k)

    key_patches = defaultdict(set)
    for g in groups.values():
        key_patches[(g[0]["champion"], g[0]["role"], g[0]["stage"])].add(g[0]["patch"])
    pairs_out, parts_out, coverage, diagnostics, assigned = [], [], Counter(), defaultdict(Counter), []
    for fold in sorted(membership):
        members = membership[fold]
        train = {k: groups[k] for mid, role in members.items() if role == "train" for k in by_match[mid]}
        pairs = select_pairs(train, min_support)
        pair_marks, pair_status = {}, {}
        for key, sel in sorted(pairs.items()):
            if isinstance(sel, str):
                pairs_out.append((fold, *key, None, None, None, None, None, None, sel))
                continue
            (a, sa), (b, sb) = sel
            scope = "boots_upgrade_purchase" if key[2] == "boots" else "first_distinguishing_component"
            for patch in sorted(key_patches[key]):
                items = catalogs.get(patch)
                if items is None or a not in items or b not in items:
                    status = "no_catalog" if items is None else "route_missing_in_patch"
                else:
                    pair_marks[(key, patch)] = ({"a": a, "b": b}, markers(a, b, key[2], items))
                    status = "accepted" if key[2] == "boots" else None
                pair_status[(key, patch)] = status
                if status:
                    pairs_out.append((fold, *key, patch, a, sa, b, sb, scope, status))
        fidelity = part_fidelity(train, pair_marks)
        gated = {}  # (key, patch) -> {item: (branch, kind, usable)}
        part_info = {}  # (key, patch, part) -> (exclusive, train fidelity)
        for (key, patch), (routes, marks) in sorted(pair_marks.items()):
            gated[(key, patch)] = {i: (branch, kind, True) for i, (branch, kind, _) in marks.items()}
            if key[2] == "boots":
                continue
            has, ok = {"a": False, "b": False}, {"a": False, "b": False}
            for item, (branch, kind, excl) in sorted(marks.items()):
                if kind != "part":
                    continue
                done, total = fidelity.get((key, patch, item), (0, 0))
                fid = done / total if total else None
                good = bool(excl or (total >= fidelity_min and fid >= fidelity_threshold))
                gated[(key, patch)][item] = (branch, kind, good)
                part_info[(key, patch, item)] = (int(excl), fid)
                has[branch], ok[branch] = True, ok[branch] or good
                parts_out.append((fold, *key, patch, item, branch, kind, int(excl), total, fid, int(good)))
            status = ("accepted" if all(ok.values()) else
                      "no_distinguishing_part" if not all(has.values()) else "no_part_passes_fidelity_gate")
            pair_status[(key, patch)] = status
            (_, sa), (_, sb) = pairs[key]
            pairs_out.append((fold, *key, patch, routes["a"], sa, routes["b"], sb, "first_distinguishing_component", status))
        for mid, split_role in members.items():
            for k in by_match[mid]:
                rows_k = groups[k]
                key, patch = (rows_k[0]["champion"], rows_k[0]["role"], k[2]), rows_k[0]["patch"]
                if pair_status.get((key, patch)) != "accepted":
                    coverage[(fold, split_role, k[2], "no_accepted_pair")] += 1
                    continue
                routes = pair_marks[(key, patch)][0]
                status, hit = first_branch(rows_k, gated[(key, patch)], routes["a"], routes["b"])
                coverage[(fold, split_role, k[2], status)] += 1
                if status != "assigned":
                    continue
                branch = gated[(key, patch)][hit["action_item"]][0]
                excl, fid = part_info.get((key, patch, hit["action_item"]), (1, None))
                pair_id = f"{fold}|{key[0]}|{key[1]}|{key[2]}|{patch}|{routes['a']}|{routes['b']}"
                assigned.append(dict(fold=fold, split_role=split_role, pair_id=pair_id,
                                     scope="boots_upgrade_purchase" if k[2] == "boots" else "first_distinguishing_component",
                                     route_a=routes["a"], route_b=routes["b"], branch=branch, branch_route=routes[branch],
                                     assigned_part=hit["action_item"], part_exclusive=excl, part_train_fidelity=fid,
                                     rowid=hit["rowid"]))
                # Descriptive only: this player's later route completions (any stage), never a label or feature.
                later = {r["action_item"] for kk in by_match[mid] if kk[1] == k[1] for r in groups[kk]
                         if r["action_kind"] in ROUTE_KINDS and r["t_ms"] >= hit["t_ms"]}
                d = diagnostics[(pair_id, split_role, branch)]
                d["buyers"] += 1
                d["completed_own"] += routes[branch] in later
                d["completed_other"] += routes["b" if branch == "a" else "a"] in later
                d["completed_neither"] += not later & {routes["a"], routes["b"]}

    out = sqlite3.connect(out_path, timeout=60)
    for table in ("branch_comparison", "branch_pairs", "branch_parts", "branch_coverage", "branch_diagnostics", "branch_meta"):
        out.execute(f"DROP TABLE IF EXISTS {table}")
    out.execute(f"CREATE TABLE branch_comparison ({', '.join(OUT_COLUMNS)})")
    out.execute("CREATE TABLE branch_pairs (fold, champion, role, stage, patch, route_a, support_a, route_b, support_b, scope, status)")
    out.execute("CREATE TABLE branch_parts (fold, champion, role, stage, patch, part, branch, kind, exclusive, "
                "train_first_buyers_completing, train_fidelity, usable)")
    out.execute("CREATE TABLE branch_coverage (fold, split_role, stage, status, player_stages)")
    out.execute("CREATE TABLE branch_diagnostics (pair_id, split_role, branch, buyers, completed_own, completed_other, completed_neither)")
    out.execute("CREATE TABLE branch_meta (key TEXT PRIMARY KEY, value TEXT)")
    for i in range(0, len(assigned), 500):
        chunk = assigned[i:i + 500]
        full = {r["rowid"]: r for r in src.execute(
            f"SELECT rowid, {', '.join(COLUMNS)} FROM purchase_decisions WHERE rowid IN ({','.join('?' * len(chunk))})",
            [a["rowid"] for a in chunk])}
        out.executemany(f"INSERT INTO branch_comparison VALUES ({','.join('?' * len(OUT_COLUMNS))})",
                        [[a[c] for c in PAIR_COLUMNS] + [full[a["rowid"]][c] for c in COLUMNS] for a in chunk])
    src.close()
    out.executemany("INSERT INTO branch_pairs VALUES (?,?,?,?,?,?,?,?,?,?,?)", pairs_out)
    out.executemany("INSERT INTO branch_parts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", parts_out)
    out.executemany("INSERT INTO branch_coverage VALUES (?,?,?,?,?)", [(*k, n) for k, n in sorted(coverage.items())])
    out.executemany("INSERT INTO branch_diagnostics VALUES (?,?,?,?,?,?,?)",
                    [(*k, d["buyers"], d["completed_own"], d["completed_other"], d["completed_neither"])
                     for k, d in sorted(diagnostics.items())])
    meta = dict(decisions=str(decisions_path), catalog_versions=catalog_versions, min_support=min_support,
                fidelity_threshold=fidelity_threshold, fidelity_min=fidelity_min, include_heldout=include_heldout,
                folds=sorted(membership), stages=STAGES, created_at=int(time.time()),
                column_groups=dict(pair=PAIR_COLUMNS, keys=KEYS, context=CONTEXT, action=ACTION, outcome=OUTCOME))
    out.executemany("INSERT INTO branch_meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in meta.items()])
    out.commit()
    out.close()
    log(f"{len(assigned):,} assigned player-stages -> {out_path}; "
        f"pairs {dict(Counter(p[-1] for p in pairs_out))}")
    return assigned, coverage, pairs_out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decisions", default=str(ROOT / "data" / "decisions.sqlite"))
    parser.add_argument("--out", default=str(ROOT / "data" / "branch-comparison.sqlite"))
    parser.add_argument("--catalog-json", help="Data Dragon item.json, or {patch: items}")
    parser.add_argument("--catalog-db", default=str(ROOT / "data" / "settistics.sqlite"),
                        help="database with a catalog table, read-only (used without --catalog-json)")
    parser.add_argument("--min-support", type=int, default=30, help="completing TRAIN player-stages each route needs")
    parser.add_argument("--fidelity-threshold", type=float, default=0.8)
    parser.add_argument("--fidelity-min", type=int, default=10, help="TRAIN first-buyers who completed an item")
    parser.add_argument("--include-heldout", action="store_true")
    args = parser.parse_args()
    if Path(args.out).resolve() in {Path(args.decisions).resolve(), Path(args.catalog_db).resolve()}:
        parser.error("--out must be a separate file")
    catalogs, versions = load_catalogs(args.catalog_json, None if args.catalog_json else args.catalog_db)
    build(args.decisions, args.out, catalogs, versions, args.min_support, args.fidelity_threshold,
          args.fidelity_min, args.include_heldout)


if __name__ == "__main__":
    main()
