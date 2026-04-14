#!/usr/bin/env python3
"""Réparer les références d'attachments cassées dans les transcriptions documents.

Scanne les .md sous `Transcriptions/Documents/` qui référencent
`./Attachments/<fichier>` inexistant au bon endroit, cherche le fichier
dans un dossier `Attachments` central (typiquement
`Transcriptions/Documents/Attachments/` résiduel) et copie le fichier
vers `<parent-du-md>/Attachments/` pour que la référence relative
fonctionne.

Ne modifie pas le contenu des .md, ne supprime pas les fichiers centraux.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from connaissance.core.paths import BASE_PATH

TRANSCRIPTIONS_DOCS = BASE_PATH / "Connaissance" / "Transcriptions" / "Documents"
CENTRAL_ATT = TRANSCRIPTIONS_DOCS / "Attachments"
PATTERN = re.compile(r'\(\.?/?Attachments/([^)]+)\)')


def _find_attachment(fname: str) -> Path | None:
    """Chercher un fichier attachment dans tout l'arbre Transcriptions/Documents.

    Ordre de recherche :
    1. Dossier Attachments central (Transcriptions/Documents/Attachments/)
    2. N'importe quel autre dossier Attachments/ dans le sous-arbre (fallback)
    """
    central_file = CENTRAL_ATT / fname
    if central_file.exists():
        return central_file
    for att_dir in TRANSCRIPTIONS_DOCS.rglob("Attachments"):
        if not att_dir.is_dir():
            continue
        candidate = att_dir / fname
        if candidate.exists():
            return candidate
    return None


def repair(dry_run: bool = False) -> dict:
    stats = {"scanned": 0, "repaired": 0, "missing": 0, "already_ok": 0}
    if not TRANSCRIPTIONS_DOCS.exists():
        print(f"✗ Pas de dossier {TRANSCRIPTIONS_DOCS}")
        return stats

    for md in sorted(TRANSCRIPTIONS_DOCS.rglob("*.md")):
        if "Attachments" in md.parts or md.name.startswith("_"):
            continue
        # Les .md à la racine de Documents/ ont ./Attachments/ qui pointe
        # directement vers CENTRAL_ATT — pas besoin de réparer.
        if md.parent == TRANSCRIPTIONS_DOCS:
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except OSError:
            continue
        refs = PATTERN.findall(content)
        if not refs:
            continue
        stats["scanned"] += 1

        dst_att_dir = md.parent / "Attachments"
        for fname in refs:
            local_file = dst_att_dir / fname
            if local_file.exists():
                stats["already_ok"] += 1
                continue
            source_file = _find_attachment(fname)
            if source_file is None:
                stats["missing"] += 1
                print(f"  ✗ Introuvable : "
                      f"{md.relative_to(TRANSCRIPTIONS_DOCS)} → {fname}")
                continue
            rel_dst = dst_att_dir.relative_to(TRANSCRIPTIONS_DOCS)
            rel_src = source_file.relative_to(TRANSCRIPTIONS_DOCS)
            if dry_run:
                print(f"  [dry-run] {rel_src} → {rel_dst}/")
            else:
                dst_att_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(source_file), str(local_file))
                print(f"  ✓ {rel_src} → {rel_dst}/")
            stats["repaired"] += 1

    return stats


# `repair()` ci-dessus est déjà l'API publique — rien de plus à ajouter.
