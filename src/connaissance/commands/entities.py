"""Module commands/entities : détecter et fusionner les doublons d'entités.

Le registre d'entités vit en fiches ``~/Connaissance/Synthèse/{type}/<slug>/`` ;
les fiches de classement (``doc_classification``) y réfèrent par
``entity_type``/``entity_slug``. Quand la même entité apparaît sous deux slugs
(``ville-de-montreal`` vs ``ville-montreal``…), le rangement se fragmente.

- ``candidates`` : paires suspectes (lecture seule, signaux lexicaux).
- ``merge`` : fusionner ``from`` → ``into`` (plan→apply) : repointe la DB,
  ajoute le nom/aliases du perdant aux ``aliases`` de la fiche gardée, déplace
  ses résumés (ledger) et envoie sa fiche à la corbeille. Dry-run par défaut.
"""
from __future__ import annotations

import yaml

from connaissance.core import entities as _ent
from connaissance.core import ledger as _ledger
from connaissance.core.paths import (CONNAISSANCE_ROOT, DOCUMENTS_DIR,
                                      require_connaissance_root)
from connaissance.core.tracking import TrackingDB

RESUMES = CONNAISSANCE_ROOT / "Résumés"
SYNTHESE = CONNAISSANCE_ROOT / "Synthèse"
_SOURCE_LABELS = ("Documents", "Courriels", "Notes")


def _rmdir_if_empty(d) -> None:
    """Supprimer un dossier devenu vide après déplacement (silencieux sinon)."""
    try:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass


def _fiche_frontmatter(etype: str, slug: str) -> dict | None:
    fiche = SYNTHESE / etype / slug / "fiche.md"
    if not fiche.is_file():
        return None
    try:
        content = fiche.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return {}
        fm = yaml.safe_load(content.split("---", 2)[1])
        return fm if isinstance(fm, dict) else {}
    except (OSError, IndexError, yaml.YAMLError):
        return None


def _inventory(db: TrackingDB) -> list[dict]:
    """Union des entités : fiches Synthèse (nom canonique) + slugs en usage
    dans ``doc_classification`` (avec compteurs)."""
    inv: dict[tuple, dict] = {}
    # En usage (avec counts).
    for e in db.distinct_entities():
        key = (e["entity_type"], e["entity_slug"])
        inv[key] = {"entity_type": e["entity_type"],
                    "entity_slug": e["entity_slug"],
                    "name": e.get("entity"), "count": e.get("count") or 0}
    # Registre (fiches) — ajoute celles sans doc classé.
    for etype in ("personnes", "organismes", "divers"):
        tdir = SYNTHESE / etype
        if not tdir.is_dir():
            continue
        for d in tdir.iterdir():
            if not (d / "fiche.md").is_file():
                continue
            key = (etype, d.name)
            if key not in inv:
                fm = _fiche_frontmatter(etype, d.name) or {}
                inv[key] = {"entity_type": etype, "entity_slug": d.name,
                            "name": fm.get("name") or d.name, "count": 0}
    # Dossiers physiques ~/Documents/<type>/<slug>/ — entités rangées sans fiche
    # ni ligne de classement (ex. acronymes bdc/bnc). Sinon invisibles.
    for etype in ("personnes", "organismes", "divers"):
        tdir = DOCUMENTS_DIR / etype
        if not tdir.is_dir():
            continue
        for d in tdir.iterdir():
            if not d.is_dir() or d.name.startswith("-"):
                continue
            key = (etype, d.name)
            if key not in inv:
                inv[key] = {"entity_type": etype, "entity_slug": d.name,
                            "name": d.name, "count": 0}
    return list(inv.values())


def candidates(db: TrackingDB | None = None) -> dict:
    """Paires d'entités candidates à la fusion (schema EntitiesCandidates).

    Lecture seule. Combine signaux lexicaux (containment, Jaccard, edit
    distance, acronyme) sur le registre + les entités en usage.
    """
    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        inv = _inventory(db)
    finally:
        if owns:
            db.close()
    pairs = _ent.find_candidates(inv)
    return {"total_entities": len(inv), "candidates": pairs,
            "count": len(pairs)}


def _split(entity: str) -> tuple[str, str]:
    etype, slug = entity.split("/", 1)
    return etype, slug


def _add_aliases(etype: str, slug: str, new_aliases: list[str]) -> list[str]:
    """Ajouter des aliases à la fiche gardée (dédup casse-insensible). Retourne
    les aliases réellement ajoutés. Édition additive (hors ledger)."""
    fiche = SYNTHESE / etype / slug / "fiche.md"
    if not fiche.is_file():
        return []
    content = fiche.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return []
    try:
        _, fm_text, body = content.split("---", 2)
    except ValueError:
        return []
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        return []
    existing = [str(a) for a in (fm.get("aliases") or [])]
    lower = {a.lower() for a in existing}
    added = []
    for a in new_aliases:
        a = str(a).strip()
        if a and a.lower() not in lower:
            existing.append(a)
            lower.add(a.lower())
            added.append(a)
    if not added:
        return []
    fm["aliases"] = existing
    new_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    fiche.write_text(f"---\n{new_fm}\n---{body}", encoding="utf-8")
    return added


