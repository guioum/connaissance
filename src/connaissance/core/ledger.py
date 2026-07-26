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
import json
import os
import shutil
import uuid
from pathlib import Path

import yaml

from connaissance.core.frontmatter import split_frontmatter, write_frontmatter
from connaissance.core.paths import (BASE_PATH, CONNAISSANCE_ROOT,
                                     DOCUMENTS_DIR, documents_read_path)
from connaissance.core.schemas import LedgerPurge, LedgerRevert, LedgerVerify
from connaissance.core.tracking import LEDGER_JOURNAL_DIR, _append_jsonl


def new_run_id(prefix: str = "run") -> str:
    """Identifiant de lot unique (1 run = 1 ensemble révertible)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def safe_move(db, old_path, new_path, reason: str, run_id: str,
              *, dry_run: bool = False, commit: bool = True,
              op: str | None = None) -> dict:
    """Déplacer/renommer un fichier en le journalisant (réversible).

    - ``dry_run`` : retourne l'entrée ``status='planned'`` SANS rien écrire.
    - sinon : calcule le hash (cache JIT, lecture via miroir SSD si dispo —
      aucun téléchargement iCloud), déplace, et enregistre une ligne
      ``status='applied'`` dans le ledger.

    ``commit=False`` : l'insertion ledger reste dans la transaction courante de
    l'appelant (ex. ``with db.transaction():`` pour grouper avec un relink de
    fiche). Le ``shutil.move`` lui-même n'est jamais transactionnel.

    ``op`` : par défaut ``rename`` si seul le nom change (même dossier parent),
    sinon ``move``. Un appelant peut forcer une autre étiquette (ex. ``trash``
    pour la corbeille ledger) afin de distinguer le type d'opération.
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
        "op": op or ("rename" if old.parent == new.parent else "move"),
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
    # Garde anti-écrasement : un rename POSIX remplace silencieusement la
    # cible (perte non journalisée du contenu écrasé). Les appelants gèrent
    # les collisions AVANT d'appeler safe_move (unique_dest, skip) ; toute
    # cible restante est une erreur. Exception : le renommage de casse sur
    # APFS insensible à la casse (old et new désignent le même fichier).
    if os.path.lexists(new):
        same = False
        try:
            same = old.exists() and new.exists() and old.samefile(new)
        except OSError:
            pass
        if not same:
            raise FileExistsError(
                f"safe_move : la destination existe déjà, refus d'écraser : {new}")
    shutil.move(str(old), str(new))
    entry["status"] = "applied"
    # Journal disque append-only, écrit AVANT l'enregistrement DB : si la DB
    # échoue (verrou, disque plein), la trace du déplacement survit sur disque
    # (rejouable par `audit restore-journals`). Le journal reste best-effort :
    # son échec n'empêche pas l'enregistrement DB.
    try:
        _append_jsonl(LEDGER_JOURNAL_DIR / f"{run_id}.jsonl", dict(entry))
    except OSError:
        pass
    db.ledger_record(entry, commit=commit)
    return entry


def safe_trash(db, path, reason: str, run_id: str,
               *, dry_run: bool = False, commit: bool = True) -> dict:
    """Envoyer un fichier à la **corbeille ledger** au lieu de le supprimer.

    Au lieu d'un ``unlink`` irréversible, déplace le fichier sous
    ``~/Connaissance/.trash/<run_id>/<chemin d'origine>`` via ``safe_move`` avec
    ``op='trash'`` : l'opération est journalisée, **réversible** par
    ``ledger revert <run>`` (restauration vérifiée par hash), et n'est détruite
    définitivement que par ``ledger purge``. La structure d'origine est
    préservée sous le dossier de run (pas de collision de noms).
    """
    path = Path(path)
    # Préserver la structure d'origine : rel à ~/Connaissance, sinon rel au
    # HOME (« Documents/… » — avant, les fichiers hors Connaissance étaient
    # aplatis au nom seul : 2 047 doublons à plat constatés le 2026-07-26).
    for racine in (CONNAISSANCE_ROOT, BASE_PATH):
        try:
            rel = path.relative_to(racine)
            break
        except ValueError:
            continue
    else:
        rel = Path(path.name)
    dest = CONNAISSANCE_ROOT / ".trash" / run_id / rel
    return safe_move(db, path, dest, reason, run_id,
                     dry_run=dry_run, commit=commit, op="trash")


