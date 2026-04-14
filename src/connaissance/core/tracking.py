"""Base de données SQLite de tracking pour le pipeline de connaissances.

Trace toutes les opérations (transcriptions, résumés, déplacements, etc.)
dans ~/Connaissance/.config/tracking.db. Partagée entre les plugins
le plugin connaissance.

Usage :
    from connaissance.core.tracking import TrackingDB

    db = TrackingDB()
    db.log("transcription", "extract_email",
           source_type="courriel",
           source_path="Archives/Courriels/.../INBOX.mbox",
           dest_path="Transcriptions/Courriels/.../INBOX/abc123.md",
           details={"message_id": "<id@domain>", "folder": "INBOX"})

    db.register_file(
        path="Transcriptions/Courriels/.../INBOX/abc123.md",
        file_type="transcription",
        source_type="courriel",
        message_id="<id@domain>")

    # Requêtes
    db.is_processed("<id@domain>", "extract_email")  # True/False
    db.get_file("Transcriptions/.../abc123.md")       # dict or None
    db.missing_resumes("Documents")                    # list of paths
"""

import json
import sqlite3
from pathlib import Path

from connaissance.core.paths import CONNAISSANCE_ROOT, require_connaissance_root

DB_PATH = CONNAISSANCE_ROOT / ".config" / "tracking.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    plugin TEXT NOT NULL,
    operation TEXT NOT NULL,
    source_type TEXT,
    source_path TEXT,
    dest_path TEXT,
    status TEXT NOT NULL DEFAULT 'success',
    details TEXT
);

CREATE INDEX IF NOT EXISTS idx_operations_timestamp ON operations(timestamp);
CREATE INDEX IF NOT EXISTS idx_operations_operation ON operations(operation);
CREATE INDEX IF NOT EXISTS idx_operations_source_path ON operations(source_path);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    file_type TEXT NOT NULL,
    source_type TEXT,
    source_path TEXT,
    entity_type TEXT,
    entity_slug TEXT,
    created TEXT,
    modified TEXT,
    message_id TEXT,
    hash TEXT,
    mtime REAL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_file_type ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_entity ON files(entity_type, entity_slug);
