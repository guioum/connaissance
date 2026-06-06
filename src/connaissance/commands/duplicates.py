"""Module commands/duplicates : Phase D du grand chantier — doublons de ~/Documents.

Deux familles, sur le corpus déjà extrait par ``documents signals`` (donc sans
secrets ni conteneurs) :

- **exacts** : même SHA256 (octet pour octet) — plusieurs copies du même fichier.
- **quasi** : SimHash texte proche (``core/dedup``) sur le résumé extractif des
  signaux — même document re-scanné/re-enregistré. Cache ``doc_simhash``
  (référentiel ~/Documents), distinct du corpus ``text_simhash``.

``scan`` rapporte les clusters (lecture seule). ``plan`` choisit un *keeper* par
cluster et propose d'envoyer les autres à la **corbeille ledger**. ``apply``
exécute (``safe_trash`` — réversible par ``ledger revert``, dry-run par défaut).
"""
from __future__ import annotations

import json
from pathlib import Path

from connaissance.core import classify as _heur
from connaissance.core import dedup as _dedup
from connaissance.core import ledger as _ledger
from connaissance.core.manifest_io import load_entries
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import (DOCUMENTS_DIR, documents_read_path,
                                     require_paths, transit_file)
from connaissance.core.tracking import TrackingDB

QUASI_THRESHOLD = _dedup.DEFAULT_THRESHOLD   # Hamming <= 3 / 64


def _summary_text(packet: dict) -> str:
    """Texte de fingerprint d'un document : phrases + mots-clés du résumé."""
    s = packet.get("summary") or {}
    parts = list(s.get("sentences") or []) + list(s.get("keywords") or [])
    return " ".join(parts).strip()


def _keeper_index(rels: list[str]) -> int:
    """Choisir le fichier à GARDER dans un cluster : le chemin le moins profond
    (le mieux rangé), puis ordre alphabétique. Les autres iront en corbeille."""
    return min(range(len(rels)),
               key=lambda i: (len(Path(rels[i]).parts), rels[i].lower()))


def scan(db: TrackingDB | None = None) -> dict:
    """Détecter les doublons (exacts + quasi) du corpus signalé (schema Duplicates).

    Lecture seule. Itère le cache ``doc_signals`` ; lit le contenu via le miroir
    SSD (jamais de download iCloud). Retourne les clusters trouvés.
    """
    require_paths(DOCUMENTS_DIR, context="duplicates scan")
    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        rows = db.all_doc_signals()
        by_hash: dict[str, list[str]] = {}
        sim_rels: list[str] = []
        sim_vals: list[int] = []
        scanned = unreadable = 0
        for rel, packet in rows:
            abs_path = DOCUMENTS_DIR / rel
            sha = db.get_or_compute_hash(
                abs_path, read_path=documents_read_path(abs_path))
            if sha is None:
                unreadable += 1
            else:
                by_hash.setdefault(sha, []).append(rel)
            scanned += 1
            text = _summary_text(packet)
            if text:
                h = db.get_or_compute_doc_simhash(
                    abs_path, rel, compute_fn=lambda _p, t=text:
                        (_dedup.to_hex(v) if (v := _dedup.simhash_text(t))
                         is not None else None))
                if h is not None:
                    sim_rels.append(rel)
                    sim_vals.append(_dedup.from_hex(h))
    finally:
        if owns:
            db.close()

    exact = [{"hash": h, "rels": sorted(rels)}
             for h, rels in by_hash.items() if len(rels) > 1]

    # Quasi : exclure les paires déjà exactes (même hash) pour ne pas les compter
    # deux fois — on ne garde un cluster quasi que s'il apporte des rels nouveaux.
    exact_set = {r for c in exact for r in c["rels"]}
    quasi = []
    for grp in _dedup.cluster_by_hamming(sim_vals, QUASI_THRESHOLD):
        rels = sorted({sim_rels[i] for i in grp} - exact_set)
        if len(rels) > 1:
            quasi.append({"rels": rels})

    return {
        "scanned": scanned,
        "unreadable": unreadable,
        "exact_clusters": exact,
        "quasi_clusters": quasi,
        "exact_duplicates": sum(len(c["rels"]) - 1 for c in exact),
        "quasi_duplicates": sum(len(c["rels"]) - 1 for c in quasi),
        "threshold": QUASI_THRESHOLD,
    }


