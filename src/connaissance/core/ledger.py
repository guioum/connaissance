"""Ledger journalisé et réversible des opérations de fichiers.

Toute modification de nom/dossier d'un fichier doit passer par ``safe_move()``,
qui journalise l'opération dans la table ``file_ledger`` de tracking.db : ancien
chemin, nouveau chemin, **SHA256**, taille, mtime, raison, ``run_id``.

Le hash est la clé de la réversibilité : au rollback, on ne restaure un fichier
que si son contenu est **intact** (hash identique à celui enregistré) — jamais
en aveugle. Un ``run_id`` regroupe les opérations d'un même lot, révertibles
ensemble.

Le ledger n'enregistre que les opérations **appliquées**. En ``dry_run``,
``safe_move`` retourne l'entrée prévue sans toucher ni au disque ni au ledger.
"""
import shutil
import uuid
from pathlib import Path

from connaissance.core.paths import documents_read_path


def new_run_id(prefix: str = "run") -> str:
    """Identifiant de lot unique (1 run = 1 ensemble révertible)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def safe_move(db, old_path, new_path, reason: str, run_id: str,
              *, dry_run: bool = False) -> dict:
    """Déplacer/renommer un fichier en le journalisant (réversible).

    - ``dry_run`` : retourne l'entrée ``status='planned'`` SANS rien écrire.
    - sinon : calcule le hash (cache JIT, lecture via miroir SSD si dispo —
      aucun téléchargement iCloud), déplace, et enregistre une ligne
      ``status='applied'`` dans le ledger.

    L'``op`` est ``rename`` si seul le nom change (même dossier parent), sinon
    ``move``.
    """
    old = Path(old_path)
    new = Path(new_path)
    try:
        st = old.stat()
        size, mtime = int(st.st_size), float(st.st_mtime)
    except OSError:
        size, mtime = None, None

    sha = db.get_or_compute_hash(old, read_path=documents_read_path(old))
    entry = {
        "run_id": run_id,
        "op": "rename" if old.parent == new.parent else "move",
        "old_path": str(old),
        "new_path": str(new),
        "sha256": sha,
        "size": size,
        "mtime": mtime,
        "reason": reason,
    }

    if dry_run:
        entry["status"] = "planned"
        return entry

    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    db.ledger_record(entry)
    entry["status"] = "applied"
    return entry


def revert_run(db, run_id: str, *, dry_run: bool = False) -> dict:
    """Annuler un run : remettre chaque fichier à son ancien emplacement.

    Parcourt les opérations ``applied`` en ordre **inverse**. Chaque restauration
    est protégée :

    - le fichier doit exister à ``new_path`` (sinon il a bougé ailleurs → skip) ;
    - son **hash** doit correspondre à celui enregistré (sinon contenu modifié
      depuis → skip, on ne perd jamais une version plus récente) ;
    - ``old_path`` doit être libre (sinon collision → skip).

    En ``dry_run``, rien n'est déplacé ; on rapporte seulement ce qui serait fait.
    """
    ops = db.ledger_ops(run_id, status="applied")
    result = {
        "run_id": run_id,
        "dry_run": dry_run,
        "reverted": 0,
        "skipped": [],  # [{path, reason}]
    }

    for row in reversed(ops):
        cur = Path(row["new_path"])
        dest = Path(row["old_path"])

        if not cur.exists():
            result["skipped"].append({"path": str(cur), "reason": "introuvable"})
            continue
        cur_hash = db.get_or_compute_hash(cur, read_path=documents_read_path(cur))
        if row["sha256"] and cur_hash and cur_hash != row["sha256"]:
            result["skipped"].append({"path": str(cur), "reason": "contenu_modifie"})
            continue
        if dest.exists():
            result["skipped"].append({"path": str(dest), "reason": "destination_occupee"})
            continue

        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cur), str(dest))
            db.ledger_mark_reverted(row["id"])
        result["reverted"] += 1

    return result


def verify_run(db, run_id: str) -> dict:
    """Vérifier la cohérence ledger ↔ disque pour un run.

    Pour chaque opération ``applied`` : le fichier est-il bien présent à
    ``new_path`` avec le hash attendu ? Remonte les écarts (déplacé, modifié,
    disparu) sans rien changer.
    """
    ops = db.ledger_ops(run_id, status="applied")
    result = {"run_id": run_id, "checked": len(ops), "ok": 0, "issues": []}
    for row in ops:
        cur = Path(row["new_path"])
        if not cur.exists():
            result["issues"].append({"path": str(cur), "reason": "disparu"})
            continue
        cur_hash = db.get_or_compute_hash(cur, read_path=documents_read_path(cur))
        if row["sha256"] and cur_hash and cur_hash != row["sha256"]:
            result["issues"].append({"path": str(cur), "reason": "contenu_modifie"})
            continue
        result["ok"] += 1
    return result
