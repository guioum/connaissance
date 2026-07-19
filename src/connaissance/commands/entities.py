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
from connaissance.core import frontmatter as _frontmatter
from connaissance.core import resolution as _resolution
from connaissance.core import relocate as _reloc
from connaissance.core.schemas import (EntitiesCandidates, EntitiesMerge,
                                       EntitiesRename)
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
    except OSError:
        return None
    if not content.startswith("---"):
        return {}
    return _frontmatter.parse_frontmatter(content) or {}


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


def candidates(db: TrackingDB | None = None) -> EntitiesCandidates:
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
    parts = _frontmatter.split_frontmatter(content)
    if parts is None:
        return []
    fm_text, body = parts
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
    _frontmatter.write_frontmatter(fiche, fm, body)
    _resolution.invalidate_alias_cache()
    return added


def _set_fiche_slug(etype: str, slug: str) -> bool:
    """Mettre le champ ``slug:`` de la fiche à jour après un renommage."""
    fiche = SYNTHESE / etype / slug / "fiche.md"
    if not fiche.is_file():
        return False
    content = fiche.read_text(encoding="utf-8")
    parts = _frontmatter.split_frontmatter(content)
    if parts is None:
        return False
    fm_text, body = parts
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        return False
    fm["slug"] = slug
    _frontmatter.write_frontmatter(fiche, fm, body)
    _resolution.invalidate_alias_cache()
    return True


def rename(from_entity: str, new_slug: str, dry_run: bool = True,
           db: TrackingDB | None = None) -> EntitiesRename:
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
          db: TrackingDB | None = None) -> EntitiesMerge:
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
            # Registre `entities` : fusionner les lignes (nom+aliases du perdant
            # → gardé, doc_count additionné, perdant supprimé).
            db.merge_entity_rows(f_type, f_slug, i_slug, into_type=i_type,
                                 commit=False)
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


# --- Registre `entities` en BD : seed + liste -------------------------------

import re as _re
import collections as _coll
from connaissance.core.resolution import construire_slug as _cslug

# Consolidations curées HAUTE CONFIANCE (canonique → variantes/aliases) — seed
# du registre. Évite la fragmentation des entités récurrentes vue dans l'ancien
# run (BNC ×3, Manuvie ×2…). BNC ≠ BDC. Éditable ; complété par `entities merge`.
_SEED_CURATED = {
    "organismes": [
        ("Banque Nationale", ["BNC", "Banque Nationale du Canada",
                               "Banque Nationale Épargne et Placements"]),
        ("Banque de développement du Canada", ["BDC",
                                               "Business Development Bank of Canada"]),
        ("Manuvie", ["Manulife", "La Compagnie d'Assurance-Vie Manufacturers",
                     "Manufacturers Life Insurance Co."]),
        ("Financière Sun Life", ["Sun Life"]),
        ("FMRQ", ["Fédération des Médecins Résidents du Québec"]),
        ("SAAQ", ["Société de l'assurance automobile du Québec"]),
        ("Agence du revenu du Canada", ["ARC"]),
        ("Ordre des ingénieurs du Québec", ["OIQ"]),
        ("Amazon Web Services", ["AWS", "Amazon Web Services Canada"]),
        ("Google", ["Google LLC"]),
    ],
    "personnes": [
        ("Guillaume Monteillet", ["Guillaume"]),
        ("Mélanie Bazin", ["Melanie Bazin"]),
        ("Arthur Monteillet", ["Arthur"]),
    ],
}


def _fm_field(txt: str, key: str):
    m = _re.search(rf"^{key}:\s*(.+?)\s*$", txt, _re.M)
    return m.group(1).strip().strip('"').strip("'") if m else None


import unicodedata as _ud
_LEGAL = _re.compile(r"\b(inc|pbc|llc|sas|sa|ltee|ltée|ltd|co|corp|pllc|enr)\b")


def _group_key(name: str) -> str:
    """Clé de regroupement tolérante : minuscules, sans accents, sans
    parenthèses ni suffixes légaux (Inc./PBC/LLC…) ni ponctuation. Rapproche
    « Anthropic, PBC »/« Anthropic » et « …(BDC) »/« BDC ». Ne touche pas au
    nom canonique stocké."""
    s = "".join(c for c in _ud.normalize("NFD", name.lower())
                if _ud.category(c) != "Mn")
    s = _re.sub(r"\([^)]*\)", " ", s)
    s = _re.sub(r"[,.\-—]", " ", s)
    s = _LEGAL.sub(" ", s)
    return _re.sub(r"\s+", " ", s).strip()


