"""Module commands/emails_cleanup : re-scoring rétroactif et archivage.

Expose `cleanup_obsolete(dry_run, only_domain, only_entity, since)` comme
API publique. Archivage réversible vers `~/Connaissance/.archive/courriels-depublies/`.
"""

from __future__ import annotations
import sys
import hashlib
import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from connaissance.core.paths import CONNAISSANCE_ROOT, require_connaissance_root
from connaissance.core.tracking import TrackingDB
from connaissance.core.filtres import Filtres

TRANSCRIPTIONS_COURRIELS = CONNAISSANCE_ROOT / "Transcriptions" / "Courriels"
RESUMES_COURRIELS = CONNAISSANCE_ROOT / "Résumés" / "Courriels"
ARCHIVE_ROOT = CONNAISSANCE_ROOT / ".archive" / "courriels-depublies"


def is_trackable(path: Path) -> bool:
    """Exclure les fichiers qu'on ne veut pas re-scorer."""
    if path.name.startswith("_"):
        return False
    if "Attachments" in path.parts:
        return False
    return True


def parse_frontmatter(content: str) -> tuple[dict, str] | None:
    """Extraire frontmatter YAML + body d'un fichier markdown.

    Retourne (frontmatter_dict, body_str) ou None si pas de frontmatter.
    """
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 4)
    if end < 0:
        return None
    raw = content[4:end].lstrip("\n")
    try:
        fm = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    # Sauter le "\n---" fermant + un éventuel saut de ligne
    body = content[end + 4:].lstrip("\n")
    return fm, body


def extract_attachments_from_body(body: str) -> list[dict]:
    """Parser la section `## Pièces jointes` d'un corps de transcription courriel."""
    attachments: list[dict] = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if "Pièces jointes" in stripped or "Attachements" in stripped or "Pieces jointes" in stripped:
                in_section = True
                continue
            if in_section:
                # Nouvelle section, on sort
                break
        if in_section and stripped.startswith("- "):
            attachments.append({"name": stripped[2:]})
    return attachments


