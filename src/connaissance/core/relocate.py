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

from pathlib import Path

import yaml

from connaissance.core import ledger as _ledger
from connaissance.core.frontmatter import (parse_frontmatter,
                                            split_frontmatter,
                                            write_frontmatter)
from connaissance.core.paths import CONNAISSANCE_ROOT, DOCUMENTS_DIR

TRANSCR = CONNAISSANCE_ROOT / "Transcriptions" / "Documents"
RESUMES = CONNAISSANCE_ROOT / "Résumés" / "Documents"


def _split(rel: str):
    """``<type>/<slug>/<stem>.<ext>`` → (type, slug, stem, ext)."""
    p = Path(rel)
    parts = p.parts
    etype = parts[0] if len(parts) >= 1 else ""
    slug = parts[1] if len(parts) >= 3 else ""
    return etype, slug, p.stem, p.suffix


def _read_fm(md: Path) -> dict | None:
    try:
        t = md.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_frontmatter(t)


def _set_fm_source(md: Path, new_source: str) -> None:
    """Mettre à jour le champ ``source`` du frontmatter d'un résumé."""
    t = md.read_text(encoding="utf-8")
    fm_text, body = split_frontmatter(t)
    fm = yaml.safe_load(fm_text) or {}
    fm["source"] = new_source
    write_frontmatter(md, fm, body)


def relocate_document(db, old_rel: str, new_rel: str, run_id: str,
                      *, dry_run: bool = False) -> dict:
    """Déplacer un document (rel ~/Documents) et tout son graphe.

    Déplace source + transcription + résumé (ceux qui existent) via le ledger,
    met à jour ``source`` du résumé, ``relink_document`` (doc_*), ``text_simhash``
    et ``files``. Transaction atomique côté DB. Retourne le détail.
    """
    otype, oslug, ostem, _ = _split(old_rel)
    ntype, nslug, nstem, _ = _split(new_rel)

    src_old, src_new = DOCUMENTS_DIR / old_rel, DOCUMENTS_DIR / new_rel
    res_old = RESUMES / otype / oslug / (ostem + ".md")
    res_new = RESUMES / ntype / nslug / (nstem + ".md")

    # Transcription : suivre le `source` du résumé (récupère les orphelines) ;
    # sinon le chemin co-localisé.
    tr_old = None
    fm = _read_fm(res_old) if res_old.is_file() else None
    if fm and fm.get("source"):
        cand = CONNAISSANCE_ROOT / fm["source"]
        tr_old = cand if cand.exists() else None
    if tr_old is None:
        cand = TRANSCR / otype / oslug / (ostem + ".md")
        tr_old = cand if cand.exists() else None
    tr_new = TRANSCR / ntype / nslug / (nstem + ".md")
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
        return {"dry_run": True, "old": old_rel, "new": new_rel,
                "moves": [k for k, _, _ in moves],
                "transcription_found": tr_old is not None}

    done = []
    with db.transaction():
        for key, s, d in moves:
            _ledger.safe_move(db, s, d, f"relocate {key}", run_id, commit=False)
            done.append(key)
        # source du résumé → transcription (co-localisée). Mise à jour même en
        # réalignement (la transcription a bougé bien que le résumé non).
        if res_new.is_file() and tr_old is not None:
            _set_fm_source(res_new, tr_new_rel)
        # références DB (relink seulement si le doc change vraiment de chemin —
        # sinon old==new viderait la ligne).
        if old_rel != new_rel:
            db.relink_document(old_rel, new_rel, commit=False)
        if tr_old is not None:
            old_tr_rel = str(Path(tr_old).relative_to(CONNAISSANCE_ROOT))
            if old_tr_rel != tr_new_rel:
                db.rename_text_simhash(old_tr_rel, tr_new_rel, commit=False)
        # files (cache) dans la même transaction
        for _key, s, d in moves:
            db.move_file(str(s), str(d), commit=False)

    return {"dry_run": False, "old": old_rel, "new": new_rel,
            "moved": done, "ledger_run": run_id}
