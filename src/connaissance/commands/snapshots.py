"""Module commands/snapshots : photos point-in-time de l'organisation.

Un **snapshot** = une copie nommée de ``tracking.db`` à l'instant T
(``VACUUM INTO``), sous ``.config/snapshots/<date>-<label>.db``. Il fige tout
l'état (tous les documents + organisation + ledger). À la différence de la vue
``Historique`` (une seule baseline = l'origine), on garde N photos datées.

``view`` rend une photo **navigable et AUTO-RÉPARANTE** sous
``~/Connaissance/Vues/Snapshots/<nom>/`` : chaque doc (union ``doc_signals`` ∪
``doc_classification`` = tout le corpus) apparaît à son chemin *de T*, mais le
symlink pointe vers son emplacement **actuel** — résolu via la chaîne du
**ledger** (old→new). La photo ne rote pas quand les fichiers bougent.
``diff`` compare deux photos (déplacés / reclassés / ajoutés / retirés, par hash).
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

from connaissance.core.paths import (DOCUMENTS_DIR, SNAPSHOTS_DIR, VIEWS_ROOT,
                                     symlink_avec_mtime)
from connaissance.core.tracking import DB_PATH, TrackingDB


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:40]


def _snap_path(name: str) -> Path | None:
    """Résoudre un nom de snapshot (exact, ou suffixe de label) vers son .db."""
    if not SNAPSHOTS_DIR.exists():
        return None
    exact = SNAPSHOTS_DIR / f"{name}.db"
    if exact.exists():
        return exact
    # match partiel (sur le label) : le dernier qui contient la chaîne
    cands = sorted(p for p in SNAPSHOTS_DIR.glob("*.db") if name in p.stem)
    return cands[-1] if cands else None


def _classification_rows(db_file: Path) -> list[dict]:
    """(rel_path, hash, entity_slug, entity_type, category, date) d'une DB."""
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT rel_path, hash, entity_type, entity_slug, category, date "
            "FROM doc_classification").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _doc_rels(db_file: Path) -> list[str]:
    """Tous les documents d'une DB : union de ``doc_signals`` (le vrac) et de
    ``doc_classification`` (les déjà-organisés sous organismes/personnes/…, que
    le scan signaux exclut). Couvre donc l'intégralité du corpus, où qu'il soit."""
    conn = sqlite3.connect(str(db_file))
    rels: set[str] = set()
    for sql in ("SELECT rel_path FROM doc_signals",
                "SELECT rel_path FROM doc_classification"):
        try:
            rels.update(r[0] for r in conn.execute(sql).fetchall())
        except sqlite3.OperationalError:
            pass
    conn.close()
    return sorted(rels)


def create(label: str = "", no_view: bool = False,
           db_path: Path | None = None) -> dict:
    """Figer une photo de tracking.db (VACUUM INTO, copie consistante).

    Par défaut, **rend aussi la vue navigable** immédiatement (``Vues/Snapshots/
    <nom>/``) — ``no_view=True`` pour ne garder que la photo DB."""
    src = Path(db_path or DB_PATH)
    if not src.exists():
        return {"error": f"base introuvable : {src}"}
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    slug = _slug(label)
    name = f"{stamp}{('-' + slug) if slug else ''}"
    dest = SNAPSHOTS_DIR / f"{name}.db"
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    out = {"snapshot": name, "path": str(dest),
           "documents": len(_doc_rels(dest))}
    if not no_view:
        out["view"] = view(name, apply=True)
    return out


def list_snapshots() -> dict:
    """Lister les photos disponibles (nom, date, nb de documents)."""
    out = []
    if SNAPSHOTS_DIR.exists():
        for p in sorted(SNAPSHOTS_DIR.glob("*.db"), reverse=True):
            out.append({"snapshot": p.stem,
                        "documents": len(_doc_rels(p)),
                        "size_bytes": p.stat().st_size})
    return {"total": len(out), "snapshots": out}


