"""core/relocate : déplacer un document ET tout son graphe, de façon cohérente.

Un document a jusqu'à **trois représentations** partageant ``<type>/<slug>/<stem>`` :
le fichier source (``~/Documents``), sa **transcription** et son **résumé**
(``~/Connaissance/{Transcriptions,Résumés}/Documents/``). Et plusieurs
**références** y pointent : le champ ``source`` du résumé (→ transcription), les
tables ``doc_classification``/``doc_signals``/``doc_sujets`` (rel ~/Documents),
``text_simhash`` (rel transcription), ``files``.

Historiquement chaque opération du flow (classify/organize/entities) ne déplaçait
qu'un sous-ensemble → dérive (transcriptions orphelines, ``source`` périmé).
``relocate_document`` centralise : il déplace les 3 représentations **via le
ledger** et met à jour **toutes** les références, en une transaction. La
transcription est localisée via le ``source`` du résumé (donc même orpheline
sous un ancien slug, elle est récupérée et réalignée).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from connaissance.core import ledger as _ledger
from connaissance.core.companions import companion_moves
from connaissance.core.frontmatter import (parse_frontmatter,
                                            split_frontmatter,
                                            write_frontmatter)
from connaissance.core.manifest_io import unique_dest
from connaissance.core.paths import CONNAISSANCE_ROOT, DOCUMENTS_DIR

TRANSCR = CONNAISSANCE_ROOT / "Transcriptions" / "Documents"
RESUMES = CONNAISSANCE_ROOT / "Résumés" / "Documents"


def _read_fm(md: Path) -> dict | None:
    try:
        t = md.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_frontmatter(t)


def _set_fm_source(md: Path, new_source: str) -> None:
    """Mettre à jour le champ ``source`` du frontmatter d'un `.md` dérivé."""
    t = md.read_text(encoding="utf-8")
    parts = split_frontmatter(t)
    if parts is None:          # `.md` sans frontmatter : en créer un
        fm, body = {}, t
    else:
        fm = yaml.safe_load(parts[0]) or {}
        body = parts[1]
    fm["source"] = new_source
    write_frontmatter(md, fm, body)


