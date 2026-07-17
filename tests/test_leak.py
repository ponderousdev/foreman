"""The leak test (#19): the runner seam must not leak runner names.

Graph, GitHub, eligibility — and every other policy-layer module — may vary
only by consuming advertised capabilities. Branching on which runner is
configured (`cfg.runner == ...`) belongs exclusively to the selection layer:
`foreman/runner/` (the registry + implementations), `config.py` (validates
the name), `capabilities.py` (the planning-time policy table that names
compatible runners in refusals), and `cli.py` (wiring).

This is a tripwire, not a proof: it greps source for the concrete leak
shapes the spec calls out. Keep the patterns tight so capability names
("docker" is both a runner name and a capability name) never false-positive.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "foreman"

# Modules allowed to know runner names (the selection/execution layer).
ALLOWED = {"cli.py", "config.py", "capabilities.py"}

LEAK_PATTERNS = [
    re.compile(r"\bcfg\.runner\b"),
    re.compile(r"\.runner\s*(?:==|!=)"),
    re.compile(r"""(?:==|!=)\s*["'](?:local|sprite|docker)["']"""),
    re.compile(r"""["'](?:local|sprite|docker)["']\s*(?:==|!=)"""),
]


class SeamDoesNotLeak(unittest.TestCase):
    def test_no_runner_name_branches_outside_the_selection_layer(self):
        offenders: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            rel = path.relative_to(SRC)
            if rel.parts[0] == "runner" or rel.name in ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern in LEAK_PATTERNS:
                    if pattern.search(line):
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "runner-name branch outside the selection layer (consume "
            "capabilities instead):\n" + "\n".join(offenders),
        )

    def test_the_scanned_set_is_not_empty(self):
        scanned = [
            p
            for p in SRC.rglob("*.py")
            if p.relative_to(SRC).parts[0] != "runner" and p.name not in ALLOWED
        ]
        self.assertGreater(len(scanned), 10)


if __name__ == "__main__":
    unittest.main()
