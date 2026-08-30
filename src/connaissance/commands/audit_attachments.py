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
import sys

import re
import shutil
from pathlib import Path

import unicodedata
from pathlib import Path as pathlib_Path

from connaissance.core import ledger as _ledger
from connaissance.core.companions import (ANNOTATIONS_SUFFIX,
                                          ATTACHMENTS_DIR,
                                          companion_moves,
                                          referenced_attachments)
from connaissance.core.paths import BASE_PATH
from connaissance.core.schemas import AuditRepairAttachments
from connaissance.core.tracking import TrackingDB

TRANSCRIPTIONS_DOCS = BASE_PATH / "Connaissance" / "Transcriptions" / "Documents"
CENTRAL_ATT = TRANSCRIPTIONS_DOCS / "Attachments"
# Les compagnons existent pour les trois sources, pas seulement les documents.
TRANSCRIPTIONS = BASE_PATH / "Connaissance" / "Transcriptions"
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


def repair(dry_run: bool = False) -> AuditRepairAttachments:
    stats: AuditRepairAttachments = {"scanned": 0, "repaired": 0,
                                     "missing": 0, "already_ok": 0}
    if not TRANSCRIPTIONS_DOCS.exists():
        print(f"✗ Pas de dossier {TRANSCRIPTIONS_DOCS}", file=sys.stderr)

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
                print(f"  [dry-run] {rel_src} → {rel_dst}/", file=sys.stderr)

            else:
                dst_att_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(source_file), str(local_file))
                print(f"  ✓ {rel_src} → {rel_dst}/", file=sys.stderr)

            stats["repaired"] += 1

    return stats


# ---------------------------------------------------------------------------
# Réunion des compagnons orphelins (JSON d'annotations + images)
# ---------------------------------------------------------------------------

def _chaine_de_moves(db) -> dict[str, str]:
    """``ancien chemin absolu -> nouveau``, d'après le ledger.

    Un `.md` a pu être déplacé plusieurs fois (classify, puis fusion
    d'entités, puis organize) : c'est une chaîne, pas un saut unique.
    """
    chaine: dict[str, str] = {}
    for old, new in db._conn.execute(
            """SELECT old_path, new_path FROM file_ledger
               WHERE op='move' AND status='applied'
                 AND old_path IS NOT NULL AND new_path IS NOT NULL
               ORDER BY id"""):
        chaine[unicodedata.normalize("NFC", old)] = \
            unicodedata.normalize("NFC", new)
    return chaine


def _suivre(chaine: dict[str, str], depart: str) -> str:
    """Dernier maillon de la chaîne. Borné : un cycle (A→B→A, possible après
    un revert partiel) ferait autrement boucler la réparation à l'infini."""
    vus: set[str] = set()
    p = depart
    while p in chaine and p not in vus:
        vus.add(p)
        p = chaine[p]
    return p