def plan(output_file: str | None = None, db: TrackingDB | None = None) -> dict:
    """Construire un manifeste de déduplication (schema DuplicatesPlan).

    Pour chaque cluster, garde un *keeper* (le mieux rangé) et marque les autres
    pour la corbeille. N'écrit/ne déplace rien sur le corpus — produit un
    manifeste plan→apply révisable (écrit en transit).
    """
    s = scan(db=db)
    entries: list[dict] = []
    for kind, clusters in (("exact", s["exact_clusters"]),
                           ("quasi", s["quasi_clusters"])):
        for c in clusters:
            rels = c["rels"]
            keep = _keeper_index(rels)
            for i, rel in enumerate(rels):
                if i == keep:
                    continue
                entries.append({"trash": rel, "keeper": rels[keep],
                                "kind": kind, "hash": c.get("hash")})

    transit = transit_file("duplicates-manifest")
    transit.write_text(json.dumps({"entries": entries}, ensure_ascii=False),
                       encoding="utf-8")
    payload = {
        "total": len(entries),
        "exact": s["exact_duplicates"],
        "quasi": s["quasi_duplicates"],
        "scanned": s["scanned"],
        "manifest_file": str(transit),
        "entries": entries,
    }

    def _summary(p: dict) -> dict:
        return {k: p[k] for k in
                ("total", "exact", "quasi", "scanned", "manifest_file")} | {
            "sample": [{"trash": e["trash"], "keeper": e["keeper"],
                        "kind": e["kind"]} for e in p["entries"][:8]],
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)


def apply(manifest_file: str, dry_run: bool = True,
          db: TrackingDB | None = None) -> dict:
    """Envoyer les doublons d'un manifeste à la corbeille ledger (schema
    DuplicatesApply). **Dry-run par défaut** ; réversible par ``ledger revert``.
    """
    require_paths(DOCUMENTS_DIR, context="duplicates apply")
    _, entries = load_entries(manifest_file)
    owns = db is None
    if db is None:
        db = TrackingDB()
    run_id = _ledger.new_run_id("duplicates")
    trashed: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    # Capture consciente du contexte : sujet(s) de CHAQUE copie d'un cluster →
    # attachés au fichier gardé (multi-sujet). Le contexte des copies supprimées
    # survit comme sujets virtuels du gardé. {keeper_rel: set(sujets)}.
    captured: dict[str, set] = {}
    for e in entries:
        keeper = e.get("keeper")
        if not keeper:
            continue
        bag = captured.setdefault(keeper, set())
        for rel in (keeper, e["trash"]):
            s = _heur.sujet_from_path(rel)
            if s:
                bag.add(s)
    sujets_captured = 0
    try:
        for e in entries:
            src = DOCUMENTS_DIR / e["trash"]
            if not src.exists():
                skipped.append({"trash": e["trash"], "reason": "introuvable"})
                continue
            if dry_run:
                trashed.append({"trash": e["trash"], "keeper": e.get("keeper")})
                continue
            try:
                _ledger.safe_trash(db, src,
                                   f"duplicate {e.get('kind') or ''}".strip(),
                                   run_id)
                trashed.append({"trash": e["trash"], "keeper": e.get("keeper")})
            except OSError as exc:
                errors.append({"trash": e["trash"], "error": str(exc)})
        # N'attacher les sujets que pour les keepers dont au moins une copie a
        # bien été corbeillée (sinon rien n'a changé).
        if not dry_run:
            trashed_keepers = {t["keeper"] for t in trashed}
            for keeper, bag in captured.items():
                if keeper in trashed_keepers and bag:
                    sujets_captured += db.add_doc_sujets(
                        keeper, sorted(bag), "dedup", commit=False)
            db._conn.commit()
    finally:
        if owns:
            db.close()

    result = {
        "dry_run": dry_run,
        "planned": len(entries),
        "trashed": 0 if dry_run else len(trashed),
        "would_trash": len(trashed) if dry_run else 0,
        "sujets_captured": (sum(len(b) for b in captured.values())
                            if dry_run else sujets_captured),
        "skipped": skipped,
        "errors": errors,
        "moves": trashed[:50],
    }
    if not dry_run and trashed:
        result["ledger_run"] = run_id
    return result
