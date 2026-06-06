"""Module commands/sujets : vue virtuelle « - Sujets » + export à la demande.

Modèle de sujets acté : un document est classé **physiquement par ENTITÉ**
(`organismes/personnes/divers`) et porte un **sujet** (champ
`doc_classification.sujet` — source de vérité unique, pas de frontmatter sur un
PDF brut). Les sujets ne sont PAS une arborescence physique : une **vue unique
de symlinks** ``~/Documents/- Sujets/<sujet>/`` les rassemble, régénérable à
volonté, et **remplace** ``- Par catégorie/`` (la catégorie devient un sujet
grossier). Virtuel par défaut ; le « physique » se fait à la demande via
``sujet export`` (copie/zip réel, ex. envoi au comptable).

Expose :
- ``view(apply=False, clear=False) -> SujetView`` : (re)génère la vue symlink.
- ``export(name, dest=None, as_zip=False) -> SujetExport`` : matérialise un sujet.
- ``list_sujets() -> SujetList`` : sujets + compteurs.
"""
from __future__ import annotations

import shutil
import unicodedata
from pathlib import Path

from connaissance.core.paths import DOCUMENTS_DIR, require_paths
from connaissance.core.tracking import TrackingDB

SUJETS_VIEW_NAME = "- Sujets"


def _slug_dir(sujet: str) -> str:
    """Nom de dossier sûr pour un sujet (pas de séparateur de chemin)."""
    return unicodedata.normalize("NFC", sujet).replace("/", "-").strip() or "divers"


def _resolve_source(rel_path: str) -> Path | None:
    """Chemin physique courant d'un document classé, ou None s'il a disparu."""
    p = DOCUMENTS_DIR / rel_path
    return p if p.exists() else None


def list_sujets(db: TrackingDB | None = None) -> dict:
    """Lister les sujets et le nombre de documents (schema SujetList)."""
    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        rows = db.sujet_memberships()
    finally:
        if owns:
            db.close()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["sujet"]] = counts.get(r["sujet"], 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return {"sujets": ordered, "total_sujets": len(ordered),
            "total_documents": sum(counts.values())}


def view(apply: bool = False, clear: bool = False,
         db: TrackingDB | None = None) -> dict:
    """Vue navigable par SUJET en raccourcis (symlinks), depuis les
    appartenances **multi-sujet** ``doc_sujets`` + ``doc_classification.sujet``
    (schema SujetView).

    Un document appartenant à N sujets apparaît sous N dossiers (éventail) —
    c'est ce qui remplace le multi-classement physique : le fichier vit une fois,
    se voit partout. Sources : le sujet primaire (classify) + les contextes
    capturés par la dédup consciente.

    - défaut : **dry-run** — renvoie la répartition sans rien écrire.
    - ``apply`` : (re)construit ``~/Documents/- Sujets/`` à neuf (idempotent).
    - ``clear`` : supprime la vue (réversible — aucun fichier source touché).

    Le préfixe « - » exclut le dossier du scan. Les raccourcis pointent le vrai
    fichier à son emplacement courant ; régénérer après tout déplacement.
    """
    require_paths(DOCUMENTS_DIR, context="sujet view")
    view_dir = DOCUMENTS_DIR / SUJETS_VIEW_NAME

    if clear:
        existed = view_dir.exists()
        if existed:
            shutil.rmtree(view_dir)
        return {"cleared": True, "existed": existed, "view_dir": str(view_dir)}

    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        rows = db.sujet_memberships()
    finally:
        if owns:
            db.close()

    by_sujet: dict[str, list[tuple[str, Path]]] = {}
    missing_source = 0
    for r in rows:
        src = _resolve_source(r["rel_path"])
        if src is None:
            missing_source += 1
            continue
        # Nom de lien = nom du fichier (sans séparateur de chemin).
        label = src.name.replace("/", "-")
        by_sujet.setdefault(r["sujet"], []).append((label, src))

    counts = {s: len(v) for s, v in
              sorted(by_sujet.items(), key=lambda kv: -len(kv[1]))}

    links_created = 0
    if apply:
        if view_dir.exists():
            shutil.rmtree(view_dir)
        view_dir.mkdir(parents=True)
        for sujet, items in by_sujet.items():
            sdir = view_dir / _slug_dir(sujet)
            sdir.mkdir(parents=True, exist_ok=True)
            for label, src in items:
                link = sdir / label
                i = 1
                while link.exists() or link.is_symlink():
                    p = Path(label)
                    link = sdir / f"{p.stem} ({i}){p.suffix}"
                    i += 1
                link.symlink_to(src)
                links_created += 1

    return {
        "sujets": counts,
        "total": sum(counts.values()),
        "missing_source": missing_source,
        "applied": apply,
        "links_created": links_created,
        "view_dir": str(view_dir),
    }


def export(name: str, dest: str | None = None, as_zip: bool = False,
           db: TrackingDB | None = None) -> dict:
    """Matérialiser un sujet : **copier** (ou zipper) ses documents vers un
    dossier réel, à la demande (schema SujetExport).

    Pour le cas « envoi au comptable » : pas de dossier physique permanent, une
    copie ponctuelle. ``dest`` par défaut : ``~/Documents/- Sujets-export/<nom>``
    (préfixe « - » → hors scan). ``as_zip`` produit un .zip à la place.
    Ne touche jamais les sources (copie pure, hors ledger).
    """
    require_paths(DOCUMENTS_DIR, context="sujet export")
    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        rows = [r for r in db.sujet_memberships()
                if unicodedata.normalize("NFC", r["sujet"]) ==
                   unicodedata.normalize("NFC", name)]
    finally:
        if owns:
            db.close()

    sources: list[Path] = []
    missing = 0
    for r in rows:
        src = _resolve_source(r["rel_path"])
        if src is None:
            missing += 1
            continue
        sources.append(src)

    out_base = (Path(dest).expanduser() if dest
                else DOCUMENTS_DIR / "- Sujets-export" / _slug_dir(name))

    if not sources:
        return {"sujet": name, "exported": 0, "missing_source": missing,
                "dest": str(out_base), "zip": as_zip}

    staging = out_base if not as_zip else out_base
    staging.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sources:
        target = staging / src.name
        i = 1
        while target.exists():
            target = staging / f"{src.stem} ({i}){src.suffix}"
            i += 1
        shutil.copy2(str(src), str(target))
        copied += 1

    result = {"sujet": name, "exported": copied, "missing_source": missing,
              "dest": str(staging), "zip": False}
    if as_zip:
        archive = shutil.make_archive(str(out_base), "zip", root_dir=str(staging))
        shutil.rmtree(staging)
        result["dest"] = archive
        result["zip"] = True
    return result
