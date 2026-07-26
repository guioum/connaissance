"""Module commands/sujets : vue virtuelle « Sujets » + export à la demande.

Modèle de sujets acté : un document est classé **physiquement par ENTITÉ**
(`organismes/personnes/divers`) et porte un **sujet** (champ
`doc_classification.sujet` — source de vérité unique, pas de frontmatter sur un
PDF brut). Les sujets ne sont PAS une arborescence physique : une **vue unique
de symlinks** ``~/Connaissance/Vues/Sujets/<sujet>/`` les rassemble, régénérable à
volonté. **Axe complémentaire** de ``- Catégories/`` (taxonomie fixe, 1/doc) :
les sujets sont des thèmes libres, N par doc ; les deux vues coexistent.
Virtuel par défaut ; le « physique » se fait à la demande via ``sujet export``
(copie/zip réel, ex. envoi au comptable).

Expose :
- ``view(apply=False, clear=False) -> SujetView`` : (re)génère la vue symlink.
- ``export(name, dest=None, as_zip=False) -> SujetExport`` : matérialise un sujet.
- ``list_sujets() -> SujetList`` : sujets + compteurs.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path

from connaissance.core.paths import (BASE_PATH, DOCUMENTS_DIR, VIEWS_ROOT,
                                     require_paths, symlink_avec_mtime)
from connaissance.core.schemas import SujetExport, SujetList, SujetView
from connaissance.core.tracking import TrackingDB

SUJETS_VIEW_NAME = "Sujets"   # sous VIEWS_ROOT (hors ~/Documents/iCloud)


def _slug_dir(sujet: str) -> str:
    """Nom de dossier sûr pour un sujet (pas de séparateur de chemin)."""
    return unicodedata.normalize("NFC", sujet).replace("/", "-").strip() or "divers"


def _resolve_source(rel_path: str) -> Path | None:
    """Chemin physique courant d'un document classé, ou None s'il a disparu."""
    p = DOCUMENTS_DIR / rel_path
    return p if p.exists() else None


def _sans_accents(s: str) -> str:
    d = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in d if not unicodedata.combining(c))


# Personnes repérables dans les chemins d'origine des packages (« 2021 Impôts
# Mélanie », « Guillaume - Consultation », « Impôts Famille 2023 »).
_PERSONNES_RE = re.compile(r"(melanie|guillaume|famille)")


def _ventilation_par_document(db: TrackingDB, sujet: str,
                              rels: list[str]) -> dict[str, dict]:
    """Ventilation par document pour un sujet regroupé par année.

    Pour chaque document : ``annee`` (l'année **du dossier d'origine** via la
    chaîne ledger — « Package Impôts 2019/ » d'avant le reclassement — fait
    autorité, un avis de cotisation daté 2025 appartenant au package 2024 ;
    repli sur l'année de la date de classification) ; ``personne`` (mélanie /
    guillaume / famille si le chemin d'origine ou le nom la révèle) ;
    ``entite`` (nom lisible du registre, sinon slug).
    """
    # Chaîne des déplacements : new → origine première (suivie de proche en proche).
    origine: dict[str, str] = {}
    for r in db._conn.execute(
            "SELECT old_path, new_path FROM file_ledger "
            "WHERE status='applied' ORDER BY id"):
        o = unicodedata.normalize("NFC", r["old_path"])
        n = unicodedata.normalize("NFC", r["new_path"])
        origine[n] = origine.pop(o, o)
    mot = _sans_accents(sujet).rstrip("s")
    motif = re.compile(
        rf"{re.escape(mot)}s?[ -]?((?:19|20)\d{{2}})"
        rf"|((?:19|20)\d{{2}})[ -]?{re.escape(mot)}s?")
    fiches = {r["rel_path"]: dict(r) for r in db._conn.execute(
        "SELECT rel_path, date, entity_slug, entity_type "
        "FROM doc_classification WHERE sujet = ?", (sujet,))}
    noms = {r["slug"]: r["name"] for r in db._conn.execute(
        "SELECT slug, name FROM entities")}
    out: dict[str, dict] = {}
    # Ne chercher année/personne que dans la partie RELATIVE au home : le
    # chemin absolu contient le nom d'utilisateur (« /Users/guillaume… ») qui
    # matcherait _PERSONNES_RE pour TOUS les documents.
    prefixe_home = _sans_accents(unicodedata.normalize("NFC", str(BASE_PATH)))
    for rel in rels:
        abs_cur = unicodedata.normalize("NFC", str(BASE_PATH / "Documents" / rel))
        chemin_origine = _sans_accents(origine.get(abs_cur, abs_cur))
        if chemin_origine.startswith(prefixe_home):
            chemin_origine = chemin_origine[len(prefixe_home):]
        v: dict = {}
        m = motif.search(chemin_origine)
        if m:
            v["annee"] = m.group(1) or m.group(2)
        fiche = fiches.get(rel) or {}
        if "annee" not in v:
            d = fiche.get("date") or ""
            if re.match(r"^(?:19|20)\d{2}", d):
                v["annee"] = d[:4]
        p = _PERSONNES_RE.search(chemin_origine)
        if p:
            v["personne"] = {"melanie": "mélanie"}.get(p.group(1), p.group(1))
        slug = fiche.get("entity_slug")
        if slug:
            v["entite"] = noms.get(slug) or slug
        out[rel] = v
    return out


def list_sujets(db: TrackingDB | None = None) -> SujetList:
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
         par_annee: list[str] | None = None,
         db: TrackingDB | None = None) -> SujetView:
    """Vue navigable par SUJET en raccourcis (symlinks), depuis les
    appartenances **multi-sujet** ``doc_sujets`` + ``doc_classification.sujet``
    (schema SujetView).

    Un document appartenant à N sujets apparaît sous N dossiers (éventail) —
    c'est ce qui remplace le multi-classement physique : le fichier vit une fois,
    se voit partout. Sources : le sujet primaire (classify) + les contextes
    capturés par la dédup consciente.

    - défaut : **dry-run** — renvoie la répartition sans rien écrire.
    - ``apply`` : (re)construit ``~/Connaissance/Vues/Sujets/`` à neuf (idempotent).
    - ``clear`` : supprime la vue (réversible — aucun fichier source touché).
    - ``par_annee`` : sujets à ventiler en sous-dossiers ``<sujet>/<AAAA>/``
      (ex. ``["impots"]`` reconstitue les packages d'impôts par année —
      l'année du dossier d'ORIGINE via la chaîne ledger prime sur la date
      du document ; sans année connue, le lien reste à la racine du sujet).

    Sous ``~/Connaissance/Vues/`` (hors ~/Documents : ni pollution iCloud, ni
    scan). Les raccourcis pointent le vrai fichier à son emplacement courant ;
    régénérer après tout déplacement.
    """
    require_paths(DOCUMENTS_DIR, context="sujet view")
    view_dir = VIEWS_ROOT / SUJETS_VIEW_NAME

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

        by_sujet: dict[str, list[tuple[str, str, Path]]] = {}
        missing_source = 0
        for r in rows:
            src = _resolve_source(r["rel_path"])
            if src is None:
                missing_source += 1
                continue
            # Nom de lien = nom du fichier (sans séparateur de chemin).
            label = src.name.replace("/", "-")
            by_sujet.setdefault(r["sujet"], []).append(
                (r["rel_path"], label, src))

        # Ventilation par année des sujets demandés (rel → année/personne/entité).
        annees: dict[str, dict[str, dict]] = {}
        for sujet in (par_annee or []):
            if sujet in by_sujet:
                annees[sujet] = _ventilation_par_document(
                    db, sujet, [rel for rel, _, _ in by_sujet[sujet]])
    finally:
        if owns:
            db.close()

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
            for rel, label, src in items:
                v = annees.get(sujet, {}).get(rel) or {}
                annee = v.get("annee")
                ldir = sdir / annee if annee else sdir
                ldir.mkdir(parents=True, exist_ok=True)
                if sujet in (par_annee or []):
                    # Nom porteur de contexte : la provenance (personne,
                    # entité) ne se lit plus dans le chemin plat de la vue.
                    prefixe = " · ".join(
                        x for x in (v.get("personne"), v.get("entite")) if x)
                    if prefixe:
                        label = f"{prefixe} · {label}"
                link = ldir / label
                i = 1
                while link.exists() or link.is_symlink():
                    p = Path(label)
                    link = ldir / f"{p.stem} ({i}){p.suffix}"
                    i += 1
                symlink_avec_mtime(link, src)
                links_created += 1

    out: SujetView = {
        "sujets": counts,
        "total": sum(counts.values()),
        "missing_source": missing_source,
        "applied": apply,
        "links_created": links_created,
        "view_dir": str(view_dir),
    }
    if annees:
        repartition: dict[str, dict[str, int]] = {}
        for sujet, mapping in annees.items():
            c: dict[str, int] = {}
            for v in mapping.values():
                if v.get("annee"):
                    c[v["annee"]] = c.get(v["annee"], 0) + 1
            repartition[sujet] = dict(sorted(c.items()))
        out["par_annee"] = repartition
    return out


def export(name: str, dest: str | None = None, as_zip: bool = False,
           db: TrackingDB | None = None) -> SujetExport:
    """Matérialiser un sujet : **copier** (ou zipper) ses documents vers un
    dossier réel, à la demande (schema SujetExport).

    Pour le cas « envoi au comptable » : pas de dossier physique permanent, une
    copie ponctuelle. ``dest`` par défaut : ``~/Connaissance/Vues/Sujets-export/<nom>``
    (hors ~/Documents). ``as_zip`` produit un .zip à la place.
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
                else VIEWS_ROOT / "Sujets-export" / _slug_dir(name))

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

    result: SujetExport = {"sujet": name, "exported": copied,
                           "missing_source": missing,
                           "dest": str(staging), "zip": False}
    if as_zip:
        archive = shutil.make_archive(str(out_base), "zip", root_dir=str(staging))
        shutil.rmtree(staging)
        result["dest"] = archive
        result["zip"] = True
    return result