def reunir_orphelins(dry_run: bool = True, db=None) -> dict:
    """Ramener chaque JSON d'annotations orphelin auprès de son `.md`.

    Un JSON est orphelin quand le `.md` qui portait son nom n'est plus dans
    son dossier — le `.md` a été déplacé sans lui (cf. la régression du
    2026-08-30 : `relocate_document` ignorait les compagnons). Le NOM du
    JSON encode le stem d'alors ; le **ledger**, lui, sait où ce `.md` est
    parti. C'est la seule clé fiable : la DB, elle, ne garde que la position
    actuelle et a perdu le lien avec l'emplacement d'origine.

    Les images d'``Attachments/`` suivent via :func:`companion_moves` — la
    même règle que celle appliquée aux déplacements normaux, pour que
    réparer et prévenir ne puissent pas diverger.
    """
    owns = db is None
    if owns:
        db = TrackingDB()
    out = {"orphelins": 0, "reunis": 0, "irrecuperables": 0,
           "fichiers_deplaces": 0, "fichiers_copies": 0,
           "dry_run": dry_run, "exemples": []}
    try:
        chaine = _chaine_de_moves(db)
        run_id = _ledger.new_run_id("reunir-compagnons") if not dry_run else ""
        for js in sorted(TRANSCRIPTIONS.rglob("*" + ANNOTATIONS_SUFFIX)):
            base = js.name[: -len(ANNOTATIONS_SUFFIX)]
            md_ici = js.parent / (base + ".md")
            if md_ici.exists():
                continue                      # compagnon déjà bien placé
            out["orphelins"] += 1
            md_final = pathlib_Path(_suivre(
                chaine, unicodedata.normalize("NFC", str(md_ici))))
            if md_final == md_ici or not md_final.is_file():
                out["irrecuperables"] += 1
                continue
            moves = companion_moves(md_ici, md_final)
            if not moves:
                out["irrecuperables"] += 1
                continue
            out["reunis"] += 1
            if len(out["exemples"]) < 5:
                out["exemples"].append({
                    "json": str(js.relative_to(TRANSCRIPTIONS)),
                    "md": str(md_final.relative_to(TRANSCRIPTIONS)),
                    "fichiers": len(moves),
                })
            for c_src, c_dst, partage in moves:
                if partage:
                    out["fichiers_copies"] += 1
                else:
                    out["fichiers_deplaces"] += 1
                if dry_run:
                    continue
                c_dst.parent.mkdir(parents=True, exist_ok=True)
                if partage:
                    shutil.copy2(str(c_src), str(c_dst))
                else:
                    _ledger.safe_move(db, c_src, c_dst,
                                      "réunion compagnon orphelin", run_id)
        if not dry_run and out["reunis"]:
            out["ledger_run"] = run_id
    finally:
        if owns:
            db.close()
    return out


def _chaine_inverse(db) -> dict[str, list[str]]:
    """``nouveau chemin -> anciens``. Un `.md` peut avoir plusieurs
    emplacements antérieurs (classify puis fusion d'entités) : on les garde
    tous, du plus récent au plus ancien."""
    inv: dict[str, list[str]] = {}
    for old, new in db._conn.execute(
            """SELECT old_path, new_path FROM file_ledger
               WHERE op='move' AND status='applied'
                 AND old_path IS NOT NULL AND new_path IS NOT NULL
               ORDER BY id DESC"""):
        inv.setdefault(unicodedata.normalize("NFC", new), []).append(
            unicodedata.normalize("NFC", old))
    return inv


def _anciens_dossiers(inv: dict[str, list[str]], md: pathlib_Path,
                      max_noeuds: int = 60) -> list[pathlib_Path]:
    """Dossiers qu'un `.md` a occupés avant d'arriver là où il est.

    L'histoire remonte en ARBRE, pas en ligne : plusieurs `.md` d'origine
    différente peuvent avoir convergé vers un même chemin (uniquification,
    fusion d'entités — quatre cartes RAMQ distinctes sont arrivées sur
    « carte-d-assurance-maladie.md »). Ne suivre que le prédécesseur le plus
    récent ferait manquer les images restées dans les autres branches.
    Parcours en largeur, borné : les branches proches d'abord.
    """
    out: list[pathlib_Path] = []
    vus = {unicodedata.normalize("NFC", str(md))}
    file: list[str] = [unicodedata.normalize("NFC", str(md))]
    while file and len(vus) < max_noeuds:
        courant = file.pop(0)
        for precedent in inv.get(courant, []):
            if precedent in vus:
                continue
            vus.add(precedent)
            file.append(precedent)
            out.append(pathlib_Path(precedent).parent)
    return out


# Un nom de la forme `<uuid>.<ext>` est unique PAR CONSTRUCTION : le chercher
# dans tout l'arbre ne peut pas recoller la mauvaise image. Aucun autre nom ne
# donne cette garantie — « img0.jpg » est produit à l'identique par chaque
# document, et un homonyme rebranché en silence serait indétectable.
_UUID_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.", re.I)