def build_msg_dict_from_transcription(path: Path) -> dict | None:
    """Construire un dict compatible avec filtres.score_courriel() depuis une transcription.

    Les signaux headers bruts (list-unsubscribe, is_html_only) ne sont pas
    présents dans le frontmatter — absents = signaux neutres. Le re-scoring
    capture surtout les NOUVELLES règles (blacklist, patterns, seuils) que
    l'utilisateur vient d'ajouter, ce qui est l'usage visé.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = parse_frontmatter(content)
    if parsed is None:
        return None
    fm, body = parsed

    from_raw = fm.get("from", "") or ""
    from_display = ""
    m = re.match(r'^(.+?)\s*<.+>$', from_raw)
    if m:
        from_display = m.group(1).strip('"\' ')

    return {
        "from": from_raw,
        "from_display": from_display,
        "to": fm.get("to", "") or "",
        "cc": fm.get("cc", "") or "",
        "subject": fm.get("subject", "") or "",
        "body": body,
        "folder": fm.get("folder", "") or "",
        "attachments": extract_attachments_from_body(body),
        "headers": {},
        "is_html_only": False,
        "date": fm.get("date") or fm.get("created") or "",
        "message_id": fm.get("message-id", "") or "",
    }


def build_source_to_resume_map() -> dict[str, Path]:
    """Scanner Résumés/Courriels/ pour construire un reverse-map source → resume."""
    m: dict[str, Path] = {}
    if not RESUMES_COURRIELS.exists():
        return m
    for resume_path in RESUMES_COURRIELS.rglob("*.md"):
        if not is_trackable(resume_path):
            continue
        try:
            content = resume_path.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_frontmatter(content)
        if parsed is None:
            continue
        fm, _ = parsed
        source = fm.get("source")
        if source:
            m[str(source)] = resume_path
    return m


def scan_obsoletes(filtres: Filtres,
                    since: datetime | None = None,
                    until: datetime | None = None) -> list[dict]:
    """Scanner toutes les transcriptions courriels et retourner celles qui seraient filtrées."""
    seuil_ignorer = filtres.scoring_config.get("seuils", {}).get("ignorer", -1)
    source_to_resume = build_source_to_resume_map()
    obsoletes: list[dict] = []

    if not TRANSCRIPTIONS_COURRIELS.exists():
        return obsoletes

    for trans_path in TRANSCRIPTIONS_COURRIELS.rglob("*.md"):
        if not is_trackable(trans_path):
            continue
        msg_dict = build_msg_dict_from_transcription(trans_path)
        if msg_dict is None:
            continue

        # Filtre temporel optionnel
        if since or until:
            date_str = str(msg_dict.get("date", ""))
            if date_str:
                try:
                    # Accepte YYYY-MM-DD ou YYYY-MM-DDTHH:MM:SS
                    msg_date = datetime.fromisoformat(date_str.split("T")[0])
                    if since and msg_date < since.replace(tzinfo=None):
                        continue
                    if until and msg_date >= until.replace(tzinfo=None):
                        continue
                except (ValueError, TypeError):
                    pass  # garder le message si date non parsable

        score, reasons = filtres.score_courriel(msg_dict)
        if score <= seuil_ignorer:
            trans_rel = str(trans_path.relative_to(CONNAISSANCE_ROOT))
            obsoletes.append({
                "transcription": trans_path,
                "transcription_rel": trans_rel,
                "resume": source_to_resume.get(trans_rel),
                "from": msg_dict["from"],
                "subject": msg_dict["subject"][:100],
                "date": str(msg_dict.get("date", "")),
                "folder": msg_dict["folder"],
                "score": score,
                "reasons": reasons,
            })

    return obsoletes


def apply_user_filters(obsoletes: list[dict],
                       only_domain: str | None = None,
                       only_entity: str | None = None) -> list[dict]:
    """Appliquer les filtres (--only-domain, --only-entity)."""
    result = obsoletes

    if only_domain:
        allowed_domains = {d.strip().lower() for d in only_domain.split(",")}
        def has_domain(item):
            from_addr = (item.get("from") or "").lower()
            if "@" not in from_addr:
                return False
            domain = from_addr.rsplit("@", 1)[-1].rstrip(">").strip()
            return domain in allowed_domains
        result = [i for i in result if has_domain(i)]

    if only_entity:
        allowed_entity = only_entity.strip()
        result = [i for i in result if f"/{allowed_entity}/" in i["transcription_rel"]
                  or i["transcription_rel"].endswith(f"/{allowed_entity}")]

    return result


def print_text_report(obsoletes: list[dict], total_scanned: int) -> None:
    """Affichage humain du dry-run."""
    print(f"\n── Re-scoring rétroactif ──", file=sys.stderr)

    print(f"  Transcriptions courriels scannées : {total_scanned}", file=sys.stderr)

    print(f"  Courriels qui seraient filtrés     : {len(obsoletes)}", file=sys.stderr)


    if not obsoletes:
        print(f"\n  Aucun courriel obsolète avec la config actuelle.", file=sys.stderr)

        return

    # Groupement par domaine
    domain_counter: Counter[str] = Counter()
    for item in obsoletes:
        from_addr = (item.get("from") or "").lower()
        if "@" in from_addr:
            domain = from_addr.rsplit("@", 1)[-1].rstrip(">").strip()
            domain_counter[domain] += 1

    print(f"\n  Top 10 domaines concernés :", file=sys.stderr)

    for domain, count in domain_counter.most_common(10):
        print(f"    {domain:40s} {count:>4d}", file=sys.stderr)


    # Groupement par dossier
    folder_counter: Counter[str] = Counter()
    for item in obsoletes:
        folder_counter[item.get("folder") or "(inconnu)"] += 1
    print(f"\n  Par dossier :", file=sys.stderr)

    for folder, count in folder_counter.most_common():
        print(f"    {folder:20s} {count:>4d}", file=sys.stderr)


    # Exemples représentatifs
    print(f"\n  Exemples (5 premiers) :", file=sys.stderr)

    for item in obsoletes[:5]:
        date = item.get("date", "?")[:10] if item.get("date") else "?"
        from_short = (item.get("from") or "")[:40]
        subject = (item.get("subject") or "")[:50]
        print(f"    [{item['score']:+d}] {date} {from_short:40s} | {subject}", file=sys.stderr)

        for reason in item["reasons"][:3]:
            print(f"         → {reason}", file=sys.stderr)


    print(f"\n  Pour archiver : --apply", file=sys.stderr)

    print(f"  Pour cibler un domaine : --apply --only-domain DOMAIN", file=sys.stderr)



def print_json_report(obsoletes: list[dict], total_scanned: int) -> None:
    """Sortie JSON pour consommation programmatique."""
    # Sérialiser les Path en str
    items = []
    for item in obsoletes:
        items.append({
            "transcription_rel": item["transcription_rel"],
            "resume_rel": (str(item["resume"].relative_to(CONNAISSANCE_ROOT))
                           if item.get("resume") else None),
            "from": item["from"],
            "subject": item["subject"],
            "date": item["date"],
            "folder": item["folder"],
            "score": item["score"],
            "reasons": item["reasons"],
        })

    print(json.dumps({
        "total_scanned": total_scanned,
        "obsoletes_count": len(obsoletes),
        "items": items,
    }, indent=2, ensure_ascii=False))


def archive_items(obsoletes: list[dict], db: TrackingDB, scoring_config: dict) -> Path:
    """Déplacer les fichiers flagués vers l'archive et mettre à jour la DB."""
    require_connaissance_root()

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = ARCHIVE_ROOT / timestamp
    archive_dir.mkdir(parents=True, exist_ok=False)

    # Préparer le manifest
    config_str = yaml.dump(scoring_config, sort_keys=True, allow_unicode=True)
    config_sha = hashlib.sha256(config_str.encode("utf-8")).hexdigest()

    manifest = {
        "archived_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "script_version": "1.0",
        "scoring_config_sha256": config_sha,
        "thresholds_used": scoring_config.get("seuils", {}),
        "count": len(obsoletes),
        "items": [],
    }

    for item in obsoletes:
        trans = item["transcription"]
        resume = item["resume"]
        trans_rel = item["transcription_rel"]

        manifest_item = {
            "transcription_original": trans_rel,
            "transcription_archived": None,
            "resume_original": None,
            "resume_archived": None,
            "from": item["from"],
            "subject": item["subject"],
            "date": item["date"],
            "folder": item["folder"],
            "score": item["score"],
            "reasons": item["reasons"],
        }

        # Archiver la transcription
        trans_dest = archive_dir / trans_rel
        trans_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trans), str(trans_dest))
        manifest_item["transcription_archived"] = str(trans_dest.relative_to(archive_dir))

        # Retirer de la DB (ignore si absente)
        db._conn.execute("DELETE FROM files WHERE path = ?", (trans_rel,))

        # Log
        db.log("connaissance", "archive_obsolete_courriel",
               source_type="courriel",
               source_path=trans_rel,
               dest_path=str(trans_dest.relative_to(CONNAISSANCE_ROOT)),
               details={
                   "score": item["score"],
                   "reasons": item["reasons"],
                   "from": item["from"],
                   "subject": item["subject"],
               })

        # Archiver le résumé correspondant s'il existe
        if resume is not None and resume.exists():
            resume_rel = str(resume.relative_to(CONNAISSANCE_ROOT))
            resume_dest = archive_dir / resume_rel
            resume_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(resume), str(resume_dest))
            db._conn.execute("DELETE FROM files WHERE path = ?", (resume_rel,))
            manifest_item["resume_original"] = resume_rel
            manifest_item["resume_archived"] = str(resume_dest.relative_to(archive_dir))

        manifest["items"].append(manifest_item)

    db._conn.commit()

    # Écrire le manifest
    manifest_path = archive_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    return archive_dir