def purge_run(db, *, run_id: str | None = None,
              older_than_days: int | None = None,
              dry_run: bool = False) -> LedgerPurge:
    """Vider la corbeille ledger : suppression **définitive** des fichiers
    déplacés en corbeille (``op='trash'``, ``status='applied'``).

    Filtrable par ``run_id`` et/ou ``older_than_days`` (ancienneté de l'opération).
    Marque les lignes purgées (``status='purged'``) pour qu'elles ne soient plus
    proposées au revert. **Irréversible** (vrai ``unlink``). En ``dry_run``,
    rapporte seulement ce qui serait purgé.
    """
    ops = db.ledger_trash_ops(run_id=run_id, older_than_days=older_than_days)
    result: LedgerPurge = {
        "dry_run": dry_run,
        "purged": 0,
        "freed_bytes": 0,
        "skipped": [],  # [{path, reason}]
    }
    for row in ops:
        p = Path(row["new_path"])
        if not p.exists():
            # Déjà parti (reverté hors ledger, ou purge interrompue) : on solde
            # quand même la ligne pour ne pas la re-proposer indéfiniment.
            if not dry_run:
                db.ledger_mark_purged(row["id"])
            result["skipped"].append({"path": str(p), "reason": "introuvable"})
            continue
        if not dry_run:
            try:
                p.unlink()
            except OSError as exc:
                result["skipped"].append({"path": str(p), "reason": str(exc)})
                continue
            db.ledger_mark_purged(row["id"])
        result["purged"] += 1
        result["freed_bytes"] += row["size"] or 0
    return result


def snapshot_entries(db, *, run_id: str | None = None) -> list[dict]:
    """Reconstruire l'historique : pour chaque déplacement journalisé, l'ancien
    chemin et l'emplacement **actuel** du fichier (en suivant la chaîne des
    déplacements old→new jusqu'au terminal).

    Retourne ``[{run_id, timestamp, op, old_path, terminal, exists}]`` (filtrable
    par ``run_id``). Sert à la vue ``- Historique`` (snapshots en symlinks).
    """
    import unicodedata

    def _n(s: str) -> str:
        # Chaîne NORMALISÉE NFC : le ledger journalise les chemins tels que
        # fournis (NFD du walk APFS ou NFC des clés DB selon l'appelant) —
        # sans normaliser, une chaîne old→new mêlant les deux formes se casse.
        return unicodedata.normalize("NFC", s)

    ops = db.ledger_all_ops(status="applied")
    forward: dict[str, str] = {}
    destinations: set[str] = set()
    for o in ops:
        if o.get("old_path") and o.get("new_path"):
            forward[_n(o["old_path"])] = _n(o["new_path"])
            destinations.add(_n(o["new_path"]))

    def resolve(p: str) -> str:
        p = _n(p)
        seen = {p}
        while p in forward and forward[p] not in seen:
            p = forward[p]
            seen.add(p)
        return p

    out: list[dict] = []
    for o in ops:
        if run_id and o["run_id"] != run_id:
            continue
        old, new = o.get("old_path"), o.get("new_path")
        if not old or not new:
            continue
        old, new = _n(old), _n(new)
        terminal = resolve(new)
        out.append({
            "run_id": o["run_id"], "timestamp": o.get("timestamp"),
            "op": o.get("op"), "reason": o.get("reason"),
            "old_path": old, "terminal": terminal,
            "exists": Path(terminal).exists(),
            # origine = chemin jamais redevenu une destination (vrai point de
            # départ ; les intermédiaires d'une chaîne sont exclus du snapshot).
            "is_origin": old not in destinations,
        })
    return out


def _revert_refs(db, cur: Path, dest: Path) -> None:
    """Faire suivre les **références DB** à une restauration (relink inverse).

    ``revert_run`` remettait les fichiers en place mais laissait les tables
    pointer vers le chemin annulé (fiche/`doc_*` sur l'ancien nouveau chemin,
    `text_simhash` d'une transcription, `files`, champ ``source`` d'un résumé).
    Miroir exact des mises à jour faites par ``relocate_document`` à l'aller.
    Best-effort : une référence absente n'empêche pas la restauration."""
    # doc_signals / doc_classification / doc_sujets (rel ~/Documents)
    try:
        cur_rel = str(cur.relative_to(DOCUMENTS_DIR))
        dest_rel = str(dest.relative_to(DOCUMENTS_DIR))
        if cur_rel != dest_rel:
            db.relink_document(cur_rel, dest_rel)
    except ValueError:
        pass
    # transcription : simhash indexé par rel ~/Connaissance
    try:
        cur_c = str(cur.relative_to(CONNAISSANCE_ROOT))
        dest_c = str(dest.relative_to(CONNAISSANCE_ROOT))
        if cur_c.startswith("Transcriptions/") and cur_c != dest_c:
            db.rename_text_simhash(cur_c, dest_c)
    except ValueError:
        pass
    # cache `files` : le calcul de hash (aller comme revert) peut avoir laissé
    # une ligne à l'ancien chemin — la purger avant l'UPDATE (UNIQUE sur path).
    db.delete_files([str(dest)])
    db.move_file(str(cur), str(dest))