CREATE INDEX IF NOT EXISTS idx_files_message_id ON files(message_id);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
"""


class TrackingDB:
    """Interface SQLite pour le tracking des opérations."""

    def __init__(self, db_path=None):
        self._db_path = db_path or DB_PATH
        # Prérequis strict : ~/Connaissance/ doit exister, jamais créée par le plugin
        require_connaissance_root()
        # OK de créer .config/ comme sous-dossier direct (parents=False : si
        # Connaissance n'existait pas le check ci-dessus aurait déjà sorti)
        self._db_path.parent.mkdir(parents=False, exist_ok=True)
        self._cleanup_fuse_hidden()
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-8000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _cleanup_fuse_hidden(self):
        """Supprimer les fichiers .fuse_hidden* orphelins du dossier de la DB.

        Sur VirtioFS (cowork), SQLite WAL/SHM laissent ces fichiers fantômes
        quand un processus ferme la connexion. Ils s'accumulent indéfiniment.
        Sûr à supprimer : aucun processus ne les référence par leur nouveau nom.
        """
        try:
            for f in self._db_path.parent.glob(".fuse_hidden*"):
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def close(self):
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # --- Operations (journal) ---

    def log(self, plugin, operation, source_type=None, source_path=None,
            dest_path=None, status="success", details=None):
        """Enregistrer une opération dans le journal."""
        details_json = json.dumps(details, ensure_ascii=False) if details else None
        self._conn.execute(
            """INSERT INTO operations (plugin, operation, source_type, source_path,
               dest_path, status, details) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (plugin, operation, source_type, str(source_path) if source_path else None,
             str(dest_path) if dest_path else None, status, details_json))
        self._conn.commit()

    def is_processed(self, identifier, operation):
        """Vérifier si un identifiant a déjà été traité pour une opération.

        Cherche dans source_path, dest_path et les details (message_id, hash).
        """
        row = self._conn.execute(
            """SELECT 1 FROM operations
               WHERE operation = ? AND status = 'success'
               AND (source_path = ? OR dest_path = ?
                    OR details LIKE ?)
               LIMIT 1""",
            (operation, identifier, identifier, f'%{identifier}%')).fetchone()
        return row is not None

    def get_operations(self, operation=None, source_type=None, limit=100):
        """Récupérer les opérations récentes."""
        query = "SELECT * FROM operations WHERE 1=1"
        params = []
        if operation:
            query += " AND operation = ?"
            params.append(operation)
        if source_type:
            query += " AND source_type = ?"
            params.append(source_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    # --- Files (état courant) ---

    def register_file(self, path, file_type, source_type=None, source_path=None,
                      entity_type=None, entity_slug=None, created=None,
                      modified=None, message_id=None, hash=None, mtime=None):
        """Enregistrer ou mettre à jour un fichier suivi."""
        # Normalize message_id : strip whitespace au cas où le frontmatter YAML
        # aurait été parsé avec un header multi-ligne (RFC 5322 folded header).
        # Sans ça, has_message_id() ne matche plus une ré-extraction proprement.
        if message_id:
            message_id = message_id.strip()
        self._conn.execute(
            """INSERT INTO files (path, file_type, source_type, source_path,
               entity_type, entity_slug, created, modified, message_id, hash, mtime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
               file_type=excluded.file_type,
               source_type=COALESCE(excluded.source_type, source_type),
               source_path=COALESCE(excluded.source_path, source_path),
               entity_type=COALESCE(excluded.entity_type, entity_type),
               entity_slug=COALESCE(excluded.entity_slug, entity_slug),
               created=COALESCE(excluded.created, created),
               modified=COALESCE(excluded.modified, modified),
               message_id=COALESCE(excluded.message_id, message_id),
               hash=COALESCE(excluded.hash, hash),
               mtime=COALESCE(excluded.mtime, mtime),
               updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')""",
            (str(path), file_type, source_type, str(source_path) if source_path else None,
             entity_type, entity_slug, created, modified, message_id, hash, mtime))
        self._conn.commit()

    def get_file(self, path):
        """Récupérer un fichier par son chemin."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
        return dict(row) if row else None

    def move_file(self, old_path, new_path, entity_type=None, entity_slug=None):
        """Mettre à jour le chemin d'un fichier (après déplacement)."""
        self._conn.execute(
            """UPDATE files SET path = ?,
               entity_type = COALESCE(?, entity_type),
               entity_slug = COALESCE(?, entity_slug),
               updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
               WHERE path = ?""",
            (str(new_path), entity_type, entity_slug, str(old_path)))
        self._conn.commit()

    def has_message_id(self, message_id):
        """Vérifier si un message-id est déjà enregistré.

        Normalize (strip) avant la comparaison pour être résilient aux anciennes
        valeurs malformées en DB (frontmatter YAML multi-ligne qui foldait
        `\\n<id>` en ` <id>` avec espace initial).
        """
        if not message_id:
            return False
        mid = message_id.strip()
        row = self._conn.execute(
            "SELECT 1 FROM files WHERE TRIM(message_id) = ? LIMIT 1",
            (mid,)).fetchone()
        return row is not None

    def has_hash(self, hash_value):
        """Vérifier si un hash SHA256 est déjà enregistré."""
        row = self._conn.execute(
            "SELECT path FROM files WHERE hash = ? LIMIT 1",
            (hash_value,)).fetchone()
        return dict(row)["path"] if row else None

    def register_hash(self, hash_value, path, size=0):
        """Enregistrer un hash SHA256 dans la table files (type 'source').

        Utilisé pour la déduplication : les documents indexés par hash
        ne seront pas re-transcrits ni re-extraits comme PJ.
        """
        self._conn.execute(
            """INSERT INTO files (path, file_type, hash, mtime)
               VALUES (?, 'source', ?, ?)
               ON CONFLICT(path) DO UPDATE SET
               hash=excluded.hash,
               mtime=excluded.mtime,
               updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')""",
            (str(path), hash_value, size))
        self._conn.commit()

    def scan_and_register_hashes(self, directory, extensions=None, min_size=1024):
        """Scanner un dossier, hasher chaque fichier, enregistrer les nouveaux.

        Returns (added, total) counts.
        """
        import hashlib as _hashlib

        if extensions is None:
            extensions = {".pdf", ".png", ".jpg", ".jpeg", ".heic", ".webp",
                          ".tiff", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}

        directory = Path(directory)
        if not directory.exists():
            return 0, 0

        added = 0
        total = 0
        for f in sorted(directory.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in extensions:
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size < min_size:
                continue

            total += 1
            h = _hashlib.sha256(f.read_bytes()).hexdigest()

            if not self.has_hash(h):
                self.register_hash(h, str(f), size)
                added += 1

        return added, total

    def missing_resumes(self, source_type=None):
        """Trouver les transcriptions sans résumé correspondant."""
        query = """
            SELECT f.path, f.source_type, f.message_id
            FROM files f
            WHERE f.file_type = 'transcription'
            AND NOT EXISTS (
                SELECT 1 FROM files r
                WHERE r.file_type = 'resume'
                AND r.source_path = f.path
            )
        """
        params = []
        if source_type:
            query += " AND f.source_type = ?"
            params.append(source_type)
        query += " ORDER BY f.created DESC"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def unorganized_resumes(self):
        """Trouver les résumés sans entité assignée."""
        return [dict(r) for r in self._conn.execute(
            """SELECT * FROM files
               WHERE file_type = 'resume' AND entity_type IS NULL
               ORDER BY created DESC""").fetchall()]

    def stale_synthesis(self):
        """Trouver les entités dont la synthèse est périmée.

        Utilise mtime (filesystem) au lieu de updated_at (horloge DB) pour
        éviter les faux positifs après un reindex (qui touche updated_at de
        toutes les rows sans que le contenu ait changé).
        """
        return [dict(r) for r in self._conn.execute(
            """SELECT f.entity_type, f.entity_slug,
                      MAX(f.mtime) as latest_resume,
                      s.mtime as synthesis_updated
               FROM files f
               LEFT JOIN files s ON s.file_type = 'fiche'
                   AND s.entity_type = f.entity_type
                   AND s.entity_slug = f.entity_slug
               WHERE f.file_type = 'resume'
               AND f.entity_type IS NOT NULL
               GROUP BY f.entity_type, f.entity_slug
               HAVING s.mtime IS NULL
                   OR s.mtime < MAX(f.mtime)""").fetchall()]

    def stale_resumes(self):
        """Trouver les résumés dont la transcription source a été modifiée depuis.

        Compare les mtime filesystem : si transcription.mtime > résumé.mtime,
        le résumé est basé sur une ancienne version de la transcription et
        devrait être régénéré.
        """
        return [dict(r) for r in self._conn.execute(
            """SELECT r.path as resume_path, r.source_path as trans_path,
                      r.mtime as resume_mtime, t.mtime as trans_mtime
               FROM files r
               JOIN files t ON t.path = r.source_path
               WHERE r.file_type = 'resume'
               AND t.file_type = 'transcription'
               AND t.mtime IS NOT NULL AND r.mtime IS NOT NULL
               AND t.mtime > r.mtime""").fetchall()]

    # --- Stats ---

    def stats(self):
        """Statistiques globales."""
        result = {}
        for file_type in ("transcription", "resume", "fiche", "chronologie", "moc", "digest"):
            row = self._conn.execute(
                "SELECT COUNT(*) as n FROM files WHERE file_type = ?",
                (file_type,)).fetchone()
            result[file_type] = row["n"]

        row = self._conn.execute("SELECT COUNT(*) as n FROM operations").fetchone()
        result["operations"] = row["n"]

        return result
