#!/usr/bin/env python3
"""Schreibt den Text für ein GitHub-Release (Changelog) aus den Commit-Nachrichten.

Jeder Commit seit der vorigen Version wird ein Punkt in der Liste:
    Summary (die erste Zeile in GitHub Desktop)   -> Listenpunkt
    Description (das Feld darunter, optional)      -> eingerückter Zusatztext
Gute Commit-Nachrichten ergeben also automatisch einen guten Changelog.

Aufruf (macht der GitHub-Workflow):
    python3 scripts/release_notes.py v0.1.3
Lokal ausprobieren, was im nächsten Release stünde:
    python3 scripts/release_notes.py v0.1.3 HEAD
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
RECORD, FIELD = "\x1e", "\x1f"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def version_key(tag: str) -> tuple[int, int, int]:
    m = VERSION_TAG.fullmatch(tag)
    return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)


def main() -> int:
    if len(sys.argv) < 2 or not VERSION_TAG.fullmatch(sys.argv[1]):
        print("Aufruf: python3 scripts/release_notes.py v0.1.3 [Commit]", file=sys.stderr)
        return 1
    tag = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else tag

    # Vorige Version: der höchste Versions-Tag unterhalb des aktuellen
    older = [t for t in git("tag", "--list", "v*").split() if VERSION_TAG.fullmatch(t)
             and version_key(t) < version_key(tag)]
    previous = max(older, key=version_key) if older else None
    span = f"{previous}..{target}" if previous else target

    commits = []
    for record in git("log", "--no-merges", f"--format=%h{FIELD}%s{FIELD}%b{RECORD}", span).split(RECORD):
        parts = record.strip("\n").split(FIELD)
        if len(parts) == 3 and parts[1].strip():
            commits.append((parts[0], parts[1].strip(), parts[2].strip()))

    repo = os.environ.get("GITHUB_REPOSITORY", "Serifenlos/Heuhaufen")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    version = tag[1:]

    lines = ["### Änderungen", ""]
    if not previous:
        lines += ["Erste Version mit Versionsnummer.", ""]
    for sha, summary, body in commits:
        lines.append(f"- {summary} ({sha})")
        for text in body.splitlines():
            if text.strip():
                lines.append(f"  {text.strip()}")
    if not commits:
        lines.append("- Keine neuen Commits, nur neu gebaut.")
    lines += [
        "",
        "### Docker-Image",
        "",
        f"`ghcr.io/{repo.lower()}:{version}`",
        "",
        "In Portainer den Stack aktualisieren und dabei das Image neu laden.",
    ]
    if previous:
        lines += ["", f"**Alle Änderungen:** {server}/{repo}/compare/{previous}...{tag}"]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