# --- API publique ---


def cleanup_obsolete(dry_run: bool = True,
                     only_domain: str | None = None,
                     only_entity: str | None = None,
                     since=None, until=None,
                     db: TrackingDB | None = None) -> dict:
    """Re-scorer et archiver les courriels obsolètes (schema EmailsCleanupObsolete).

    Mode par défaut : dry_run=True (ne modifie rien, retourne la liste).
    Appeler avec dry_run=False pour appliquer l'archivage réversible.
    """
    require_connaissance_root()

    if isinstance(since, str):
        since = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if isinstance(until, str):
        until = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    filtres = Filtres()
    owns_db = db is None
    if db is None:
        db = TrackingDB()

    try:
        total_scanned = 0
        if TRANSCRIPTIONS_COURRIELS.exists():
            total_scanned = sum(
                1 for p in TRANSCRIPTIONS_COURRIELS.rglob("*.md") if is_trackable(p)
            )

        obsoletes = scan_obsoletes(filtres, since=since, until=until)
        obsoletes = apply_user_filters(obsoletes, only_domain=only_domain,
                                       only_entity=only_entity)

        would_archive = [{
            "transcription_rel": o.get("transcription_rel"),
            "from": o.get("from"),
            "subject": o.get("subject"),
            "score": o.get("score"),
            "reasons": o.get("reasons", []),
        } for o in obsoletes]

        if dry_run or not obsoletes:
            return {
                "would_archive": would_archive,
                "archived_to": "",
                "manifest_path": "",
                "total_scanned": total_scanned,
                "dry_run": True,
            }

        archive_dir = archive_items(obsoletes, db, filtres.scoring_config)
        return {
            "would_archive": would_archive,
            "archived_to": str(archive_dir),
            "manifest_path": str(archive_dir / "manifest.json"),
            "total_scanned": total_scanned,
            "dry_run": False,
        }
    finally:
        if owns_db:
            db.close()
