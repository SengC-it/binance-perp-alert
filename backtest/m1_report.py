"""M1 report template that keeps failed gates and data warnings first."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def render_m1_report(result: Mapping[str, Any] | None = None) -> str:
    """Render a skeleton now, or a result with the decision before good news."""
    if result is None:
        return (
            "# M1-A REPORT TEMPLATE\n\n"
            "M1-A PROTOCOL READY\n\n"
            "No formal M1 historical backtest was run.\n"
            "No formal M1 return, Sharpe, bootstrap, or gate result exists.\n\n"
            "## Required M1-B section order\n\n"
            "1. Executive Decision\n"
            "2. Failed Gates\n"
            "3. Data Integrity\n"
            "4. Point-in-Time Universe\n"
            "5. Delisted Symbol Coverage\n"
            "6. External Validation\n"
            "7. 2020–2021 Bull Death Test\n"
            "8. Year-by-Year\n"
            "9. Long/Short Attribution\n"
            "10. Funding & Costs\n"
            "11. 2x / 3x Cost Stress\n"
            "12. Block Bootstrap\n"
            "13. Leave-One-Out\n"
            "14. Symbol Concentration\n"
            "15. Regime Attribution\n"
            "16. Shadow Comparison\n"
            "17. Discovery Window Reference\n"
            "18. Limitations\n"
            "19. Final Decision\n"
        )
    decision = str(result.get("decision", "FAIL"))
    failed = result.get("failed_gates", ())
    lines = [f"# M1 {decision}", "", "## Failed Gates"]
    lines.extend(f"- {gate}" for gate in failed)
    lines.extend(["", "## Machine-readable result", "", "```json", json.dumps(result, ensure_ascii=False, indent=2, default=str), "```"])
    return "\n".join(lines) + "\n"


def write_m1_report(path: str | Path, result: Mapping[str, Any] | None = None) -> None:
    """Write a report only when a future caller explicitly requests it."""
    Path(path).write_text(render_m1_report(result), encoding="utf-8")