def view(name: str, apply: bool = False, clear: bool = False,
         db: TrackingDB | None = None) -> dict:
    """Rendre une photo navigable (symlinks auto-réparants via le ledger).

    Dry-run par défaut. ``apply`` (re)construit, ``clear`` supprime la vue."""
    view_dir = VIEWS_ROOT / "Snapshots" / name
    if clear:
        existed = view_dir.exists()
        if existed:
            shutil.rmtree(view_dir)
        return {"cleared": True, "existed": existed, "view_dir": str(view_dir)}

    snap = _snap_path(name)
    if snap is None:
        return {"error": f"snapshot introuvable : {name}"}
    # On garde le nom réel (au cas où `name` était un match partiel).
    real = snap.stem
    view_dir = VIEWS_ROOT / "Snapshots" / real

    # Tous les DOCUMENTS de la photo (vrac ∪ déjà-organisés), pas que les classés.
    snap_rels = _doc_rels(snap)
    # Auto-réparation par le LEDGER de la base LIVE (doc_signals n'a pas de hash) :
    # chaîne old→new pour retrouver l'emplacement actuel d'un chemin de T.
    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        # Carte NORMALISÉE NFC des deux côtés : le ledger journalise les
        # chemins tels que fournis (souvent NFD, rel brut du walk APFS) alors
        # que les rels du snapshot sont NFC (clés DB) — sans normaliser, la
        # chaîne rate tous les déplacements et la vue les croit « gone »
        # (constaté en réel : 914 introuvables après l'apply tranche 1).
        # L'accès disque APFS est insensible à la normalisation → un chemin
        # résolu en NFC se lit même si le fichier est nommé NFD.
        _n = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
        fwd: dict[str, str] = {}
        for op in db.ledger_all_ops(status="applied"):
            if op.get("old_path") and op.get("new_path"):
                fwd[_n(op["old_path"])] = _n(op["new_path"])
    finally:
        if owns:
            db.close()

    def _current(abs_t: str) -> str:
        abs_t = _n(abs_t)
        seen = {abs_t}
        while abs_t in fwd and fwd[abs_t] not in seen:
            abs_t = fwd[abs_t]
            seen.add(abs_t)
        return abs_t

    linked = gone = 0
    for rel_t in snap_rels:                          # chemin AU MOMENT du snapshot
        cur = _current(str(DOCUMENTS_DIR / rel_t))
        if not Path(cur).exists():
            gone += 1
            continue
        if apply:
            link = view_dir / rel_t
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                link.unlink()
            symlink_avec_mtime(link, cur)
        linked += 1

    return {"snapshot": real, "documents": len(snap_rels),
            "linked": linked, "gone": gone, "applied": apply,
            "view_dir": str(view_dir)}


def _ledger_forward(db_file: Path) -> dict[str, str]:
    """Carte old→new (chemins ABS, NFC) du ``file_ledger`` d'une photo."""
    conn = sqlite3.connect(str(db_file))
    try:
        rows = conn.execute(
            "SELECT old_path, new_path FROM file_ledger "
            "WHERE status = 'applied'").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    n = unicodedata.normalize
    return {n("NFC", o): n("NFC", w) for o, w in rows if o and w}


def diff(a: str, b: str) -> dict:
    """Comparer deux photos : déplacés / reclassés / ajoutés / retirés.

    Appariement d'une ligne de A à sa ligne de B : par **hash** quand les deux
    photos en ont un (ancre idéale — estampillé par ``relocate_document`` à
    chaque déplacement), sinon par **rel NFC résolu à travers le ledger de la
    photo B** (chaîne old→new, comme la vue snapshot) — c'est ce qui rend le
    diff véridique même pour les photos historiques dont ``hash`` est NULL
    (le « hash en ancre » du design v2.31 n'était jamais alimenté : le diff
    renvoyait des zéros systématiques, constaté le 2026-07-25)."""
    pa, pb = _snap_path(a), _snap_path(b)
    if pa is None or pb is None:
        return {"error": f"snapshot introuvable : {a if pa is None else b}"}
    n = unicodedata.normalize
    rows_a = _classification_rows(pa)
    rows_b = _classification_rows(pb)
    B_by_hash = {r["hash"]: r for r in rows_b if r["hash"]}
    B_by_rel = {n("NFC", r["rel_path"]): r for r in rows_b}
    fwd = _ledger_forward(pb)

    def _resolve(abs_t: str) -> str:
        seen = {abs_t}
        while abs_t in fwd and fwd[abs_t] not in seen:
            abs_t = fwd[abs_t]
            seen.add(abs_t)
        return abs_t

    moved, reclassified, removed = [], [], []
    matched_b: set[str] = set()
    for ra in rows_a:
        rel_a = n("NFC", ra["rel_path"])
        rb = B_by_hash.get(ra["hash"]) if ra["hash"] else None
        if rb is None:
            rb = B_by_rel.get(rel_a)
        if rb is None:
            # rel disparu de B : suivre la chaîne du ledger de B.
            terminal = _resolve(n("NFC", str(DOCUMENTS_DIR / ra["rel_path"])))
            try:
                rel_t = n("NFC", str(Path(terminal).relative_to(DOCUMENTS_DIR)))
            except ValueError:
                rel_t = None
            rb = B_by_rel.get(rel_t) if rel_t else None
        if rb is None:
            removed.append(ra["rel_path"])
            continue
        rel_b = n("NFC", rb["rel_path"])
        matched_b.add(rel_b)
        if rel_a != rel_b:
            moved.append({"from": ra["rel_path"], "to": rb["rel_path"]})
        if (ra["entity_slug"], ra["category"]) != \
                (rb["entity_slug"], rb["category"]):
            reclassified.append({
                "rel_path": rb["rel_path"],
                "from": f'{ra["entity_slug"]}/{ra["category"]}',
                "to": f'{rb["entity_slug"]}/{rb["category"]}'})
    added = [r["rel_path"] for r in rows_b
             if n("NFC", r["rel_path"]) not in matched_b]
    return {"a": pa.stem, "b": pb.stem,
            "added": len(added), "removed": len(removed),
            "moved": len(moved), "reclassified": len(reclassified),
            "sample": {"moved": moved[:10], "reclassified": reclassified[:10],
                       "added": added[:10], "removed": removed[:10]}}
