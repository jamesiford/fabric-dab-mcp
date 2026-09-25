"""Turn benchmark output (out/bench.json, written by bench.py) into out/latency.png.

Both lanes are measured on the same machine against the same live Lakehouse:
the Fabric data agent over its native MCP endpoint, and a DAB tool call over MCP.
"""

from __future__ import annotations

import json
import os
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "out", "bench.json")
CHART = os.path.join(HERE, "out", "latency.png")

INK = "#1b1b1f"
MEASURED_C = "#0f7b6c"
AGENT_C = "#8b2f5f"


def main() -> None:
    with open(BENCH, encoding="utf-8") as fh:
        rows = json.load(fh)

    mcp_times = [r["mcp_s"] for r in rows]
    mcp_med = statistics.median(mcp_times)
    mcp_min, mcp_max = min(mcp_times), max(mcp_times)

    done = [r for r in rows if r.get("agent_status") == "completed"]
    agent_times = [r["agent_s"] for r in done]
    agent_med = statistics.median(agent_times) if agent_times else None

    fig, ax = plt.subplots(figsize=(11, 4.2))

    labels, lows, highs, colors = [], [], [], []

    if agent_times:
        labels.append("Data agent via MCP\n(measured here)")
        lows.append(min(agent_times))
        highs.append(max(agent_times))
        colors.append(AGENT_C)

    labels.append("DAB tool call via MCP\n(measured here)")
    lows.append(mcp_min)
    highs.append(mcp_max)
    colors.append(MEASURED_C)

    ypos = range(len(labels))
    for y, lo, hi, c in zip(ypos, lows, highs, colors):
        ax.barh(y, hi - lo, left=lo, height=0.5, color=c, alpha=0.85,
                edgecolor=c, linewidth=0)
        txt = f"{lo:.1f}-{hi:.1f}s" if lo >= 1 else f"{lo*1000:.0f}-{hi*1000:.0f} ms"
        ax.text(hi * 1.12, y, txt, va="center", ha="left",
                fontsize=10.5, color=c, fontweight="bold")

    ax.set_yticks(list(ypos))
    ax.set_yticklabels(labels, fontsize=10.5, color=INK)
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlim(0.1, 200)
    ax.set_xlabel("Time to answer a single analytical question  (seconds, log scale)",
                  fontsize=10.5, color=INK)
    ax.set_xticks([0.1, 0.5, 1, 5, 10, 30, 60, 120])
    ax.set_xticklabels(["0.1", "0.5", "1", "5", "10", "30", "60", "120"])
    ax.grid(axis="x", alpha=0.25, linestyle=":")
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)

    ax.set_title(
        "Reading from Fabric: data agent vs Data API Builder, both over MCP",
        fontsize=14.5, fontweight="bold", color=INK, pad=34, loc="left",
    )

    if agent_med:
        subtitle = (
            f"Measured head-to-head, {len(rows)} questions x 3 repeats, one F2 capacity, "
            f"8,123 accounts.\nData agent median {agent_med:.1f}s vs MCP tool call "
            f"{mcp_med*1000:.0f} ms - {agent_med/mcp_med:.0f}x."
        )
    else:
        subtitle = (
            f"Median MCP tool call {mcp_med*1000:.0f} ms over {len(rows)} questions, "
            "live lh_deposits SQL analytics endpoint (F2 capacity, 8,123 accounts)."
        )
    ax.annotate(subtitle, xy=(0, 1.02), xycoords="axes fraction",
                fontsize=9.5, color="#55555f", va="bottom")

    handles = [Patch(facecolor=MEASURED_C, label="DAB tool call - measured here")]
    if agent_times:
        handles.append(Patch(facecolor=AGENT_C, label="Fabric data agent - measured here"))
    ax.legend(handles=handles, loc="lower right", frameon=False, fontsize=9.5)

    fig.tight_layout()
    fig.savefig(CHART, dpi=170, facecolor="white")
    print(f"wrote {CHART}")
    print(f"measured MCP       : median {mcp_med*1000:.0f} ms, "
          f"range {mcp_min*1000:.0f}-{mcp_max*1000:.0f} ms")
    if agent_med:
        print(f"measured data agent: median {agent_med:.2f} s, "
              f"range {min(agent_times):.2f}-{max(agent_times):.2f} s "
              f"({len(done)}/{len(rows)} questions completed)")
        print(f"speed-up           : {agent_med/mcp_med:.0f}x")


if __name__ == "__main__":
    main()