def seed(from_backup: str | None = None, db: TrackingDB | None = None) -> dict:
    """Peupler le registre `entities` (idempotent). Base : dossiers rangés +
    consolidations curées. Avec ``--from-backup <dir>`` : enrichit depuis les
    `entity_name` des résumés du backup (regroupés par slug, canonique = le plus
    fréquent), sans sur-fusionner. À réviser ensuite via `entities list`/`merge`."""
    owns = db is None
    if db is None:
        db = TrackingDB()
    n = 0
    try:
        # 1. Consolidations curées (haute confiance).
        for etype, items in _SEED_CURATED.items():
            for name, aliases in items:
                db.upsert_entity(etype, _cslug(name), name, aliases,
                                 status="seed", commit=False)
                n += 1
        # 2. Dossiers rangés (canoniques propres), si absents.
        for etype in ("organismes", "personnes"):
            d = DOCUMENTS_DIR / etype
            if not d.is_dir():
                continue
            for c in sorted(d.iterdir()):
                if c.is_dir() and not c.name.startswith("."):
                    name = c.name.replace("-", " ").replace("_", " ").strip()
                    db.upsert_entity(etype, c.name, name.title(),
                                     status="seed", commit=False)
                    n += 1
        # 3. Enrichissement depuis un backup de résumés (optionnel).
        enriched = 0
        if from_backup:
            from pathlib import Path
            rdir = Path(from_backup).expanduser()
            if rdir.name != "Résumés" and (rdir / "Résumés").is_dir():
                rdir = rdir / "Résumés"
            # Radicaux de TOUTES les entités déjà seedées (curées + dossiers) :
            # une variante du backup qui matche est ajoutée en alias, pas créée.
            curated_keys = {}
            for e in db.all_entities():
                for v in [e["name"]] + (e["aliases"] or []):
                    curated_keys[(e["type"], _group_key(v))] = (
                        e["type"], e["slug"], e["name"])
            # Regrouper le backup par clé tolérante (rapproche les variantes).
            groups: dict = {}
            for f in rdir.rglob("*.md"):
                t = f.read_text(encoding="utf-8", errors="replace")[:800]
                nm = _fm_field(t, "entity_name"); et = _fm_field(t, "entity_type")
                if nm and et in ("organismes", "personnes"):
                    groups.setdefault((et, _group_key(nm)), _coll.Counter())[nm] += 1
            for (et, gkey), names in groups.items():
                if not gkey:
                    continue
                cur = curated_keys.get((et, gkey))
                if cur:   # variante d'une entité curée → ajouter en alias
                    db.upsert_entity(cur[0], cur[1], cur[2], list(names),
                                     status="seed", commit=False)
                    continue
                # Canonique = le nom le plus PROPRE du groupe (sans parenthèse ni
                # virgule = sans suffixe légal/qualificatif), puis le plus court,
                # puis le plus fréquent → slug stable, variantes en alias.
                canon = min(names, key=lambda nm: (
                    1 if ("(" in nm or "," in nm) else 0, len(nm), -names[nm]))
                aliases = [nm for nm in names if nm != canon]
                db.upsert_entity(et, _cslug(canon), canon, aliases,
                                 status="seed", commit=False)
                enriched += 1
        db._conn.commit()
    finally:
        if owns:
            db.close()
    return {"seeded": n, "enriched_from_backup": enriched if from_backup else 0}


def list_registry(db: TrackingDB | None = None) -> dict:
    """Lister le registre `entities` (canonique + aliases + compteur)."""
    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        ents = db.all_entities()
    finally:
        if owns:
            db.close()
    return {
        "total": len(ents),
        "by_type": dict(_coll.Counter(e["type"] for e in ents)),
        "entities": [{"type": e["type"], "slug": e["slug"], "name": e["name"],
                      "aliases": e["aliases"], "doc_count": e["doc_count"]}
                     for e in ents],
    }