def _restore_resume_source(dest: Path) -> None:
    """Repointer le champ ``source`` d'un résumé restauré vers sa transcription
    co-localisée (relocate l'avait fait pointer vers le nouveau chemin).
    Appelé en **post-passe** du revert : le parcours inverse restaure le résumé
    avant sa transcription, le champ ne peut être réaligné qu'une fois tous
    les fichiers revenus."""
    try:
        rel = dest.relative_to(CONNAISSANCE_ROOT)
    except ValueError:
        return
    if rel.parts[:2] != ("Résumés", "Documents"):
        return
    tr = CONNAISSANCE_ROOT / "Transcriptions" / Path(*rel.parts[1:])
    if not tr.is_file():
        return
    try:
        parts = split_frontmatter(dest.read_text(encoding="utf-8"))
        if not parts:
            return
        fm_text, body = parts
        fm = yaml.safe_load(fm_text) or {}
        fm["source"] = str(tr.relative_to(CONNAISSANCE_ROOT))
        write_frontmatter(dest, fm, body)
    except (OSError, yaml.YAMLError):
        pass


def revert_run(db, run_id: str, *, dry_run: bool = False) -> LedgerRevert:
    """Annuler un run : remettre chaque fichier à son ancien emplacement.

    Parcourt les opérations ``applied`` en ordre **inverse**. Chaque restauration
    est protégée :

    - le fichier doit exister à ``new_path`` (sinon il a bougé ailleurs → skip) ;
    - son **hash** doit correspondre à celui enregistré (sinon contenu modifié
      depuis → skip, on ne perd jamais une version plus récente) ;
    - ``old_path`` doit être libre (sinon collision → skip).

    Les **références DB suivent** la restauration (``_revert_refs`` : relink
    inverse fiche/simhash/files + champ ``source`` des résumés).
    En ``dry_run``, rien n'est déplacé ; on rapporte seulement ce qui serait fait.
    """
    ops = db.ledger_ops(run_id, status="applied")
    result: LedgerRevert = {
        "run_id": run_id,
        "dry_run": dry_run,
        "reverted": 0,
        "skipped": [],  # [{path, reason}]
    }
    restored: list[Path] = []

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
            _revert_refs(db, cur, dest)
            db.ledger_mark_reverted(row["id"])
            restored.append(dest)
        result["reverted"] += 1

    # Post-passe : réaligner le `source` des résumés restaurés (leur
    # transcription est revenue APRÈS eux dans le parcours inverse).
    for dest in restored:
        if dest.suffix == ".md":
            _restore_resume_source(dest)

    return result


def run_report_md(run_id: str) -> str:
    """Vue Markdown lisible d'un run de déplacements, lue du **JSONL disque**.

    Indépendante de la DB (consultation même si la base est perdue). Sert de
    projection jetable (le JSONL reste la source durable)."""
    path = LEDGER_JOURNAL_DIR / f"{run_id}.jsonl"
    entries: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    lines = [f"# Run ledger `{run_id}`", "",
             f"{len(entries)} opération(s).", "",
             "| op | de | vers |", "|----|----|------|"]
    for e in entries:
        old = (e.get("old_path") or "").replace("|", "\\|")
        new = (e.get("new_path") or "").replace("|", "\\|")
        lines.append(f"| {e.get('op', '')} | `{old}` | `{new}` |")
    return "\n".join(lines) + "\n"


def write_run_report(run_id: str) -> str | None:
    """Écrire la vue Markdown du run à côté de son JSONL ; retourne le chemin."""
    md_path = LEDGER_JOURNAL_DIR / f"{run_id}.md"
    try:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(run_report_md(run_id), encoding="utf-8")
        return str(md_path)
    except OSError:
        return None


def verify_run(db, run_id: str) -> LedgerVerify:
    """Vérifier la cohérence ledger ↔ disque pour un run.

    Pour chaque opération ``applied`` : le fichier est-il bien présent à
    ``new_path`` avec le hash attendu ? Remonte les écarts (déplacé, modifié,
    disparu) sans rien changer.
    """
    ops = db.ledger_ops(run_id, status="applied")
    result: LedgerVerify = {"run_id": run_id, "checked": len(ops), "ok": 0,
                            "issues": []}
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