def _index_par_nom() -> dict[str, list[pathlib_Path]]:
    """Tous les fichiers d'``Attachments/``, groupés par nom."""
    idx: dict[str, list[pathlib_Path]] = {}
    for f in TRANSCRIPTIONS.rglob("*"):
        if f.is_file() and ATTACHMENTS_DIR in f.parts:
            idx.setdefault(f.name, []).append(f)
    return idx


def _porteur_unique_si_uuid(nom: str,
                            porteurs: dict[str, list[pathlib_Path]]
                            ) -> pathlib_Path | None:
    """Dernier recours, volontairement étroit : seulement pour un nom UUID, et
    seulement s'il n'existe qu'UN porteur. Deux porteurs = ambiguïté, on
    s'abstient plutôt que de choisir."""
    if not _UUID_NAME.match(nom):
        return None
    trouves = porteurs.get(nom) or []
    return trouves[0] if len(trouves) == 1 else None


def rapatrier_images(dry_run: bool = True, db=None) -> dict:
    """Ramener les images qu'un `.md` cite sans les avoir sous la main.

    Symétrique de :func:`reunir_orphelins` : là on partait du compagnon resté
    derrière, ici on part du `.md` dont le lien est mort. Les deux sont
    nécessaires — une image reste orpheline sans JSON pour la signaler, et
    aucun JSON orphelin ne pointe alors vers elle.

    L'image est cherchée dans les dossiers que CE `.md` a réellement occupés,
    remontés par le ledger — pas par son nom dans tout l'arbre. Deux
    documents scannés le même jour peuvent porter des noms d'image
    identiques ; prendre le premier homonyme venu recollerait la mauvaise
    image au bon document, en silence et sans rien casser de visible.
    """
    owns = db is None
    if owns:
        db = TrackingDB()
    out = {"md_examines": 0, "refs_cassees": 0, "rapatriees": 0,
           "introuvables": 0, "copiees": 0, "par_uuid": 0, "dry_run": dry_run}
    try:
        inv = _chaine_inverse(db)
        porteurs = _index_par_nom()
        run_id = _ledger.new_run_id("rapatrier-images") if not dry_run else ""
        for md in sorted(TRANSCRIPTIONS.rglob("*.md")):
            if ATTACHMENTS_DIR in md.parts:
                continue
            manquantes = {n for n in referenced_attachments(md)
                          if not (md.parent / ATTACHMENTS_DIR / n).exists()}
            if not manquantes:
                continue
            out["md_examines"] += 1
            out["refs_cassees"] += len(manquantes)
            dossiers = _anciens_dossiers(inv, md)
            for nom in sorted(manquantes):
                source = next(
                    (d / ATTACHMENTS_DIR / nom for d in dossiers
                     if (d / ATTACHMENTS_DIR / nom).is_file()), None)
                if source is None:
                    source = _porteur_unique_si_uuid(nom, porteurs)
                    if source is not None:
                        out["par_uuid"] += 1
                if source is None:
                    out["introuvables"] += 1
                    continue
                cible = md.parent / ATTACHMENTS_DIR / nom
                # Un `.md` resté dans le dossier d'origine peut citer la même
                # image : on duplique plutôt que de casser son rendu.
                partage = any(
                    autre != md and nom in referenced_attachments(autre)
                    for autre in source.parent.parent.glob("*.md"))
                out["copiees" if partage else "rapatriees"] += 1
                if dry_run:
                    continue
                cible.parent.mkdir(parents=True, exist_ok=True)
                if partage:
                    shutil.copy2(str(source), str(cible))
                else:
                    _ledger.safe_move(db, source, cible,
                                      "rapatriement image", run_id)
        if not dry_run and (out["rapatriees"] or out["copiees"]):
            out["ledger_run"] = run_id
    finally:
        if owns:
            db.close()
    return out