def relocate_document(db, old_rel: str, new_rel: str, run_id: str,
                      *, dry_run: bool = False,
                      reason: str = "relocate") -> dict:
    """Déplacer un document (rel ~/Documents) et tout son graphe.

    Déplace source + transcription + résumé (ceux qui existent) via le ledger,
    met à jour ``source`` du résumé, ``relink_document`` (doc_*), ``text_simhash``
    et ``files``. Transaction atomique côté DB. Retourne le détail.
    ``reason`` préfixe le motif journalisé (« <reason> source », …).
    """
    src_old, src_new = DOCUMENTS_DIR / old_rel, DOCUMENTS_DIR / new_rel
    # Collision de SOURCE : la destination est déjà occupée par un AUTRE
    # fichier (fusion d'entités : « bulletin-de-paie.pdf » existe des deux
    # côtés) → uniquifier ici, et TOUT le graphe (miroirs, refs) suit le rel
    # uniquifié. Les appelants qui pré-uniquifient (classify apply) ne sont
    # pas affectés (destination libre). Cas homonyme APFS insensible à la
    # casse (même fichier) exclu.
    if src_old.exists() and src_old != src_new and src_new.exists():
        same = False
        try:
            same = src_old.samefile(src_new)
        except OSError:
            pass
        if not same:
            src_new = unique_dest(src_new)
            new_rel = str(src_new.relative_to(DOCUMENTS_DIR))
    # Transcriptions et résumés vivent au MIROIR COMPLET du rel
    # (``Transcriptions/Documents/<rel>.md``) — pour un doc déjà organisé le
    # rel EST ``<type>/<slug>/<stem>.ext``, pour un doc en vrac c'est son
    # chemin profond. Ne jamais tronquer aux deux premiers segments : c'est ce
    # qui laissait orphelines les transcriptions des docs en vrac au premier
    # ``classify apply`` (bug attrapé par le pilote du 2026-07-25).
    res_old = RESUMES / Path(old_rel).with_suffix(".md")
    res_new = RESUMES / Path(new_rel).with_suffix(".md")
    if res_old.is_file() and res_new.exists() and res_old != res_new:
        res_new = unique_dest(res_new)     # même collision de miroir (stem)

    # Transcription : suivre le `source` du résumé (récupère les orphelines
    # sous un ancien slug) ; sinon le miroir co-localisé du rel.
    tr_old = None
    fm = _read_fm(res_old) if res_old.is_file() else None
    if fm and fm.get("source"):
        cand = CONNAISSANCE_ROOT / fm["source"]
        tr_old = cand if cand.exists() else None
    if tr_old is None:
        cand = TRANSCR / Path(old_rel).with_suffix(".md")
        tr_old = cand if cand.exists() else None
    tr_new = TRANSCR / Path(new_rel).with_suffix(".md")
    # Collision de MIROIR : deux sources d'extensions différentes (scan .jpg +
    # .pdf du même document) peuvent viser le même stem → même `.md`. La
    # source est uniquifiée par l'appelant, pas ses miroirs : uniquifier ici
    # (constaté en réel tranche 2 : 3 refus d'écrasement, sources déjà
    # parties — le move disque n'est pas transactionnel).
    if tr_old is not None and tr_new.exists() and tr_old != tr_new:
        tr_new = unique_dest(tr_new)
    tr_new_rel = str(tr_new.relative_to(CONNAISSANCE_ROOT))

    # On ignore les déplacements src==dst : permet d'appeler relocate avec
    # old_rel == new_rel pour un simple **réalignement** (ex. transcription
    # orpheline ramenée à sa place), idempotent.
    moves = []   # (clé, src, dst)
    if src_old.exists() and src_old != src_new:
        moves.append(("source", src_old, src_new))
    if tr_old is not None and tr_old != tr_new:
        moves.append(("transcription", tr_old, tr_new))
    if res_old.is_file() and res_old != res_new:
        moves.append(("resume", res_old, res_new))

    if dry_run:
        n_comp = (len(companion_moves(tr_old, tr_new))
                  if tr_old is not None and tr_old != tr_new else 0)
        return {"dry_run": True, "old": old_rel, "new": new_rel,
                "moves": [k for k, _, _ in moves],
                "compagnons": n_comp,
                "transcription_found": tr_old is not None}

    # Compagnons de la transcription : le JSON d'annotations et les images
    # d'`Attachments/`. Ils sont désignés par des chemins RELATIFS au dossier
    # du `.md` — les laisser derrière ne casse pas le déplacement, ça casse
    # les liens, en silence. `classify` et `entities` passent tous deux par
    # ici : c'est leur absence de ce calcul qui a laissé 1 210 JSON orphelins
    # et 5 896 images non référencées au 2026-08-30.
    compagnons: list[tuple[Path, Path, bool]] = []
    if tr_old is not None and tr_old != tr_new:
        compagnons = companion_moves(tr_old, tr_new)

    done = []
    src_sha = None
    with db.transaction():
        for key, s, d in moves:
            entry = _ledger.safe_move(db, s, d, f"{reason} {key}", run_id,
                                      commit=False)
            if key == "source":
                src_sha = entry.get("sha256")
            done.append(key)
        for c_src, c_dst, partage in compagnons:
            c_dst.parent.mkdir(parents=True, exist_ok=True)
            if partage:
                # Un autre `.md` resté sur place cite encore cette image :
                # la déplacer casserait SON rendu. On duplique — quelques
                # kilo-octets valent mieux qu'un lien mort.
                shutil.copy2(str(c_src), str(c_dst))
            else:
                _ledger.safe_move(db, c_src, c_dst, f"{reason} compagnon",
                                  run_id, commit=False)
        # source du résumé → transcription (co-localisée). Mise à jour même en
        # réalignement (la transcription a bougé bien que le résumé non).
        if res_new.is_file() and tr_old is not None:
            _set_fm_source(res_new, tr_new_rel)
        # source de la transcription → fichier source (rel ~) : sans cette mise
        # à jour, tout déplacement laisse un `source` périmé dans la
        # transcription (constaté en réel : 7 893 transcriptions périmées après
        # le grand classement — cassait l'appariement des exclusions de
        # résumés et propageait des chemins morts dans les requêtes de batch).
        if tr_old is not None and tr_new.is_file():
            _set_fm_source(tr_new, f"Documents/{new_rel}")
        # références DB (relink seulement si le doc change vraiment de chemin —
        # sinon old==new viderait la ligne).
        if old_rel != new_rel:
            db.relink_document(old_rel, new_rel, commit=False)
        # Hash en ancre de la fiche (diff de photos) : le ledger vient de le
        # calculer pour le move de la source — l'estampiller gratuitement.
        db.set_classification_hash(new_rel, src_sha, commit=False)
        if tr_old is not None:
            old_tr_rel = str(Path(tr_old).relative_to(CONNAISSANCE_ROOT))
            if old_tr_rel != tr_new_rel:
                db.rename_text_simhash(old_tr_rel, tr_new_rel, commit=False)
        # files (cache) dans la même transaction
        for _key, s, d in moves:
            db.move_file(str(s), str(d), commit=False)

    return {"dry_run": False, "old": old_rel, "new": new_rel,
            "moved": done, "ledger_run": run_id}
