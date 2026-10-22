#!/usr/bin/env python3
"""Prüft, ob der build-Ordner zum source-Ordner passt.

Läuft automatisch bei GitHub vor dem Bau des Docker-Images und bricht ab, wenn
CodeKit nach einer Änderung nicht neu gebaut hat. Lokal aufrufen:

    python3 scripts/check_build.py

Regeln:
* Dateien, die CodeKit nur kopiert (Python, Templates …), müssen in build/ identisch sein.
* Dateien, die CodeKit verarbeitet, müssen in build/ vorhanden sein:
    app.css -> app-min.css   (Tailwind, verkleinert)
    app.js  -> app-min.js    (verkleinert)
    *.svg   -> gleicher Name (optimiert)
  Das CSS darf keine Tailwind-Anweisungen mehr enthalten, sonst wurde es nicht übersetzt.
* Dateinamen mit _ am Anfang überspringt CodeKit (gilt als Teildatei) – in source/ also vermeiden.
* In build/app darf nichts liegen, was es in source/app nicht mehr gibt.
* Außerhalb von build/app darf in build/ nichts liegen (Projektdateien gehören nicht ins Image).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "source"
BUILD = ROOT / "build"

PROCESSED = {".css": "-min.css", ".js": "-min.js", ".svg": ".svg"}   # Endung -> Endung in build/
IGNORED_NAMES = {".DS_Store"}
IGNORED_DIRS = {"__pycache__", "node_modules"}
TAILWIND_MARKERS = ('@import "tailwindcss"', "@source ", "@theme", "@custom-variant")


def files(base: Path) -> set[Path]:
    found = set()
    for path in base.rglob("*"):
        rel = path.relative_to(base)
        if path.is_dir() or path.name in IGNORED_NAMES or IGNORED_DIRS & set(rel.parts):
            continue
        found.add(rel)
    return found


def build_name(rel: Path) -> Path:
    """Name, unter dem CodeKit eine Datei aus source/ in build/ ablegt."""
    if rel.suffix in PROCESSED:
        return rel.with_name(rel.stem + PROCESSED[rel.suffix])
    return rel


def main() -> int:
    if not BUILD.is_dir():
        print("Der Ordner build/ fehlt. In CodeKit einmal das ganze Projekt bauen und build/ committen.")
        return 1

    src, out = files(SOURCE), files(BUILD / "app")
    out = {Path("app") / rel for rel in out}
    problems: list[str] = []

    for rel in sorted(src):
        if rel.name.startswith("_"):
            problems.append(f"beginnt mit _ (CodeKit überspringt die Datei, bitte umbenennen): source/{rel}")
            continue
        built = build_name(rel)
        target = BUILD / built
        if built not in out:
            problems.append(f"fehlt in build/:             {built}")
        elif rel.suffix in PROCESSED:
            if rel.suffix == ".css":
                text = target.read_text(encoding="utf-8", errors="replace")
                if any(marker in text for marker in TAILWIND_MARKERS):
                    problems.append(f"CSS nicht übersetzt:         {built}")
        elif target.read_bytes() != (SOURCE / rel).read_bytes():
            problems.append(f"in build/ veraltet:          {built}")

    for rel in sorted(out - {build_name(r) for r in src}):
        problems.append(f"in source/ gelöscht, aber noch in build/: {rel}")

    for entry in sorted(BUILD.iterdir()):
        if entry.name == ".gitignore":
            problems.append("build/.gitignore schließt build/ von Git aus (CodeKit-Option „Build-Ordner in Git ignorieren“ ausschalten)")
        elif entry.name not in ("app", ".DS_Store"):
            problems.append(f"gehört nicht in build/ (in CodeKit ignorieren): {entry.name}{'/' if entry.is_dir() else ''}")

    if problems:
        print("build/ passt nicht zu source/:\n")
        for line in problems:
            print("  " + line)
        print("\nLösung: In CodeKit das ganze Projekt bauen, dann build/ mit committen.")
        return 1

    print(f"build/ passt zu source/ ({len(src)} Dateien geprüft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