def merge(from_entity: str, into_entity: str, dry_run: bool = True,
          db: TrackingDB | None = None) -> dict:
    """Fusionner ``from_entity`` → ``into_entity`` (schema EntitiesMerge).

    ``from_entity``/``into_entity`` au format ``type/slug``. **Dry-run par
    défaut.** À l'apply : repointe ``doc_classification``+``files`` (atomique),
    ajoute nom/aliases du perdant à la fiche gardée, déplace ses résumés (ledger)
    et envoie sa fiche à la corbeille. Réversible par ``ledger revert``.
    """
    require_connaissance_root()
    f_type, f_slug = _split(from_entity)
    i_type, i_slug = _split(into_entity)
    if (f_type, f_slug) == (i_type, i_slug):
        return {"error": "from et into identiques"}

    owns = db is None
    if db is None:
        db = TrackingDB()
    run_id = _ledger.new_run_id("entities-merge")

    # Inventaire pour le nom + compteur du perdant/gardé.
    from_fm = _fiche_frontmatter(f_type, f_slug) or {}
    into_name = (_fiche_frontmatter(i_type, i_slug) or {}).get("name") or i_slug
    to_reassign = [e for e in db.distinct_entities()
                   if e["entity_type"] == f_type and e["entity_slug"] == f_slug]
    docs_n = sum(e.get("count") or 0 for e in to_reassign)
    from_name = (from_fm.get("name")
                 or (to_reassign[0]["entity"] if to_reassign else f_slug))
    alias_payload = [from_name, f_slug] + [str(a) for a in
                                           (from_fm.get("aliases") or [])]

    # Résumés du perdant à déplacer (~/Connaissance/Résumés/<label>/<type>/<slug>/).
    resume_moves: list[tuple] = []
    for label in _SOURCE_LABELS:
        d = RESUMES / label / f_type / f_slug
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file():
                    dest = RESUMES / label / i_type / i_slug / p.relative_to(d)
                    resume_moves.append((p, dest))

    # Documents BRUTS du perdant à déplacer (~/Documents/<type>/<slug>/) — le cas
    # que la première version oubliait : l'utilisateur range ses docs ici.
    doc_moves: list[tuple] = []
    from_docs_dir = DOCUMENTS_DIR / f_type / f_slug
    if from_docs_dir.is_dir():
        for p in from_docs_dir.rglob("*"):
            if p.is_file():
                dest = DOCUMENTS_DIR / i_type / i_slug / p.relative_to(from_docs_dir)
                doc_moves.append((p, dest))

    from_fiche_dir = SYNTHESE / f_type / f_slug

    if dry_run:
        if owns:
            db.close()
        return {
            "dry_run": True,
            "from": from_entity, "into": into_entity,
            "docs_to_reassign": docs_n,
            "resumes_to_move": len(resume_moves),
            "documents_to_move": len(doc_moves),
            "aliases_to_add": alias_payload,
            "from_fiche_exists": from_fiche_dir.is_dir(),
        }

    reassigned = 0
    moved = 0
    docs_moved = 0
    try:
        with db.transaction():
            reassigned = db.reassign_entity(f_type, f_slug, i_type, i_slug,
                                            into_name, commit=False)
        added = _add_aliases(i_type, i_slug, alias_payload)
        for src, dest in resume_moves:
            try:
                _ledger.safe_move(db, src, dest, "entities merge", run_id)
                moved += 1
            except OSError:
                pass
        for src, dest in doc_moves:
            try:
                _ledger.safe_move(db, src, dest, "entities merge (document)",
                                  run_id)
                docs_moved += 1
            except OSError:
                pass
        trashed = False
        if from_fiche_dir.is_dir():
            for p in sorted(from_fiche_dir.rglob("*")):
                if p.is_file():
                    _ledger.safe_trash(db, p, "entities merge (fiche)", run_id)
            trashed = True
        # Nettoyer les dossiers vidés du perdant (Synthèse + Documents + Résumés).
        _rmdir_if_empty(from_fiche_dir)
        _rmdir_if_empty(from_docs_dir)
        for label in _SOURCE_LABELS:
            _rmdir_if_empty(RESUMES / label / f_type / f_slug)
    finally:
        if owns:
            db.close()

    return {
        "dry_run": False,
        "from": from_entity, "into": into_entity,
        "reassigned": reassigned,
        "resumes_moved": moved,
        "documents_moved": docs_moved,
        "aliases_added": added,
        "from_fiche_trashed": trashed,
        "ledger_run": run_id,
    }
