"""Run every preset and every guardrail against the live app.

Asserts each preset returns exactly the row count advertised on its button -
"complete result set" has to mean complete, or the label is lying on stage.
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8000")


def stream(url: str, payload: dict) -> list[dict]:
    events: list[dict] = []
    with httpx.stream("POST", BASE + url, json=payload, timeout=240.0) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def main() -> int:
    cfg = httpx.get(BASE + "/api/presets", timeout=30).json()
    failures: list[str] = []

    print("=" * 78)
    print("PRESETS - expecting complete result sets")
    print("=" * 78)
    for p in cfg["presets"]:
        ev = stream("/api/stream", {"question": p["question"]})
        rows = next((e["rows"] for e in ev if e.get("t") == "rows"), [])
        done = next((e for e in ev if e.get("t") == "done"), None)
        err = next((e for e in ev if e.get("t") == "error"), None)
        tokens = sum(1 for e in ev if e.get("t") == "token")
        n = len(rows)
        ok = (n == p["rows"]) and not err
        flag = "PASS" if ok else "FAIL"
        secs = f"{done['elapsed_s']:.2f}s" if done else "  -  "
        print(f"  [{flag}] {p['label']:30s} {n:>5,}/{p['rows']:<5,} {secs:>7}  tok={tokens}")
        if err:
            print(f"         error: {err['message'][:110]}")
        if not ok:
            failures.append(f"{p['label']}: got {n}, expected {p['rows']}")

    print()
    print("=" * 78)
    print("GUARDRAILS - every one must be rejected by DAB, with no rows returned")
    print("=" * 78)
    for g in cfg["guardrails"]:
        ev = stream("/api/guardrail", {"tool": g["tool"], "args": g["args"]})
        blocked = next((e for e in ev if e.get("t") == "blocked"), None)
        unexpected = any(e.get("t") == "done" and e.get("unexpected") for e in ev)
        ran = any(
            e.get("t") == "step" and e.get("id") == "execute" and e.get("state") == "ok"
            for e in ev
        )
        ok = bool(blocked) and not unexpected and not ran
        print(f"  [{'PASS' if ok else 'FAIL'}] {g['label']:30s} "
              f"{'rejected' if blocked else 'NOT REJECTED'}")
        if blocked:
            print(f"         {blocked.get('error_type', '')}: {blocked['message'][:90]}")
        if not ok:
            failures.append(f"guardrail not rejected: {g['label']}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all presets complete, all guardrails rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
