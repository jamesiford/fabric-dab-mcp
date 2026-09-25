"""Generate a Data API Builder config from a Fabric Lakehouse SQL endpoint.

Reads INFORMATION_SCHEMA for the objects you name and writes a dab-config.json
with one entity per object, ready for `dab validate` and `dab start`.

Why this exists rather than `dab add` alone:
  - Lakehouse Delta tables carry no primary-key metadata, so DAB cannot find a
    key by itself. This script tests candidate columns for uniqueness.
  - `dab update --fields.description` splits on commas, so a description such as
    "One of: USD, CNY, HKD" is silently saved as "One of: USD". Entity blocks are
    written as JSON here to avoid that.
  - Descriptions are what the model reads. Facts that can be measured (the
    distinct values of a low-cardinality column) are filled in; everything that
    needs judgement is left as a TODO for a human.

Usage:
    python scripts/generate_dab_config.py --object dbo.vw_deposits --object dbo.vw_maturity_ladder
    python scripts/generate_dab_config.py --schema dbo --views-only
    python scripts/generate_dab_config.py --object dbo.vw_customer_exposure \\
        --key dbo.vw_customer_exposure=customer_id,currency,product_code
    python scripts/generate_dab_config.py --object dbo.vw_deposits --merge dab/dab-config.json

--merge keeps every description already written in the existing config and only
adds what is new, so it is safe to re-run after a schema change.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from fabric_sql import FabricSqlClient  # noqa: E402

TODO = "TODO: describe this"

# Columns whose names suggest an identifier are tried first when inferring a key.
_KEY_HINT = re.compile(r"(^id$|_id$|_number$|_no$|_key$|_code$)", re.IGNORECASE)
_STRING_TYPES = {"varchar", "nvarchar", "char", "nchar"}
# A float sum can be unique across every row by coincidence; that is not an identity.
_KEY_TYPES = _STRING_TYPES | {"int", "bigint", "smallint", "tinyint", "uniqueidentifier"}

# Read-only MCP surface. Write tools are off, so they are never advertised.
READ_ONLY_TOOLS = {
    "describe-entities": True,
    "read-records": True,
    "aggregate-records": {"enabled": True, "query-timeout": 30},
    "create-record": False,
    "update-record": False,
    "delete-record": False,
    "execute-entity": False,
}


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def entity_name(obj: str) -> str:
    bare = obj.split(".")[-1]
    bare = re.sub(r"^(vw|v|tbl)_", "", bare, flags=re.IGNORECASE)
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[_\W]+", bare) if part)


def inventory(client: FabricSqlClient) -> dict[str, dict]:
    """Every table and view the connection can see, keyed by schema.name."""
    rows = client.query(
        "SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE, c.COLUMN_NAME, c.DATA_TYPE, c.ORDINAL_POSITION "
        "FROM INFORMATION_SCHEMA.TABLES t "
        "JOIN INFORMATION_SCHEMA.COLUMNS c "
        "  ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME "
        "WHERE t.TABLE_SCHEMA NOT IN ('sys', 'queryinsights', 'INFORMATION_SCHEMA') "
        "ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME, c.ORDINAL_POSITION"
    ).rows
    objects: dict[str, dict] = {}
    for schema, name, ttype, col, dtype, _ in rows:
        key = f"{schema}.{name}"
        obj = objects.setdefault(key, {
            "schema": schema, "name": name,
            "type": "view" if ttype == "VIEW" else "table",
            "columns": [],
        })
        obj["columns"].append({"name": col, "type": dtype.lower()})
    return objects


def profile(client: FabricSqlClient, obj: dict, max_values: int) -> dict:
    """Row count, per-column distinct counts, and the values of small string columns."""
    target = f"{q(obj['schema'])}.{q(obj['name'])}"
    cols = obj["columns"]
    selects = ", ".join(f"COUNT(DISTINCT {q(c['name'])})" for c in cols)
    counts = client.query(f"SELECT COUNT(*), {selects} FROM {target}").rows[0]
    total = int(counts[0])
    distinct = {c["name"]: int(n) for c, n in zip(cols, counts[1:])}

    values: dict[str, list[str]] = {}
    for c in cols:
        n = distinct[c["name"]]
        if c["type"] in _STRING_TYPES and 0 < n <= max_values:
            rows = client.query(
                f"SELECT DISTINCT {q(c['name'])} FROM {target} "
                f"WHERE {q(c['name'])} IS NOT NULL ORDER BY {q(c['name'])}"
            ).rows
            values[c["name"]] = [str(r[0]) for r in rows]
    return {"rows": total, "distinct": distinct, "values": values}


def infer_key(obj: dict, stats: dict) -> list[str] | None:
    """First identifier-typed column whose distinct count equals the row count."""
    cols = sorted(
        (c for c in obj["columns"] if c["type"] in _KEY_TYPES),
        key=lambda c: (0 if _KEY_HINT.search(c["name"]) else 1),
    )
    for c in cols:
        if stats["rows"] and stats["distinct"][c["name"]] == stats["rows"]:
            return [c["name"]]
    return None


def key_is_unique(client: FabricSqlClient, obj: dict, keys: list[str], rows: int) -> bool:
    target = f"{q(obj['schema'])}.{q(obj['name'])}"
    cols = ", ".join(q(k) for k in keys)
    distinct = client.query(f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {target}) d").rows[0][0]
    return int(distinct) == rows


def measured_facts(col: dict, stats: dict) -> str:
    vals = stats["values"].get(col["name"])
    if vals is None:
        return ""
    if len(vals) == 1:
        return f"Always '{vals[0]}' in the current data, so grouping by it returns one row."
    return f"Values: {', '.join(vals)}."


def build_entity(obj: dict, stats: dict, keys: list[str], previous: dict | None) -> dict:
    prev_fields = {f["name"]: f for f in (previous or {}).get("fields", [])}
    fields = []
    for col in obj["columns"]:
        old = prev_fields.get(col["name"])
        if old and old.get("description") and not old["description"].startswith("TODO"):
            description = old["description"]
        else:
            facts = measured_facts(col, stats)
            description = f"{TODO}. {facts}".strip() if facts else TODO
        fields.append({
            "name": col["name"],
            "primary-key": col["name"] in keys,
            "description": description,
        })

    prev_desc = (previous or {}).get("description")
    return {
        "description": prev_desc if prev_desc and not prev_desc.startswith("TODO")
        else f"{TODO}: what one row of {obj['schema']}.{obj['name']} represents, "
             f"and when a model should use it. {stats['rows']:,} rows.",
        "source": {"object": f"{obj['schema']}.{obj['name']}", "type": obj["type"]},
        "rest": {"enabled": False},
        "graphql": {"enabled": False},
        "fields": fields,
        "permissions": (previous or {}).get("permissions")
        or [{"role": "anonymous", "actions": [{"action": "read"}]}],
    }


def scaffold_runtime() -> dict:
    """Runtime section from the installed DAB CLI, so it matches that version."""
    if not shutil.which("dab"):
        sys.exit("The 'dab' CLI is not on PATH: dotnet tool install -g Microsoft.DataApiBuilder")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["dab", "init", "--database-type", "mssql",
             "--connection-string", "@env('FABRIC_CONN')",
             "--host-mode", "production", "--auth.provider", "Unauthenticated",
             "--rest.enabled", "false", "--graphql.enabled", "false",
             "--mcp.enabled", "true"],
            cwd=tmp, check=True, capture_output=True, text=True,
        )
        with open(os.path.join(tmp, "dab-config.json"), encoding="utf-8") as fh:
            return json.load(fh)


def harden(cfg: dict) -> None:
    """Settings this repo relies on, applied whether the base is new or merged."""
    runtime = cfg.setdefault("runtime", {})
    runtime.setdefault("rest", {})["enabled"] = False
    runtime.setdefault("graphql", {})["enabled"] = False
    # Without this DAB pages at 100 rows and a "complete" read returns TOP 101.
    runtime["pagination"] = {"max-page-size": 100000, "default-page-size": 100000}
    mcp = runtime.setdefault("mcp", {})
    mcp["enabled"] = True
    mcp.setdefault("path", "/mcp")
    mcp["dml-tools"] = READ_ONLY_TOOLS
    cfg.setdefault("data-source", {}).setdefault("options", {})["set-session-context"] = True


def resolve_objects(args, objects: dict[str, dict]) -> list[str]:
    if args.object:
        missing = [o for o in args.object if o not in objects]
        if missing:
            sys.exit(f"Not found on this endpoint: {', '.join(missing)}. "
                     f"Available: {', '.join(sorted(objects))}")
        return args.object
    picked = [k for k, o in objects.items()
              if o["schema"] == args.schema and (o["type"] == "view" or not args.views_only)]
    if not picked:
        sys.exit(f"No objects in schema '{args.schema}'.")
    return picked


def parse_keys(pairs: list[str]) -> dict[str, list[str]]:
    out = {}
    for pair in pairs or []:
        obj, _, cols = pair.partition("=")
        if not cols:
            sys.exit(f"--key expects schema.object=col1,col2 - got '{pair}'")
        out[obj] = [c.strip() for c in cols.split(",") if c.strip()]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pick = ap.add_mutually_exclusive_group(required=True)
    pick.add_argument("--object", action="append", help="schema.name to publish; repeatable")
    pick.add_argument("--schema", help="publish every object in this schema")
    ap.add_argument("--views-only", action="store_true", help="with --schema, skip base tables")
    ap.add_argument("--key", action="append", help="schema.object=col1,col2 - overrides key inference")
    ap.add_argument("--merge", help="existing config to extend; its descriptions are kept")
    ap.add_argument("--out", default=os.path.join(ROOT, "dab", "dab-config.json"))
    ap.add_argument("--max-values", type=int, default=12,
                    help="list distinct values for string columns with at most this many")
    ap.add_argument("--no-values", action="store_true",
                    help="never copy real data values into descriptions")
    args = ap.parse_args()
    if args.no_values:
        args.max_values = 0

    client = FabricSqlClient.from_env()
    objects = inventory(client)
    selected = resolve_objects(args, objects)
    explicit_keys = parse_keys(args.key)
    for obj in explicit_keys:
        if obj not in selected:
            sys.exit(f"--key names '{obj}', which is not being published.")

    if args.merge:
        with open(args.merge, encoding="utf-8") as fh:
            cfg = json.load(fh)
    else:
        cfg = scaffold_runtime()
    harden(cfg)
    entities = cfg.setdefault("entities", {})
    by_source = {e.get("source", {}).get("object"): n for n, e in entities.items()}

    todo_count = 0
    embedded: list[str] = []
    for name in selected:
        obj = objects[name]
        print(f"profiling {name} ...", flush=True)
        stats = profile(client, obj, args.max_values)

        column_names = {c["name"] for c in obj["columns"]}
        keys = explicit_keys.get(name)
        if keys:
            bad = [k for k in keys if k not in column_names]
            if bad:
                sys.exit(f"{name}: key column(s) not found: {', '.join(bad)}")
            if not key_is_unique(client, obj, keys, stats["rows"]):
                sys.exit(f"{name}: ({', '.join(keys)}) is not unique across "
                         f"{stats['rows']:,} rows, so it cannot be the key.")
        else:
            keys = infer_key(obj, stats)
        if not keys:
            sys.exit(
                f"{name}: no identifier column is unique across {stats['rows']:,} rows, so "
                f"the key is probably composite. Pass it explicitly, e.g.\n"
                f"    --key {name}=col_a,col_b"
            )

        ent_name = by_source.get(name) or entity_name(name)
        previous = entities.get(ent_name)
        entity = build_entity(obj, stats, keys, previous)
        entities[ent_name] = entity

        stale = sorted({f["name"] for f in (previous or {}).get("fields", [])} - column_names)
        todos = sum(1 for f in entity["fields"] if f["description"].startswith(TODO))
        todos += entity["description"].startswith(TODO)
        todo_count += todos
        embedded += [f"{ent_name}.{f['name']}" for f in entity["fields"]
                     if f["description"].startswith(TODO) and "Values:" in f["description"]]
        print(f"  entity {ent_name}: {len(entity['fields'])} fields, key {keys}, "
              f"{stats['rows']:,} rows, {todos} TODO")
        if stale:
            print(f"  WARNING: config lists fields no longer in {name}: {', '.join(stale)}")

    client.close()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")

    print(f"\nwrote {args.out}")
    if embedded:
        print(f"\nREVIEW: real data values were copied into {len(embedded)} description(s):")
        print("  " + ", ".join(embedded))
        print("  describe_entities returns descriptions to every caller that can reach the")
        print("  endpoint. Remove any value that is sensitive, or re-run with --no-values.")
    if todo_count:
        print(f"{todo_count} description(s) still say TODO. These are what the model reads -")
        print("replace them before you deploy, then run:  dab validate -c " + os.path.relpath(args.out))
    else:
        print("no TODOs left. Next:  dab validate -c " + os.path.relpath(args.out))


if __name__ == "__main__":
    main()
