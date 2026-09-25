"""Derive the per-profile DAB configs from the one authored config.

    dab/dab-config.json               authored: entities, fields, descriptions
      -> infra/generated/dab-config.poc.json      unchanged; anonymous read
      -> infra/generated/dab-config.secure.json   Entra ID JWT; authenticated read

The secure file is not just the PoC file with auth switched on. Under Entra ID a
request with no token still runs as 'anonymous', so any anonymous permission
left behind would be a hole. Every anonymous grant is rewritten to
'authenticated', and the build fails if one survives.

Run by the azd preprovision hook. Safe to run by hand:
    python scripts/build_profiles.py
"""

from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCE = os.path.join(ROOT, "dab", "dab-config.json")
OUT_DIR = os.path.join(ROOT, "infra", "generated")


def poc(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    out["runtime"]["host"]["authentication"] = {"provider": "Unauthenticated"}
    out["runtime"]["host"]["mode"] = "production"
    return out


def secure(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    host = out["runtime"]["host"]
    host["mode"] = "production"
    host["cors"] = {"origins": [], "allow-credentials": False}
    host["authentication"] = {
        "provider": "EntraID",
        "jwt": {
            "audience": "@env('DAB_JWT_AUDIENCE')",
            "issuer": "@env('DAB_JWT_ISSUER')",
        },
    }
    for entity in out.get("entities", {}).values():
        for perm in entity.get("permissions", []):
            if perm.get("role") == "anonymous":
                perm["role"] = "authenticated"
    leaked = [
        name for name, e in out.get("entities", {}).items()
        if any(p.get("role") == "anonymous" for p in e.get("permissions", []))
    ]
    if leaked:
        sys.exit(f"secure profile still grants anonymous access to: {', '.join(leaked)}")
    return out


def main() -> None:
    with open(SOURCE, encoding="utf-8") as fh:
        cfg = json.load(fh)
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, build in (("poc", poc), ("secure", secure)):
        path = os.path.join(OUT_DIR, f"dab-config.{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(build(cfg), fh, indent=2)
            fh.write("\n")
        print(f"wrote {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
