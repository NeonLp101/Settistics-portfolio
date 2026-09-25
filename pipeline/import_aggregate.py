"""Validate a licensed aggregate JSON/CSV export for Settistics. No web scraping."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
ROLES = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}
KINDS = {"packages", "items", "runes", "spells"}

def integer(value, name):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result

def dimensions(row):
    values = {key: str(row.get(key, "")).strip() for key in ("champion", "opponent", "role", "patch", "region")}
    if not values["champion"] or not values["opponent"]:
        raise ValueError("Every bucket needs champion and opponent")
    values["role"] = values["role"].upper()
    values["region"] = values["region"].upper()
    if values["role"] not in ROLES:
        raise ValueError(f"Unsupported role: {values['role']}")
    if not re.fullmatch(r"\d+\.\d+", values["patch"]):
        raise ValueError(f"Patch must look like 16.18: {values['patch']}")
    if not values["region"]:
        raise ValueError("Every bucket needs a region such as EUW1 or GLOBAL")
    return values

def normalize_bucket(raw):
    bucket = dimensions(raw)
    bucket["games"] = integer(raw.get("games"), "games")
    bucket["wins"] = integer(raw.get("wins"), "wins")
    if bucket["wins"] > bucket["games"]:
        raise ValueError("wins cannot exceed games")
    eligible = raw.get("eligible") or {}
    bucket["eligible"] = {kind: integer(eligible.get(kind, 0), f"eligible.{kind}") for kind in KINDS}
    bucket["choices"] = []
    seen = set()
    for raw_choice in raw.get("choices") or []:
        kind = str(raw_choice.get("kind", "")).strip()
        ident = str(raw_choice.get("id", "")).strip()
        if kind not in KINDS or not ident:
            raise ValueError("Every choice needs a supported kind and non-empty id")
        key = (kind, ident)
        if key in seen:
            raise ValueError(f"Duplicate choice: {kind}:{ident}")
        seen.add(key)
        games = integer(raw_choice.get("games"), "choice.games")
        wins = integer(raw_choice.get("wins"), "choice.wins")
        time_sum = float(raw_choice.get("timeSum", 0) or 0)
        time_count = integer(raw_choice.get("timeCount", 0), "choice.timeCount")
        if wins > games or games > bucket["eligible"][kind] or time_sum < 0 or time_count > games:
            raise ValueError(f"Invalid counts for {kind}:{ident}")
        bucket["choices"].append({"kind":kind,"id":ident,"label":str(raw_choice.get("label") or ident),
                                  "games":games,"wins":wins,"timeSum":time_sum,"timeCount":time_count})
    return bucket

def from_csv(path):
    grouped = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            dims = dimensions(row)
            key = tuple(dims.values())
            counts = (integer(row.get("games"), "games"), integer(row.get("wins"), "wins"))
            bucket = grouped.setdefault(key, {**dims,"games":counts[0],"wins":counts[1],
                "eligible":{kind:integer(row.get("eligible"+kind.title(),0) or 0,f"eligible.{kind}") for kind in KINDS},"choices":[]})
            if (bucket["games"],bucket["wins"]) != counts:
                raise ValueError("Repeated CSV bucket rows must use identical games/wins")
            kind = str(row.get("kind","")).strip()
            if kind:
                bucket["choices"].append({"kind":kind,"id":row.get("id"),"label":row.get("label"),
                    "games":row.get("choiceGames"),"wins":row.get("choiceWins"),
                    "timeSum":row.get("timeSum",0),"timeCount":row.get("timeCount",0)})
    return list(grouped.values())

def load(path):
    if path.suffix.lower()==".csv":
        return from_csv(path)
    value=json.loads(path.read_text(encoding="utf-8"))
    return value.get("buckets") if isinstance(value,dict) else value

def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(value,separators=(",",":"),allow_nan=False),encoding="utf-8")
    temp.replace(path)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",required=True,type=Path)
    parser.add_argument("--provider-id",required=True,help="Stable lowercase id, e.g. licensed-provider")
    parser.add_argument("--provider-name",required=True)
    parser.add_argument("--source-url",required=True)
    parser.add_argument("--obtained-at",default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--note",default="Imported aggregate counts; underlying matches and timelines are unavailable.")
    parser.add_argument("--rights-confirmed",action="store_true",help="Required: you have permission to reuse this dataset")
    parser.add_argument("--output",type=Path)
    args=parser.parse_args(argv)
    if not args.rights_confirmed:
        parser.error("--rights-confirmed is required; Settistics does not scrape third-party sites")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}",args.provider_id):
        parser.error("--provider-id must be 2-64 lowercase letters, numbers or hyphens")
    raw=load(args.input)
    if not isinstance(raw,list):
        raise ValueError("Input must be a bucket array or an object with buckets")
    buckets=[normalize_bucket(bucket) for bucket in raw]
    keys=[tuple(bucket[k] for k in ("champion","opponent","role","patch","region")) for bucket in buckets]
    if len(keys)!=len(set(keys)):
        raise ValueError("Duplicate champion/opponent/role/patch/region bucket")
    output=args.output or ROOT/"data"/"imports"/(args.provider_id+".json")
    payload={"importSchemaVersion":1,"rightsConfirmed":True,"obtainedAt":args.obtained_at,
             "provider":{"id":args.provider_id,"name":args.provider_name,"sourceUrl":args.source_url,"note":args.note},
             "buckets":buckets}
    write(output,payload)
    print(f"Validated {len(buckets)} aggregate buckets into {output}. WPA remains unavailable for this source.")

if __name__=="__main__":
    try:main()
    except (OSError,ValueError,json.JSONDecodeError) as error:raise SystemExit(str(error))
