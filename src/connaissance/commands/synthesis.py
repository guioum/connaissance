"""Module commands/synthesis : synthèse et candidats pour fiches/chronologies.

Expose :
- `plan()` : entités et MOC à régénérer (wrapper pipeline.detect).
- `aliases_candidates(entity)` : scan déterministe des alias dans les résumés (NEW).
- `relations_candidates(entity)` : co-mentions via le frontmatter des résumés (NEW).
- `register(rel_path, source_type, source_path)` : enregistre un résumé dans la DB.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import cast

import yaml

from connaissance.core.paths import CONNAISSANCE_ROOT
from connaissance.core.tracking import TrackingDB
from connaissance.core.resolution import construire_slug

RESUMES = CONNAISSANCE_ROOT / "Résumés"
SYNTHESE = CONNAISSANCE_ROOT / "Synthèse"


def _parse_frontmatter(content: str) -> dict | None:
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 4)
    if end < 0:
        return None
    try:
        fm = yaml.safe_load(content[4:end]) or {}
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _iter_entity_resumes(entity: str) -> list[tuple[Path, dict]]:
    """Itérer sur les résumés d'une entité (format 'type/slug').

    Retourne la liste [(path, frontmatter_dict)] pour chaque résumé trouvé.
    """
    try:
        entity_type, entity_slug = entity.split("/", 1)
    except ValueError:
        return []

    results: list[tuple[Path, dict]] = []
    if not RESUMES.exists():
        return results

    for source_label in ("Documents", "Courriels", "Notes"):
        entity_dir = RESUMES / source_label / entity_type / entity_slug
        if not entity_dir.exists():
            continue
        for md_file in entity_dir.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(content)
            if fm is None:
                continue
            results.append((md_file, fm))
    return results


def _load_existing_aliases(entity: str) -> list[str]:
    try:
        entity_type, entity_slug = entity.split("/", 1)
    except ValueError:
        return []
    fiche_path = SYNTHESE / entity_type / entity_slug / "fiche.md"
    if not fiche_path.exists():
        return []
    try:
        fm = _parse_frontmatter(fiche_path.read_text(encoding="utf-8"))
    except OSError:
        return []
    if not fm:
        return []
    return [str(a) for a in (fm.get("aliases") or [])]


# --- API publique ---


def plan(db: TrackingDB | None = None) -> dict:
    """Lister les entités et MOC à régénérer (schema SynthesisPlan)."""
    from connaissance.commands import pipeline
    result = pipeline.detect(db=db, steps=["synthese_perimee", "moc_perimes"])
    return {
        "stale_entities": result.get("synthese_perimee", {}).get("entites", []),
        "stale_mocs": result.get("moc_perimes", {}).get("categories", []),
    }


def aliases_candidates(entity: str) -> dict:
    """Extraire les alias candidats pour une entité (schema AliasesCandidates, NEW).

    Scanne tous les résumés de l'entité et extrait :
    - les valeurs `from` (courriels) — candidats alias d'adresse email
    - les valeurs `entity_name` (documents/notes) — candidats alias de nom
    - les domaines extraits des adresses — candidats alias `*@domain`

    Chaque candidat est scoré par le nombre de résumés où il apparaît.
    Un support ≥ 2 peut être auto-accepté par Claude.
    """
    existing = set(_load_existing_aliases(entity))
    existing_lower = {a.lower() for a in existing}

    candidate_sources: dict[str, dict] = {}  # key_lower → {alias, kind, count}

    for _path, fm in _iter_entity_resumes(entity):
        entity_name = fm.get("entity_name")
        from_field = fm.get("from") or ""

        if entity_name:
            key = str(entity_name).strip().lower()
            if key and key not in existing_lower:
                entry = candidate_sources.setdefault(
                    key, {"alias": str(entity_name).strip(), "kind": "name", "count": 0})
                entry["count"] += 1

        if from_field:
            addr = str(from_field).strip().lower()
            if addr and addr not in existing_lower:
                entry = candidate_sources.setdefault(
                    addr, {"alias": str(from_field).strip(), "kind": "from", "count": 0})
                entry["count"] += 1
            if "@" in addr:
                domain = addr.rsplit("@", 1)[-1].rstrip(">").strip()
                if domain:
                    wildcard = f"*@{domain}"
                    if wildcard.lower() not in existing_lower:
                        entry = candidate_sources.setdefault(
                            wildcard.lower(),
                            {"alias": wildcard, "kind": "domain", "count": 0},
                        )
                        entry["count"] += 1

    candidates = sorted(
        ({"alias": v["alias"], "support_resumes": v["count"], "kind": v["kind"]}
         for v in candidate_sources.values()),
        key=lambda c: (-cast(int, c["support_resumes"]), c["alias"]),
    )

    return {
        "entity": entity,
        "existing_aliases": sorted(existing),
        "candidates": candidates,
    }


def relations_candidates(entity: str) -> dict:
    """Extraire les relations candidates via co-mentions (schema RelationsCandidates, NEW).

    Scanne tous les résumés de l'entité et collecte les autres entités
    mentionnées dans le frontmatter. Une entité co-mentionnée est une
    candidate de relation. Support = nombre de résumés où elle apparaît.
    """
    try:
        entity_type, entity_slug = entity.split("/", 1)
    except ValueError:
        return {"entity": entity, "candidates": []}

    co_mentions: dict[str, list[str]] = defaultdict(list)

    for md_path, fm in _iter_entity_resumes(entity):
        relations = fm.get("relations") or []
        for rel in relations:
            if not isinstance(rel, str) or "/" not in rel:
                continue
            other = rel.strip()
            if other == entity:
                continue
            try:
                rel_path = str(md_path.relative_to(CONNAISSANCE_ROOT))
            except ValueError:
                rel_path = str(md_path)
            co_mentions[other].append(rel_path)

    candidates = []
    for other, supports in co_mentions.items():
        candidates.append({
            "other": other,
            "co_mentions": len(supports),
            "support_resumes": supports,
        })
    candidates.sort(key=lambda c: (-cast(int, c["co_mentions"]), c["other"]))

    return {"entity": entity, "candidates": candidates}


def register(rel_path: str, source_type: str, source_path: str,
             db: TrackingDB | None = None) -> dict:
    """Enregistrer un résumé dans la DB."""
    if db is None:
        db = TrackingDB()
    db.register_file(rel_path, "resume",
                     source_type=source_type,
                     source_path=source_path)
    db.log("connaissance", "resume",
           source_type=source_type,
           source_path=source_path,
           dest_path=rel_path)
    return {"registered": 1, "file_type": "resume", "path": rel_path}
