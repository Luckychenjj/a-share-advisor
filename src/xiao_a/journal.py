from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from xiao_a.models import AdvisorDecision


class DecisionJournal:
    """Append-only JSONL journal with point-in-time outcome filtering."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, decision: AdvisorDecision) -> None:
        payload = asdict(decision)
        payload["as_of"] = decision.as_of.isoformat()
        payload["raw_action"] = decision.raw_action.value
        payload["final_action"] = decision.final_action.value
        for signal in payload["strategy_signals"]:
            signal["action"] = signal["action"].value if hasattr(signal["action"], "value") else signal["action"]
        payload.update({"resolved_at": None, "realized_return": None, "benchmark_return": None})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def settled_as_of(self, cutoff: date | datetime) -> list[dict]:
        cutoff_date = cutoff.date() if isinstance(cutoff, datetime) else cutoff
        records = []
        for record in self.load():
            resolved = record.get("resolved_at")
            if resolved and date.fromisoformat(str(resolved)[:10]) <= cutoff_date:
                records.append(record)
        return records

    def settle(
        self,
        *,
        symbol: str,
        as_of: datetime,
        resolved_at: date,
        realized_return: float,
        benchmark_return: float,
    ) -> bool:
        records = self.load()
        updated = False
        for record in records:
            if record.get("symbol") == symbol and record.get("as_of") == as_of.isoformat():
                record["resolved_at"] = resolved_at.isoformat()
                record["realized_return"] = realized_return
                record["benchmark_return"] = benchmark_return
                updated = True
                break
        if not updated:
            return False
        with NamedTemporaryFile("w", delete=False, dir=self.path.parent, encoding="utf-8") as temp:
            for record in records:
                temp.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            temp_path = Path(temp.name)
        os.replace(temp_path, self.path)
        return True

