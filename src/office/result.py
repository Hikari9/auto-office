"""Command results: terse lines, one `next:` action, optional structured data."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Result:
    lines: list[str] = field(default_factory=list)
    next: str | None = None
    data: dict = field(default_factory=dict)
    verbose: list[str] = field(default_factory=list)
    exit_code: int = 0
    notices: list[str] = field(default_factory=list)

    def add(self, line: str) -> "Result":
        self.lines.append(line)
        return self
