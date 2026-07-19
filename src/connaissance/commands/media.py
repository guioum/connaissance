"""Module commands/media : ranger les MÉDIAS de ~/Documents par date.

Volet « groupes B/C/D par logique propre » du grand chantier. Le code et les
exports sont déjà gardés en unités par le triage (Phase A) ; le seul geste neuf
est de ranger les **médias** (images/audio/vidéo) sous ``- Médias/AAAA/MM/`` —
une logique propre aux médias, distincte du classement par entité des documents.

Date d'un média : date dans le nom si présente, sinon date de création/modif
filesystem (jamais de download iCloud — stat seul). Plan→apply via le **ledger**
(réversible), dry-run par défaut. Le préfixe « - » exclut la cible du scan.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from connaissance.core import ledger as _ledger
from connaissance.core.manifest_io import load_entries
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import DOCUMENTS_DIR, require_paths, transit_file
from connaissance.core.schemas import MediaApply, MediaPlan
from connaissance.core.signals import _date_from_name, _fs_dates
from connaissance.core.tracking import TrackingDB
from connaissance.commands.triage import MARKER_DIRS, MEDIA_EXTS

MEDIA_VIEW_NAME = "- Médias"
# Dossiers déjà « à part » qu'on ne ré-éclate jamais (vues, archives, protégés).
_SKIP_PREFIX = "-"


def _media_date(path: Path) -> tuple[str, str]:
    """(AAAA, MM) d'un média : date du nom sinon création/modif filesystem.

    Retourne ``("0000", "00")`` si rien d'exploitable (range en « sans-date »)."""
    d = _date_from_name(path.name)
    if not d:
        created, modified = _fs_dates(path)
        d = created or modified
    if d and len(d) >= 7:
        return d[:4], d[5:7]
    return "0000", "00"


def _iter_media(base: Path):
    """Médias sous ``base``, en élaguant conteneurs/vues/dossiers cachés."""
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs
                   if not d.startswith(_SKIP_PREFIX)
                   and not d.startswith(".")
                   and d not in MARKER_DIRS]
        for name in files:
            if name.startswith("."):
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext in MEDIA_EXTS:
                yield Path(root) / name


def plan(scope: str | None = None, output_file: str | None = None) -> MediaPlan:
    """Construire un manifeste de rangement des médias par date (schema MediaPlan).

    N'écrit/ne déplace rien sur le corpus — produit un manifeste plan→apply.
    """
    require_paths(DOCUMENTS_DIR, context="media plan")
    base = DOCUMENTS_DIR if scope is None else (DOCUMENTS_DIR / scope)
    entries: list[dict] = []
    by_year: dict[str, int] = {}
    for src in _iter_media(base):
        year, month = _media_date(src)
        rel_src = str(src.relative_to(DOCUMENTS_DIR))
        dest = f"{MEDIA_VIEW_NAME}/{year}/{month}/{src.name}"
        if rel_src == dest:
            continue  # déjà rangé
        entries.append({"source": rel_src, "dest": dest})
        by_year[year] = by_year.get(year, 0) + 1

    transit = transit_file("media-manifest")
    transit.write_text(json.dumps({"entries": entries}, ensure_ascii=False),
                       encoding="utf-8")
    payload: MediaPlan = {
        "total": len(entries),
        "by_year": dict(sorted(by_year.items(), reverse=True)),
        "manifest_file": str(transit),
        "entries": entries,
    }

    def _summary(p: dict) -> dict:
        return {"total": p["total"], "by_year": p["by_year"],
                "manifest_file": p["manifest_file"],
                "sample": p["entries"][:8]}

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)


def apply(manifest_file: str, dry_run: bool = True,
          db: TrackingDB | None = None) -> MediaApply:
    """Appliquer un manifeste de rangement médias (schema MediaApply).

    Déplace chaque média via le **ledger** (réversible). **Dry-run par défaut.**
    Collisions de noms gérées (suffixe ``(2)``).
    """
    require_paths(DOCUMENTS_DIR, context="media apply")
    _, entries = load_entries(manifest_file)
    owns = db is None
    if db is None:
        db = TrackingDB()
    run_id = _ledger.new_run_id("media")
    moved: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    try:
        for e in entries:
            src = DOCUMENTS_DIR / e["source"]
            if not src.exists():
                skipped.append({"source": e["source"], "reason": "introuvable"})
                continue
            dst = DOCUMENTS_DIR / e["dest"]
            i = 2
            while dst.exists():
                dst = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
                i += 1
            if dry_run:
                moved.append({"source": e["source"], "dest": e["dest"]})
                continue
            try:
                _ledger.safe_move(db, src, dst, "media by date", run_id)
                moved.append({"source": e["source"],
                              "dest": str(dst.relative_to(DOCUMENTS_DIR))})
            except OSError as exc:
                errors.append({"source": e["source"], "error": str(exc)})
    finally:
        if owns:
            db.close()

    result: MediaApply = {
        "dry_run": dry_run,
        "planned": len(entries),
        "moved": 0 if dry_run else len(moved),
        "would_move": len(moved) if dry_run else 0,
        "skipped": skipped,
        "errors": errors,
        "moves": moved[:50],
    }
    if not dry_run and moved:
        result["ledger_run"] = run_id
    return result
