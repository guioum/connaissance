"""Module commands/notes : scan et copie des notes Apple.

Expose :
- `scan(since, until) -> dict`
- `copy(dry_run=False, since=None, until=None, db=None) -> NotesCopy`
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from connaissance.core.paths import BASE_PATH, require_paths
from connaissance.core.tracking import TrackingDB
from connaissance.core.filtres import Filtres

NOTES_DIR = BASE_PATH / "Notes"
TRANSCRIPTIONS_DIR = BASE_PATH / "Connaissance" / "Transcriptions" / "Notes"


def _parse_frontmatter_dates(content: str) -> dict[str, str]:
    """Extraire created/modified du frontmatter YAML."""
    dates = {}
    if not content.startswith("---"):
        return dates
    # Chercher `\n---` pour éviter de matcher un `---` dans une valeur YAML.
    end = content.find("\n---", 4)
    if end < 0:
        return dates
    fm_text = content[4:end]
    for field in ("created", "modified"):
        match = re.search(rf'^{field}:\s*(\d{{4}}-\d{{2}}-\d{{2}})', fm_text, re.MULTILINE)
        if match:
            dates[field] = match.group(1)
    return dates


def _extract_attachment_refs(content: str) -> set[str]:
    """Extraire les noms de fichiers Attachments/ référencés dans le contenu."""
    refs = set()
    for match in re.findall(r'(?:!\[.*?\]|)\(Attachments/([^)]+)\)', content):
        refs.add(match)
    # Liens Markdown classiques aussi
    for match in re.findall(r'\[.*?\]\(Attachments/([^)]+)\)', content):
        refs.add(match)
    return refs


def scan_notes(since=None, until=None):
    """Scanner ~/Notes/ et retourner les notes à copier/mettre à jour."""
    if not NOTES_DIR.exists():
        return [], {}

    filtres = Filtres()
    to_process = []
    skipped = {}

    for f in sorted(NOTES_DIR.rglob("*.md")):
        if not f.is_file():
            continue
        if "Attachments" in f.parts:
            continue

        # Filtrage (dossiers ignorés + dates frontmatter)
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped["erreur_lecture"] = skipped.get("erreur_lecture", 0) + 1
            continue

        ok, reason = filtres.filter_note(f, content=content, since=since, until=until)
        if not ok:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        # Chemin de destination (miroir)
        rel = f.relative_to(NOTES_DIR)
        dest = TRANSCRIPTIONS_DIR / rel

        # Mode incrémental
        status = "nouveau"
        if dest.exists():
            if f.stat().st_mtime > dest.stat().st_mtime:
                status = "modifie"
            else:
                skipped["a_jour"] = skipped.get("a_jour", 0) + 1
                continue

        # Attachements référencés
        att_refs = _extract_attachment_refs(content)
        att_dir = f.parent / "Attachments"
        attachments = []
        for att_name in att_refs:
            att_src = att_dir / att_name
            if att_src.exists():
                attachments.append(att_name)

        dates = _parse_frontmatter_dates(content)

        to_process.append({
            "source": str(f),
            "destination": str(dest),
            "rel": str(rel),
            "status": status,
            "size": f.stat().st_size,
            "attachments": attachments,
            "created": dates.get("created"),
            "modified": dates.get("modified"),
        })

    return to_process, skipped


def copy_notes(items, db, dry_run=False):
    """Copier les notes et leurs attachements."""
    copied = 0
    updated = 0
    att_copied = 0

    for item in items:
        src = Path(item["source"])
        dest = Path(item["destination"])
        status = item["status"]

        if dry_run:
            label = "→ copier" if status == "nouveau" else "→ mettre à jour"
            print(f"  {label} : {item['rel']}")
            if item["attachments"]:
                print(f"    + {len(item['attachments'])} attachement(s)")
            if status == "nouveau":
                copied += 1
            else:
                updated += 1
            att_copied += len(item["attachments"])
            continue

        # Copier la note (cp -p préserve les dates)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))

        # Copier les attachements référencés
        att_src_dir = src.parent / "Attachments"
        att_dst_dir = dest.parent / "Attachments"
        for att_name in item["attachments"]:
            att_src = att_src_dir / att_name
            att_dst = att_dst_dir / att_name
            if att_src.exists() and (not att_dst.exists() or att_src.stat().st_mtime > att_dst.stat().st_mtime):
                att_dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(att_src), str(att_dst))
                att_copied += 1

        # Tracking
        try:
            rel_path = str(dest.relative_to(BASE_PATH / "Connaissance"))
        except ValueError:
            rel_path = str(dest)

        db.register_file(rel_path, "transcription",
                         source_type="note",
                         source_path=str(src),
                         created=item.get("created"),
                         modified=item.get("modified"))
        db.log("connaissance", "copy_note",
               source_type="note",
               source_path=str(src),
               dest_path=rel_path)

        if status == "nouveau":
            copied += 1
        else:
            updated += 1

    return copied, updated, att_copied


# --- API publique ---


def _parse_dates(since, until):
    if isinstance(since, str):
        since = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if isinstance(until, str):
        until = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return since, until


def scan(since=None, until=None) -> dict:
    """Lister les notes à copier (schema dict avec to_copy + skipped)."""
    require_paths(NOTES_DIR, context="notes scan")
    since, until = _parse_dates(since, until)
    to_process, skipped = scan_notes(since, until)
    return {
        "to_copy": to_process,
        "skipped": [{"reason": k, "count": v} for k, v in sorted(skipped.items())],
    }


def copy(dry_run: bool = False, since=None, until=None,
         db: TrackingDB | None = None) -> dict:
    """Copier les notes (schema NotesCopy)."""
    require_paths(NOTES_DIR, context="notes copy")
    since, until = _parse_dates(since, until)
    if db is None:
        db = TrackingDB()
    to_process, _ = scan_notes(since, until)
    if not to_process:
        return {"copied": 0, "skipped": 0, "errors": []}
    copied, updated, att_copied = copy_notes(to_process, db, dry_run=dry_run)
    return {
        "copied": copied + updated,
        "skipped": 0,
        "errors": [],
        "attachments_copied": att_copied,
        "dry_run": dry_run,
    }
