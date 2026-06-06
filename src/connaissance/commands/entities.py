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
from connaissance.core import relocate as _reloc
from connaissance.core.paths import (CONNAISSANCE_ROOT, DOCUMENTS_DIR,
                                      require_connaissance_root)
from connaissance.core.tracking import TrackingDB

RESUMES = CONNAISSANCE_ROOT / "Résumés"
TRANSCRIPTIONS = CONNAISSANCE_ROOT / "Transcriptions"
SYNTHESE = CONNAISSANCE_ROOT / "Synthèse"
_SOURCE_LABELS = ("Documents", "Courriels", "Notes")


def _relocate_entity_docs(db, ft, fs, it, is_, run_id) -> int:
    """Déplacer chaque DOCUMENT (label Documents) de ``ft/fs`` → ``it/is_`` via la
    primitive ``relocate_document`` (graphe complet + toutes les références).
    Retourne le nombre de documents relocalisés."""
    docs_dir = DOCUMENTS_DIR / ft / fs
    n = 0
    if docs_dir.is_dir():
        for p in sorted(docs_dir.rglob("*")):
            if p.is_file():
                sub = p.relative_to(docs_dir)
                _reloc.relocate_document(
                    db, f"{ft}/{fs}/{sub}", f"{it}/{is_}/{sub}", run_id)
                n += 1
    return n


def _sweep_entity_rest(db, ft, fs, it, is_, run_id, *, trash_fiche=False) -> int:
    """Déplacer le RESTE de ``ft/fs`` → ``it/is_`` : résumés/transcriptions
    Courriels/Notes + orphelins Documents que ``relocate`` n'a pas pris faute de
    source. La fiche Synthèse est déplacée (rename) ou mise en corbeille
    (``trash_fiche`` pour une fusion). Retourne le nombre de fichiers traités."""
    moved = 0
    # Synthèse (fiche) : déplacer (rename) ou corbeiller (merge).
    syn_old = SYNTHESE / ft / fs
    if syn_old.is_dir():
        for p in sorted(syn_old.rglob("*")):
            if p.is_file():
                try:
                    if trash_fiche:
                        _ledger.safe_trash(db, p, "entities merge (fiche)", run_id)
                    else:
                        _ledger.safe_move(db, p, SYNTHESE / it / is_ / p.relative_to(syn_old),
                                          "entity rename (fiche)", run_id)
                    moved += 1
                except OSError:
                    pass
    # Résumés/transcriptions de TOUS les labels (Documents orphelins + Courriels/Notes).
    for root in (RESUMES, TRANSCRIPTIONS):
        for lbl in _SOURCE_LABELS:
            old_dir = root / lbl / ft / fs
            if not old_dir.is_dir():
                continue
            for p in sorted(old_dir.rglob("*")):
                if p.is_file():
                    try:
                        _ledger.safe_move(db, p, root / lbl / it / is_ / p.relative_to(old_dir),
                                          "entity move (rest)", run_id)
                        moved += 1
                    except OSError:
                        pass
    return moved


def _cleanup_entity_dirs(ft, fs) -> None:
    for base in ([DOCUMENTS_DIR / ft, SYNTHESE / ft]
                 + [RESUMES / lbl / ft for lbl in _SOURCE_LABELS]
                 + [TRANSCRIPTIONS / lbl / ft for lbl in _SOURCE_LABELS]):
        _rmdir_if_empty(base / fs)


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


def _set_fiche_slug(etype: str, slug: str) -> bool:
    """Mettre le champ ``slug:`` de la fiche à jour après un renommage."""
    fiche = SYNTHESE / etype / slug / "fiche.md"
    if not fiche.is_file():
        return False
    content = fiche.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return False
    try:
        _, fm_text, body = content.split("---", 2)
    except ValueError:
        return False
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        return False
    fm["slug"] = slug
    new_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    fiche.write_text(f"---\n{new_fm}\n---{body}", encoding="utf-8")
    return True


def rename(from_entity: str, new_slug: str, dry_run: bool = True,
           db: TrackingDB | None = None) -> dict:
    """Renommer le slug d'une entité (même type) — ré-accentuation, correction.

    Déplace les dossiers (``~/Documents``, ``Synthèse``, ``Résumés``) via le
    **ledger** (réversible), met à jour la base (``rename_slug`` : entity_slug +
    segments de rel_path + valeurs de sujet) et le champ ``slug`` de la fiche.
    **Dry-run par défaut.**
    """
    require_connaissance_root()
    etype, old_slug = _split(from_entity)
    if old_slug == new_slug:
        return {"error": "old == new"}
    owns = db is None
    if db is None:
        db = TrackingDB()
    run_id = _ledger.new_run_id("entities-rename")
    docs_dir = DOCUMENTS_DIR / etype / old_slug
    n_docs = sum(1 for p in docs_dir.rglob("*") if p.is_file()) \
        if docs_dir.is_dir() else 0

    if dry_run:
        try:
            su = db._conn.execute(
                "SELECT COUNT(*) FROM doc_sujets WHERE sujet=?",
                (old_slug,)).fetchone()[0]
        finally:
            if owns:
                db.close()
        return {"dry_run": True, "from": from_entity, "new_slug": new_slug,
                "documents": n_docs, "sujet_refs": su}

    try:
        # 1. Documents → relocate_document (graphe complet + toutes les refs).
        relocated = _relocate_entity_docs(db, etype, old_slug, etype, new_slug, run_id)
        # 2. Le reste (fiche Synthèse déplacée, Courriels/Notes, orphelins).
        moved = _sweep_entity_rest(db, etype, old_slug, etype, new_slug, run_id)
        # 3. DB : entity_slug + valeurs de sujet (rel_path déjà fait par relocate).
        counts = db.rename_slug(etype, old_slug, new_slug)
        fiche_updated = _set_fiche_slug(etype, new_slug)
        _cleanup_entity_dirs(etype, old_slug)
    finally:
        if owns:
            db.close()
    return {"dry_run": False, "from": from_entity, "new_slug": new_slug,
            "documents_relocated": relocated, "files_moved": moved,
            "db": counts, "fiche_updated": fiche_updated, "ledger_run": run_id}


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

    from_fiche_dir = SYNTHESE / f_type / f_slug
    from_docs_dir = DOCUMENTS_DIR / f_type / f_slug
    n_docs_files = sum(1 for p in from_docs_dir.rglob("*") if p.is_file()) \
        if from_docs_dir.is_dir() else 0

    if dry_run:
        if owns:
            db.close()
        return {
            "dry_run": True,
            "from": from_entity, "into": into_entity,
            "docs_to_reassign": docs_n,
            "documents_to_move": n_docs_files,
            "aliases_to_add": alias_payload,
            "from_fiche_exists": from_fiche_dir.is_dir(),
        }

    reassigned = 0
    try:
        with db.transaction():
            reassigned = db.reassign_entity(f_type, f_slug, i_type, i_slug,
                                            into_name, commit=False)
        added = _add_aliases(i_type, i_slug, alias_payload)
        # Documents → relocate_document vers l'entité gardée (graphe + refs).
        docs_moved = _relocate_entity_docs(db, f_type, f_slug, i_type, i_slug, run_id)
        # Le reste : Courriels/Notes + orphelins déplacés ; fiche perdante → corbeille.
        moved = _sweep_entity_rest(db, f_type, f_slug, i_type, i_slug, run_id,
                                   trash_fiche=True)
        trashed = from_fiche_dir.is_dir()
        _cleanup_entity_dirs(f_type, f_slug)
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
