#!/usr/bin/env python3
"""Bestimmt die Versionsnummer für den nächsten Build.

Aufbau: 0.1.3
  0.1  steht in der Datei VERSION im Hauptordner. Das ist die einzige Stelle,
       an der die Versionsnummer von Hand eingestellt wird.
  3    zählt GitHub bei jedem Push automatisch hoch: 0.1.0, 0.1.1, 0.1.2 …
       Steht in VERSION eine neue Nummer (z. B. 0.2), beginnt die Zählung bei 0.

Grundlage sind die Git-Tags (v0.1.0, v0.1.1 …), die der GitHub-Workflow nach jedem
erfolgreichen Build setzt. Hat der aktuelle Commit schon einen Versions-Tag, weil der
Workflow erneut gestartet wurde, bleibt es bei dieser Nummer.

Lokal aufrufen, um zu sehen, welche Version der nächste Push bekäme:
    python3 scripts/version.py

Ausgabe (so liest sie der Workflow über $GITHUB_OUTPUT):
    version=0.1.3
    base=0.1
    tag=v0.1.3
    new=true
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout.split()


def main() -> int:
    base = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+", base):
        print(f"In der Datei VERSION müssen zwei Zahlen stehen, zum Beispiel 0.1 (gefunden: {base!r}).",
              file=sys.stderr)
        return 1

    tag_pattern = re.compile(rf"v{re.escape(base)}\.(\d+)")

    def patch_numbers(tags: list[str]) -> list[int]:
        return [int(m.group(1)) for t in tags if (m := tag_pattern.fullmatch(t))]

    on_this_commit = patch_numbers(git_lines("tag", "--points-at", "HEAD"))
    if on_this_commit:
        patch, new = max(on_this_commit), False
    else:
        existing = patch_numbers(git_lines("tag", "--list", f"v{base}.*"))
        patch, new = (max(existing) + 1 if existing else 0), True

    version = f"{base}.{patch}"
    print(f"version={version}")
    print(f"base={base}")
    print(f"tag=v{version}")
    print(f"new={'true' if new else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
