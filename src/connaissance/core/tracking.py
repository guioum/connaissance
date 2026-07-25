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
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from connaissance.core.paths import CONNAISSANCE_ROOT, require_connaissance_root

DB_PATH = CONNAISSANCE_ROOT / ".config" / "tracking.db"
# Artefacts couplés à la base, sous .config/ (cf. paths : .config = couplé DB).
# Journaux append-only sur disque : la DB en devient reconstructible (les
# tables file_ledger / llm_usage sont des enregistrements primaires, pas
# dérivables du frontmatter — sans copie disque, elles seraient perdues si la
# DB l'était). `backups/` = snapshots avant opérations risquées.
BACKUPS_DIR = DB_PATH.parent / "backups"
JOURNAL_DIR = DB_PATH.parent / "journal"
LEDGER_JOURNAL_DIR = JOURNAL_DIR / "ledger"
USAGE_JOURNAL = JOURNAL_DIR / "llm_usage.jsonl"


def snapshot_db(reason: str = "", *, keep: int = 10,
                db_path: Path | None = None) -> str | None:
    """Copie **consistante** de tracking.db avant une opération risquée.

    ``VACUUM INTO`` produit un snapshot propre (gère le WAL, pas de demi-écriture).
    Garde les ``keep`` plus récents (purge des plus vieux). Retourne le chemin du
    snapshot, ou ``None`` si la base n'existe pas encore. Best-effort : une erreur
    de copie n'interrompt pas l'appelant (elle est remontée par l'absence de
    retour, jamais en exception non gérée côté run)."""
    src = db_path or DB_PATH
    if not Path(src).exists():
        return None
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    tag = re.sub(r"[^a-z0-9]+", "-", (reason or "").lower()).strip("-")[:30]
    dest = BACKUPS_DIR / f"tracking-{stamp}{('-' + tag) if tag else ''}.db"
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    snaps = sorted(BACKUPS_DIR.glob("tracking-*.db"))
    for old in snaps[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return str(dest)


def _append_jsonl(path: Path, row: dict) -> None:
    """Ajouter une ligne JSON à un journal append-only (best-effort).

    Une erreur d'écriture du journal ne doit JAMAIS casser l'opération DB
    qu'il double — la DB reste la copie de travail, le JSONL la copie durable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _nfc(rel_path) -> str:
    """Clé de cache normalisée NFC — macOS écrit les chemins en NFD (accents
    décomposés) ; on canonicalise pour que stockage et lecture matchent quelle
    que soit la source du chemin (walk filesystem NFD vs littéral/CLI NFC)."""
    return unicodedata.normalize("NFC", str(rel_path))


# Bruit de dossier d'origine : sujets `classify` provisoires dérivés de noms de
# dossiers du ~/Documents chaotique (pas de vrais thèmes). Filtrés de la vue
# « - Sujets » (jamais des sujets `resume`, propres par construction).
_JUNK_SUJET_SUBSTR = (
    "archive", "triage", "takeout", "non-organis", "sans-tag", "sans-titre",
    "boite-de-reception", "boîte-de-réception", "vrac", "telechargement",
    "téléchargement", "download", "scanner", "doxie", "untitled", "_mois_",
)
_JUNK_SUJET_EXACT = {
    "divers", "document", "documents", "fichier", "fichiers", "scan", "scans",
    "material", "materiel", "matériel", "note", "notes", "autre", "autres",
    "mois", "tmp", "temp",
}


def _is_junk_sujet(s: str) -> bool:
    """Vrai si un sujet provisoire (`classify`) est du bruit de dossier : date
    nue (``2018``, ``2018-02``), artefact de triage/archive, ou terme générique
    non thématique."""
    s = unicodedata.normalize("NFC", (s or "")).strip().lower()
    if len(s) < 2 or s in _JUNK_SUJET_EXACT:
        return True
    if s.replace("-", "").isdigit():        # 2018, 2018-02, 20180101…
        return True
    return any(tok in s for tok in _JUNK_SUJET_SUBSTR)


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
    size INTEGER,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_file_type ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_entity ON files(entity_type, entity_slug);
CREATE INDEX IF NOT EXISTS idx_files_message_id ON files(message_id);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
-- idx_files_size créé dans _migrate() après ALTER TABLE ADD COLUMN size,
-- pour rester compatible avec les DB v2.13.0 et antérieures.

-- Cache des SimHash texte des TRANSCRIPTIONS (détection de quasi-doublons du
-- corpus). Univers : ~/Connaissance/Transcriptions/**. rel_path relatif à
-- CONNAISSANCE_ROOT (clé stable Mac natif ~/Connaissance comme cowork VM
-- ~/mnt/Connaissance). NFC-normalisé, validé par (size, mtime).
-- ⚠️ Ne PAS y mettre de SimHash de fichiers bruts ~/Documents : référentiel
-- différent → table doc_simhash ci-dessous (Phase D).
CREATE TABLE IF NOT EXISTS text_simhash (
    rel_path TEXT NOT NULL UNIQUE,
    simhash TEXT,
    size INTEGER,
    mtime REAL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_text_simhash ON text_simhash(simhash);

-- Cache des SimHash texte des FICHIERS BRUTS ~/Documents (Phase D — doublons
-- du pré-classement). Même forme que text_simhash mais référentiel DISTINCT :
-- rel_path relatif à DOCUMENTS_DIR (~/Documents), comme doc_signals /
-- doc_classification. Table séparée par conception : un seul référentiel par
-- table, jamais de collision corpus ↔ bruts. NFC-normalisé.
CREATE TABLE IF NOT EXISTS doc_simhash (
    rel_path TEXT NOT NULL UNIQUE,
    simhash TEXT,
    size INTEGER,
    mtime REAL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_doc_simhash ON doc_simhash(simhash);

-- Cache des paquets de signaux Phase B (documents signals), keyé sur
-- (rel_path, size, mtime) comme text_simhash. Évite de re-parser/relire un
-- document inchangé. `signals` = JSON sérialisé du paquet.
CREATE TABLE IF NOT EXISTS doc_signals (
    rel_path TEXT NOT NULL UNIQUE,
    signals TEXT,
    size INTEGER,
    mtime REAL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- Étage « classement » de la fiche d'identité d'un document (Phase C). Jointe à
-- doc_signals par rel_path. `hash` sert d'ancre stable quand le fichier bouge
-- (relink du rel_path à l'apply). État courant mutable, raffiné à chaque passe.
CREATE TABLE IF NOT EXISTS doc_classification (
    rel_path TEXT NOT NULL UNIQUE,
    hash TEXT,
    entity TEXT,
    entity_type TEXT,
    entity_slug TEXT,
    category TEXT,
    date TEXT,
    title TEXT,
    sujet TEXT,
    confidence TEXT,
    status TEXT,
    model TEXT,
    reasons TEXT,
    size INTEGER,
    mtime REAL,
    classified_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- Index doc_classification(status) et (entity_slug) créés dans _migrate(),
-- après les ALTER TABLE : sur une base ancienne ces colonnes peuvent manquer
-- au moment du SCHEMA, ce qui ferait échouer la création d'index.

-- Appartenances MULTI-SUJET d'un document (un doc physique → N sujets).
-- Permet à la vue virtuelle « - Sujets » de montrer un fichier sous tous ses
-- contextes. Alimentée par classify (source 'classify', le sujet primaire) et
-- par la dédup consciente du contexte (source 'dedup', les dossiers des copies
-- supprimées) — voir docs/reorganisation.md. rel_path relatif à ~/Documents.
CREATE TABLE IF NOT EXISTS doc_sujets (
    rel_path TEXT NOT NULL,
    sujet TEXT NOT NULL,
    source TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE(rel_path, sujet)
);

CREATE INDEX IF NOT EXISTS idx_doc_sujets_sujet ON doc_sujets(sujet);
CREATE INDEX IF NOT EXISTS idx_doc_sujets_rel ON doc_sujets(rel_path);

-- Registre canonique d'entités (personnes/organismes), VIVANT : seedé depuis les
-- dossiers rangés + le backup, puis enrichi de batch en batch par `register`
-- (ajout d'une entité découverte si nouvelle, sinon rattachement par alias).
-- C'est la source de `known_entities()` injectée dans les prompts (canonique +
-- aliases → le modèle rabat les variantes, anti-fragmentation). Clé = (type, slug)
-- avec slug = construire_slug(name) (accents conservés). aliases = JSON list.
CREATE TABLE IF NOT EXISTS entities (
    type TEXT NOT NULL,            -- 'organismes' | 'personnes'
    slug TEXT NOT NULL,            -- construire_slug(name), accents conservés
    name TEXT NOT NULL,            -- nom canonique d'affichage
    aliases TEXT,                  -- JSON: variantes que le modèle doit rabattre
    doc_count INTEGER NOT NULL DEFAULT 0,
    status TEXT,                   -- 'seed' | 'active'
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE(type, slug)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

-- Ledger journalisé des opérations de fichiers, réversible. Chaque ligne
-- 'applied' enregistre l'ancien et le nouveau chemin + le SHA256, ce qui permet
-- un rollback vérifié (on ne restaure que si le fichier est intact). Les
-- opérations partagent un run_id (1 run = 1 lot révertible).
--   op     : 'move' | 'rename' (organisation) | 'trash' (corbeille ledger :
--            suppression différée, le fichier est sous ~/Connaissance/.trash/).
--   status : 'applied' (en vigueur) | 'reverted' (annulé par revert) |
--            'purged' (corbeille vidée définitivement par `ledger purge`).
CREATE TABLE IF NOT EXISTS file_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    op TEXT NOT NULL,
    old_path TEXT,
    new_path TEXT,
    sha256 TEXT,
    size INTEGER,
    mtime REAL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'applied'
);

CREATE INDEX IF NOT EXISTS idx_ledger_run ON file_ledger(run_id);
CREATE INDEX IF NOT EXISTS idx_ledger_sha ON file_ledger(sha256);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    operation TEXT NOT NULL,
    source_type TEXT,
    source_path TEXT,
    dest_path TEXT,
    custom_id TEXT,
    model TEXT,
    mode TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    units INTEGER,            -- unité non-token (pages OCR Mistral) ; NULL pour Claude
    cost_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_timestamp ON llm_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_usage_operation ON llm_usage(operation);
CREATE INDEX IF NOT EXISTS idx_llm_usage_source_type ON llm_usage(source_type);

-- Journal de la passe `documents ocr-images` : UNE ligne par image traitée
-- (document OU non-document). Permet une reprise idempotente d'un balayage long
-- sans re-OCRiser les photos déjà classées, et garde la trace des décisions
-- (densité de texte → doc/photo). `is_document` 1/0 ; `chars`/`confidence` =
-- mesure Vision. Additive, CREATE IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS image_ocr_log (
    rel_path TEXT PRIMARY KEY,        -- relatif à ~/Documents
    is_document INTEGER NOT NULL,     -- 1 = document (transcription écrite), 0 = photo
    chars INTEGER,
    confidence REAL,
    processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_image_ocr_log_isdoc ON image_ocr_log(is_document);
"""


# Tarifs USD / million de tokens — alignés sur claude-api-mcp/src/anthropic.ts.
# Le prompt ephemeral cached applique 1.25× au write et 0.10× au read.
PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-opus-4-5-20250929": {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0},
    "claude-haiku-4-5": {"input": 0.8, "output": 4.0},
}
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0}


# Prix Mistral OCR 4 (déjà tarif batch : $2 / 1000 pages ; OCR 3 était à
# $1/1000, migration 2026-07-19 — modèle épinglé dans mistral-ocr/cli.py).
# Source unique du coût OCR : journal ``llm_usage`` (tokens=0, units=pages)
# ET estimateur de ``documents transcribe-plan`` (import dans commands/ocr).
MISTRAL_PAGE_COST_USD = 0.002


def compute_cost_usd(model: str | None, usage: dict,
                     batch: bool = False) -> float | None:
    """Calculer le coût USD d'un appel LLM à partir du ``usage`` Anthropic.

    ``usage`` doit contenir ``input_tokens``, ``output_tokens`` et optionnellement
    ``cache_creation_input_tokens`` et ``cache_read_input_tokens``. Retourne
    ``None`` si le dict est vide.

    ``batch=True`` applique la **remise Batch API −50 %** : une opération soumise
    via ``messages/batches`` est facturée la moitié du tarif direct. Sans ce flag,
    le coût journalisé d'un batch serait 2× trop élevé.
    """
    if not usage:
        return None
    pricing = PRICING_USD_PER_MTOK.get(model or "", _DEFAULT_PRICING)
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cw = usage.get("cache_creation_input_tokens") or 0
    cr = usage.get("cache_read_input_tokens") or 0
    input_cost = (
        inp * pricing["input"]
        + cw * pricing["input"] * 1.25
        + cr * pricing["input"] * 0.10
    ) / 1_000_000
    output_cost = out * pricing["output"] / 1_000_000
    total = input_cost + output_cost
    if batch:
        total *= 0.5
    return round(total, 6)


class TrackingDB:
    """Interface SQLite pour le tracking des opérations."""

    def __init__(self, db_path=None):
        self._db_path = db_path or DB_PATH
        self._entity_idx = None   # cache resolve_entity (cf. _entity_index)
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
        self._migrate()
        self._conn.commit()

    # Type SQLite des colonnes non-TEXT (défaut TEXT pour les autres). Sert à
    # la migration générique ci-dessous.
    _COL_TYPES = {"size": "INTEGER", "mtime": "REAL"}

    def _expected_columns(self) -> dict[str, dict[str, str]]:
        """Colonnes attendues par table, au-delà du CREATE d'origine.

        ``_migrate()`` ajoute ce qui manque sur une base ancienne. La fiche
        ``doc_classification`` est dérivée de ``_CLS_COLS`` : ajouter une
        colonne à ``_CLS_COLS`` suffit à migrer les bases existantes (son
        ``INSERT`` est généré dynamiquement — sans migration, il planterait).
        """
        return {
            "files": {"size": "INTEGER"},
            "doc_classification": {c: self._COL_TYPES.get(c, "TEXT")
                                   for c in self._CLS_COLS},
            "llm_usage": {"units": "INTEGER"},
        }

    def _migrate(self):
        """Migrations légères pour bases existantes.

        ``CREATE TABLE IF NOT EXISTS`` ne modifie pas une table déjà créée
        avec un schéma plus ancien. Pour chaque table, on ajoute les colonnes
        manquantes (``PRAGMA table_info`` vs colonnes attendues) puis les
        index associés. Tout est idempotent. Les noms de tables/colonnes sont
        des constantes internes (jamais d'entrée utilisateur).
        """
        for table, expected in self._expected_columns().items():
            existing = {r[1] for r in
                        self._conn.execute(
                            f"PRAGMA table_info({table})").fetchall()}
            if not existing:
                continue  # table absente (ne devrait pas arriver après SCHEMA)
            for col, coltype in expected.items():
                if col not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        # Index créés ici (pas dans SCHEMA) car ils portent sur des colonnes
        # potentiellement ajoutées par les ALTER ci-dessus : sur une base
        # ancienne, les créer dans SCHEMA échouerait (colonne inexistante).
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_cls_status "
            "ON doc_classification(status)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_cls_entity "
            "ON doc_classification(entity_slug)")
        # Normalisation des message_id hérités (frontmatter YAML replié qui
        # produisait ` <id>` avec espace initial). Une fois la colonne propre,
        # `has_message_id` peut comparer en égalité stricte et profiter de
        # l'index — le `TRIM()` historique dans le WHERE forçait un full scan
        # de `files` à CHAQUE message d'une extraction. Idempotent.
        self._conn.execute(
            "UPDATE files SET message_id = TRIM(message_id) "
            "WHERE message_id IS NOT NULL AND message_id != TRIM(message_id)")

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

    @contextmanager
    def transaction(self):
        """Grouper plusieurs écritures en une unité atomique.

        ``COMMIT`` à la sortie normale, ``ROLLBACK`` sur exception. Les méthodes
        appelées dans le bloc doivent passer ``commit=False`` pour ne pas
        committer prématurément (sinon l'atomicité est rompue). Sert notamment
        à `classify apply` pour que l'enregistrement ledger et le relink de la
        fiche soient indissociables.

        Note : une opération filesystem (``shutil.move``) exécutée dans le bloc
        n'est PAS annulée par le rollback — seules les écritures SQLite le sont.
        """
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def commit(self):
        """Committer la transaction implicite courante.

        Pour les boucles d'ingestion : passer ``commit=False`` aux méthodes
        d'écriture puis committer une fois le lot terminé (un fsync par lot
        au lieu d'un par ligne). ``close()`` sans commit préalable abandonne
        les écritures non committées — c'est voulu (pas de lot partiel)."""
        self._conn.commit()

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
            dest_path=None, status="success", details=None,
            *, commit: bool = True):
        """Enregistrer une opération dans le journal."""
        details_json = json.dumps(details, ensure_ascii=False) if details else None
        self._conn.execute(
            """INSERT INTO operations (plugin, operation, source_type, source_path,
               dest_path, status, details) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (plugin, operation, source_type, str(source_path) if source_path else None,
             str(dest_path) if dest_path else None, status, details_json))
        if commit:
            self._conn.commit()

    def is_processed(self, identifier, operation):
        """Vérifier si un identifiant a déjà été traité pour une opération.

        Cherche dans source_path, dest_path et les details (message_id, hash).
        """
        # Échapper les wildcards SQL (% et _) pour éviter qu'un identifiant
        # contenant l'un d'eux ne produise des faux positifs dans LIKE.
        escaped = identifier.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        row = self._conn.execute(
            r"""SELECT 1 FROM operations
                WHERE operation = ? AND status = 'success'
                AND (source_path = ? OR dest_path = ?
                     OR details LIKE ? ESCAPE '\')
                LIMIT 1""",
            (operation, identifier, identifier, f'%{escaped}%')).fetchone()
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
                      modified=None, message_id=None, hash=None, mtime=None,
                      size=None, *, commit: bool = True):
        """Enregistrer ou mettre à jour un fichier suivi."""
        # Normalize message_id : strip whitespace au cas où le frontmatter YAML
        # aurait été parsé avec un header multi-ligne (RFC 5322 folded header).
        # Sans ça, has_message_id() ne matche plus une ré-extraction proprement.
        if message_id:
            message_id = message_id.strip()
        self._conn.execute(
            """INSERT INTO files (path, file_type, source_type, source_path,
               entity_type, entity_slug, created, modified, message_id, hash, mtime, size)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
               size=COALESCE(excluded.size, size),
               updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')""",
            (str(path), file_type, source_type, str(source_path) if source_path else None,
             entity_type, entity_slug, created, modified, message_id, hash, mtime, size))
        if commit:
            self._conn.commit()

    def get_file(self, path):
        """Récupérer un fichier par son chemin."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
        return dict(row) if row else None

    def rename_text_simhash(self, old_rel, new_rel, *, commit: bool = True) -> int:
        """Repointer la clé d'une transcription dans text_simhash (rel relatif à
        CONNAISSANCE_ROOT) après déplacement. ``UPDATE OR IGNORE`` (collision →
        garde l'existant). Retourne le nombre de lignes touchées."""
        cur = self._conn.execute(
            "UPDATE OR IGNORE text_simhash SET rel_path = ? WHERE rel_path = ?",
            (str(new_rel), str(old_rel)))
        n = cur.rowcount
        if commit:
            self._conn.commit()
        return n

    def move_file(self, old_path, new_path, entity_type=None, entity_slug=None,
                  *, commit: bool = True):
        """Mettre à jour le chemin d'un fichier (après déplacement)."""
        self._conn.execute(
            """UPDATE files SET path = ?,
               entity_type = COALESCE(?, entity_type),
               entity_slug = COALESCE(?, entity_slug),
               updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
               WHERE path = ?""",
            (str(new_path), entity_type, entity_slug, str(old_path)))
        if commit:
            self._conn.commit()

    def has_message_id(self, message_id):
        """Vérifier si un message-id est déjà enregistré.

        Comparaison en égalité stricte (indexable) : les valeurs en DB sont
        normalisées à l'écriture (``register_file`` strip) et les valeurs
        héritées malformées sont assainies par la migration ``_migrate``.
        """
        if not message_id:
            return False
        mid = message_id.strip()
        row = self._conn.execute(
            "SELECT 1 FROM files WHERE message_id = ? LIMIT 1",
            (mid,)).fetchone()
        return row is not None

    def has_hash(self, hash_value):
        """Vérifier si un hash SHA256 est déjà enregistré."""
        row = self._conn.execute(
            "SELECT path FROM files WHERE hash = ? LIMIT 1",
            (hash_value,)).fetchone()
        return dict(row)["path"] if row else None

    def has_size(self, size: int) -> bool:
        """Vérifier si une taille de fichier est présente dans l'index.

        Préfiltre JIT : un fichier candidat ne peut être un doublon que si une
        autre entrée partage sa taille. Permet d'éviter le hash quand la taille
        est unique.
        """
        row = self._conn.execute(
            "SELECT 1 FROM files WHERE size = ? LIMIT 1", (int(size),)).fetchone()
        return row is not None

    def files_with_size(self, size: int,
                        exclude_path: str | None = None) -> list[dict]:
        """Lister les entrées partageant une taille donnée.

        Retourne des dicts ``{path, size, mtime, hash, file_type}``. Utilisé
        pour résoudre une collision de taille en hash ciblé.
        """
        rows = self._conn.execute(
            """SELECT path, size, mtime, hash, file_type
               FROM files WHERE size = ?""", (int(size),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if exclude_path and d["path"] == exclude_path:
                continue
            out.append(d)
        return out

    def upsert_stat(self, path, size: int, mtime: float,
                    file_type: str = "source") -> None:
        """Enregistrer ``(path, size, mtime)`` sans toucher au hash (stat-only).

        Si le couple ``(size, mtime)`` a changé par rapport à la ligne existante,
        invalide le ``hash`` (NULL) — le fichier a été modifié, l'ancien hash
        ne s'applique plus.
        """
        row = self._conn.execute(
            "SELECT size, mtime FROM files WHERE path = ?", (str(path),)).fetchone()
        if row is not None:
            old = dict(row)
            changed = (old.get("size") != int(size)
                       or old.get("mtime") != float(mtime))
            if changed:
                self._conn.execute(
                    """UPDATE files SET size = ?, mtime = ?, hash = NULL,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
                       WHERE path = ?""",
                    (int(size), float(mtime), str(path)))
            else:
                self._conn.execute(
                    """UPDATE files SET
                       updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
                       WHERE path = ?""",
                    (str(path),))
        else:
            self._conn.execute(
                """INSERT INTO files (path, file_type, size, mtime)
                   VALUES (?, ?, ?, ?)""",
                (str(path), file_type, int(size), float(mtime)))
        self._conn.commit()

    def register_hash(self, hash_value, path, size=0, mtime: float | None = None):
        """Enregistrer un hash SHA256 dans la table files (type 'source').

        Utilisé pour la déduplication : les documents indexés par hash
        ne seront pas re-transcrits ni re-extraits comme PJ. ``size`` et
        ``mtime`` (si fournis) alimentent le cache JIT pour éviter de
        rehasher le fichier aux runs suivants.
        """
        self._conn.execute(
            """INSERT INTO files (path, file_type, hash, size, mtime)
               VALUES (?, 'source', ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
               hash=excluded.hash,
               size=COALESCE(excluded.size, size),
               mtime=COALESCE(excluded.mtime, mtime),
               updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')""",
            (str(path), hash_value, int(size) if size else None, mtime))
        self._conn.commit()

    def get_or_compute_hash(self, path,
                            compute_fn=None, read_path=None) -> str | None:
        """Récupérer le hash d'un fichier depuis le cache, ou le calculer.

        Cœur du pipeline JIT :

        - ``stat()`` ``path`` (canonique — clé de cache et de stat). Si la ligne
          DB existe avec ``(size, mtime)`` identiques et un ``hash`` non NULL,
          retourner ce hash sans lire le fichier.
        - Sinon, lire + hasher le **contenu** depuis ``read_path`` s'il est
          fourni (ex. un miroir SSD local pour éviter un téléchargement iCloud),
          sinon depuis ``path``. Persister ``(hash, size, mtime)`` sous ``path``.

        ``path`` reste l'identité (clé, frontmatter, DB) ; ``read_path`` n'est
        qu'une source de lecture alternative — voir ``paths.documents_read_path``.
        ``stat()`` seul ne télécharge jamais un fichier iCloud dataless.

        Retourne ``None`` si le fichier est inaccessible.
        """
        try:
            st = Path(path).stat()
        except OSError:
            return None
        size = int(st.st_size)
        mtime = float(st.st_mtime)

        row = self._conn.execute(
            "SELECT hash, size, mtime FROM files WHERE path = ?",
            (str(path),)).fetchone()
        if row is not None:
            d = dict(row)
            if (d.get("hash")
                    and d.get("size") == size
                    and d.get("mtime") == mtime):
                return d["hash"]

        if compute_fn is None:
            import hashlib as _hashlib

            def _default_compute(p):
                h = _hashlib.sha256()
                try:
                    with open(p, "rb") as fh:
                        for chunk in iter(lambda: fh.read(8192), b""):
                            h.update(chunk)
                    return h.hexdigest()
                except OSError:
                    return None
            compute = _default_compute
        else:
            compute = compute_fn

        # Lecture du contenu depuis le miroir si fourni ; identité = ``path``.
        h = compute(read_path if read_path is not None else path)
        if h is None:
            return None
        self.register_hash(h, str(path), size=size, mtime=mtime)
        return h

    def _get_or_compute_simhash(self, abs_path, rel_path, *, table: str,
                                compute_fn=None) -> str | None:
        """Cœur JIT du cache SimHash, paramétré par ``table``.

        Clé de cache = ``rel_path`` **normalisé NFC** (macOS écrit en NFD ;
        sans normalisation, une relecture en NFC raterait le cache). Validé par
        ``(size, mtime)``. ``table`` est une constante interne (``text_simhash``
        pour le corpus transcrit, ``doc_simhash`` pour les bruts ~/Documents) —
        jamais une entrée utilisateur.
        """
        try:
            st = Path(abs_path).stat()
        except OSError:
            return None
        size = int(st.st_size)
        mtime = float(st.st_mtime)
        rkey = _nfc(rel_path)

        row = self._conn.execute(
            f"SELECT simhash, size, mtime FROM {table} WHERE rel_path = ?",
            (rkey,)).fetchone()
        if row is not None:
            d = dict(row)
            if (d.get("simhash")
                    and d.get("size") == size
                    and d.get("mtime") == mtime):
                return d["simhash"]

        if compute_fn is None:
            from connaissance.core.dedup import simhash_text, to_hex

            def _default_compute(p):
                try:
                    txt = Path(p).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
                v = simhash_text(txt)
                return to_hex(v) if v is not None else None
            compute = _default_compute
        else:
            compute = compute_fn

        h = compute(abs_path)
        if h is None:
            return None
        self._conn.execute(
            f"""INSERT INTO {table} (rel_path, simhash, size, mtime)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(rel_path) DO UPDATE SET
                 simhash=excluded.simhash,
                 size=excluded.size,
                 mtime=excluded.mtime,
                 updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')""",
            (rkey, h, size, mtime))
        self._conn.commit()
        return h

    def get_or_compute_simhash(self, abs_path, rel_path,
                               compute_fn=None) -> str | None:
        """SimHash texte d'une **transcription** (corpus ~/Connaissance).

        - ``abs_path`` : chemin physique à lire/stat (dépend de l'environnement).
        - ``rel_path`` : chemin LOGIQUE relatif à CONNAISSANCE_ROOT — la clé de
          cache, stable entre Mac natif et cowork VM.

        Caché dans ``text_simhash``. Pour le SimHash des **fichiers bruts**
        ~/Documents (Phase D), voir ``get_or_compute_doc_simhash`` (table et
        référentiel distincts — ne JAMAIS mélanger les deux).
        """
        return self._get_or_compute_simhash(
            abs_path, rel_path, table="text_simhash", compute_fn=compute_fn)

    def get_or_compute_doc_simhash(self, abs_path, rel_path,
                                   compute_fn=None) -> str | None:
        """SimHash texte d'un **fichier brut** ~/Documents (Phase D — doublons).

        Pendant de ``get_or_compute_simhash`` pour l'univers des sources brutes :
        ``rel_path`` est relatif à ``DOCUMENTS_DIR`` (~/Documents), comme
        ``doc_signals``/``doc_classification``, et le cache vit dans la table
        **séparée** ``doc_simhash``. Séparer les tables garantit qu'un seul
        référentiel coexiste par table (jamais de collision corpus ↔ bruts).
        """
        return self._get_or_compute_simhash(
            abs_path, rel_path, table="doc_simhash", compute_fn=compute_fn)

    def get_or_compute_signals(self, abs_path, rel_path, compute_fn,
                               *, tr_mtime: float | None = None):
        """Paquet de signaux Phase B d'un document, caché par ``(rel_path, size,
        mtime)``.

        ``abs_path`` : chemin à stat (canonique — métadonnées seules, jamais de
        download). ``rel_path`` : clé de cache LOGIQUE (relatif à ~/Documents).
        ``compute_fn(abs_path) -> dict`` : calcule le paquet quand le cache est
        froid/périmé. Retourne le dict (caché ou frais), ou ``None`` si stat
        échoue.

        ``tr_mtime`` : mtime de la **transcription** du document (None si
        absente). Le paquet mémorise ce jeton (``_tr_mtime``) : une
        transcription apparue, mise à jour ou supprimée depuis le calcul
        invalide le cache — sinon un doc OCRisé après coup garderait un
        ``excerpt`` tiré de la couche PDF (voire d'une vieille couche OCR
        pourrie) au lieu de sa transcription (source prioritaire ``ocr_cache``).
        """
        import json as _json
        try:
            st = Path(abs_path).stat()
        except OSError:
            return None
        size = int(st.st_size)
        mtime = float(st.st_mtime)
        rkey = _nfc(rel_path)

        from connaissance.core.signals import SIGNALS_SCHEMA_VERSION

        row = self._conn.execute(
            "SELECT signals, size, mtime FROM doc_signals WHERE rel_path = ?",
            (rkey,)).fetchone()
        if row is not None:
            d = dict(row)
            if d.get("signals") and d.get("size") == size and d.get("mtime") == mtime:
                try:
                    packet = _json.loads(d["signals"])
                    # Recalcule si le paquet caché est d'une version antérieure
                    # du schéma (p. ex. sans le champ `excerpt`, schéma v1) ou
                    # si la transcription a changé depuis (jeton `_tr_mtime` —
                    # les vieux paquets sans jeton ne restent valides que si le
                    # doc n'a toujours pas de transcription).
                    if packet.get("_v") == SIGNALS_SCHEMA_VERSION \
                            and packet.get("_tr_mtime") == tr_mtime:
                        return packet
                except (ValueError, TypeError):
                    pass  # cache corrompu → recalculer

        packet = compute_fn(abs_path)
        if packet is None:
            return None
        packet["_tr_mtime"] = tr_mtime
        self._conn.execute(
            """INSERT INTO doc_signals (rel_path, signals, size, mtime)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(rel_path) DO UPDATE SET
                 signals=excluded.signals,
                 size=excluded.size,
                 mtime=excluded.mtime,
                 updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')""",
            (rkey, _json.dumps(packet, ensure_ascii=False), size, mtime))
        self._conn.commit()
        return packet

    # --- Fiche d'identité : étage « classement » (doc_classification) ---

    _CLS_COLS = ("hash", "entity", "entity_type", "entity_slug", "category",
                 "date", "title", "sujet", "confidence", "status", "model",
                 "reasons", "size", "mtime")

    def get_signals_row(self, rel_path):
        """Paquet de signaux brut (dict) caché pour ``rel_path``, ou None."""
        import json as _json
        row = self._conn.execute(
            "SELECT signals FROM doc_signals WHERE rel_path = ?",
            (_nfc(rel_path),)).fetchone()
        if not row or not row["signals"]:
            return None
        try:
            return _json.loads(row["signals"])
        except (ValueError, TypeError):
            return None

    def all_doc_signals(self) -> list[tuple[str, dict]]:
        """Tous les paquets de signaux cachés : ``[(rel_path, packet_dict)]``.

        Source de la Phase D (doublons) — déjà sans secrets ni conteneurs
        (élagués par ``documents signals`` à l'écriture)."""
        import json as _json
        out: list[tuple[str, dict]] = []
        for r in self._conn.execute(
                "SELECT rel_path, signals FROM doc_signals "
                "WHERE signals IS NOT NULL ORDER BY rel_path"):
            try:
                out.append((r[0], _json.loads(r[1])))
            except (ValueError, TypeError):
                continue
        return out

    # --- Journal de la passe ocr-images (reprise idempotente) ---

    def image_ocr_logged_rels(self) -> set[str]:
        """Ensemble des ``rel_path`` d'images déjà traitées par ``ocr-images``
        (document OU photo). Sert à reprendre un balayage sans re-OCRiser."""
        return {r[0] for r in self._conn.execute(
            "SELECT rel_path FROM image_ocr_log")}

    def log_image_ocr(self, rel_path: str, is_document: bool,
                      chars: int | None, confidence: float | None) -> None:
        """Journaliser le verdict Vision d'une image (idempotent par rel)."""
        self._conn.execute(
            """INSERT INTO image_ocr_log (rel_path, is_document, chars, confidence)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(rel_path) DO UPDATE SET
                 is_document=excluded.is_document, chars=excluded.chars,
                 confidence=excluded.confidence,
                 processed_at=strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')""",
            (_nfc(rel_path), 1 if is_document else 0, chars, confidence))
        self._conn.commit()

    def upsert_classification(self, rel_path, data: dict,
                              *, commit: bool = True) -> None:
        """Insérer/rafraîchir l'étage classement de la fiche d'un document."""
        import json as _json
        vals = {c: data.get(c) for c in self._CLS_COLS}
        if isinstance(vals["reasons"], (list, dict)):
            vals["reasons"] = _json.dumps(vals["reasons"], ensure_ascii=False)
        cols = ", ".join(self._CLS_COLS)
        ph = ", ".join("?" * len(self._CLS_COLS))
        setexpr = ", ".join(f"{c}=excluded.{c}" for c in self._CLS_COLS)
        self._conn.execute(
            f"""INSERT INTO doc_classification (rel_path, {cols})
                VALUES (?, {ph})
                ON CONFLICT(rel_path) DO UPDATE SET {setexpr},
                  updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')""",
            (_nfc(rel_path), *[vals[c] for c in self._CLS_COLS]))
        if commit:
            self._conn.commit()

    def get_classification(self, rel_path):
        """Étage classement (dict) de la fiche d'un document, ou None."""
        row = self._conn.execute(
            "SELECT * FROM doc_classification WHERE rel_path = ?",
            (_nfc(rel_path),)).fetchone()
        return dict(row) if row else None

    def classification_summary(self) -> dict:
        """Compteurs corpus de l'étage classement (statut, catégorie, type)."""
        def _by(col):
            return {r[0]: r[1] for r in self._conn.execute(
                f"SELECT {col}, COUNT(*) FROM doc_classification "
                f"WHERE {col} IS NOT NULL GROUP BY {col} ORDER BY COUNT(*) DESC")}
        total = self._conn.execute(
            "SELECT COUNT(*) FROM doc_classification").fetchone()[0]
        return {"total": total, "by_status": _by("status"),
                "by_category": _by("category"), "by_entity_type": _by("entity_type"),
                "by_entity": _by("entity")}

    def distinct_entities(self) -> list[dict]:
        """Entités EN USAGE dans les fiches de classement : ``[{entity,
        entity_type, entity_slug, count}]``, pour la détection de doublons
        d'entités (``entities candidates``)."""
        rows = self._conn.execute(
            """SELECT entity, entity_type, entity_slug, COUNT(*) AS count
               FROM doc_classification
               WHERE entity_slug IS NOT NULL AND TRIM(entity_slug) != ''
               GROUP BY entity_type, entity_slug
               ORDER BY count DESC""").fetchall()
        return [dict(r) for r in rows]

    def reassign_entity(self, from_type, from_slug, to_type, to_slug,
                        to_name, *, commit: bool = True) -> int:
        """Repointer les fiches de classement d'une entité vers une autre
        (fusion). Retourne le nombre de lignes mises à jour."""
        cur = self._conn.execute(
            """UPDATE doc_classification
               SET entity = ?, entity_type = ?, entity_slug = ?
               WHERE entity_type = ? AND entity_slug = ?""",
            (to_name, to_type, to_slug, from_type, from_slug))
        n = cur.rowcount
        # Table `files` (corpus) : même repointage si l'entité y figure.
        self._conn.execute(
            """UPDATE files SET entity_type = ?, entity_slug = ?
               WHERE entity_type = ? AND entity_slug = ?""",
            (to_type, to_slug, from_type, from_slug))
        if commit:
            self._conn.commit()
        return n

    def rename_slug(self, old_type, old_slug, new_slug,
                    *, commit: bool = True) -> dict:
        """Renommer un slug PARTOUT dans la base (ré-accentuation, etc.).

        Met à jour : ``entity_slug`` (doc_classification + files), les **segments
        de ``rel_path``** ``<type>/<old>/…`` → ``<type>/<new>/…`` (doc_classification,
        doc_signals, doc_sujets), et les **valeurs de sujet** égales au slug
        (doc_sujets.sujet, doc_classification.sujet). Retourne des compteurs.
        ``UPDATE OR IGNORE`` partout pour absorber d'éventuelles collisions.
        """
        old_seg = _nfc(f"{old_type}/{old_slug}/")
        new_seg = _nfc(f"{old_type}/{new_slug}/")
        c: dict = {}
        c["entity_doc_classification"] = self._conn.execute(
            "UPDATE doc_classification SET entity_slug=? "
            "WHERE entity_type=? AND entity_slug=?",
            (new_slug, old_type, old_slug)).rowcount
        c["entity_files"] = self._conn.execute(
            "UPDATE files SET entity_slug=? WHERE entity_type=? AND entity_slug=?",
            (new_slug, old_type, old_slug)).rowcount
        for tbl in ("doc_classification", "doc_signals", "doc_sujets"):
            rows = self._conn.execute(
                f"SELECT rowid, rel_path FROM {tbl} WHERE rel_path LIKE ?",
                (old_seg + "%",)).fetchall()
            for r in rows:
                nrp = new_seg + r["rel_path"][len(old_seg):]
                self._conn.execute(
                    f"UPDATE OR IGNORE {tbl} SET rel_path=? WHERE rowid=?",
                    (nrp, r["rowid"]))
            c[f"rel_{tbl}"] = len(rows)
        c["sujet_doc_sujets"] = self._conn.execute(
            "UPDATE OR IGNORE doc_sujets SET sujet=? WHERE sujet=?",
            (new_slug, old_slug)).rowcount
        c["sujet_doc_classification"] = self._conn.execute(
            "UPDATE doc_classification SET sujet=? WHERE sujet=?",
            (new_slug, old_slug)).rowcount
        # Registre `entities` : suivre le renommage de slug (re-accentuation…).
        c["entities"] = self._conn.execute(
            "UPDATE OR IGNORE entities SET slug=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime') "
            "WHERE type=? AND slug=?", (new_slug, old_type, old_slug)).rowcount
        self._invalidate_entity_index()
        if commit:
            self._conn.commit()
        return c

    def classifications_with_sujet(self) -> list[dict]:
        """Fiches ayant un ``sujet`` non vide, pour la vue virtuelle ``- Sujets``.

        Retourne ``rel_path``/``sujet``/``entity_type``/``entity_slug``/
        ``category`` — la source de vérité du sujet d'un document est cette
        colonne (pas de frontmatter sur un PDF brut)."""
        rows = self._conn.execute(
            """SELECT rel_path, sujet, entity_type, entity_slug, category
               FROM doc_classification
               WHERE sujet IS NOT NULL AND TRIM(sujet) != ''
               ORDER BY sujet, rel_path""").fetchall()
        return [dict(r) for r in rows]

    # --- Appartenances multi-sujet (doc_sujets) ---

    def add_doc_sujets(self, rel_path, sujets, source: str,
                       *, commit: bool = True) -> int:
        """Ajouter des appartenances (rel_path, sujet) — un doc → N sujets.

        Idempotent (``INSERT OR IGNORE`` sur ``UNIQUE(rel_path, sujet)``).
        Retourne le nombre de lignes effectivement ajoutées."""
        rkey = _nfc(rel_path)
        added = 0
        for s in sujets:
            s = (s or "").strip()
            if not s:
                continue
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO doc_sujets (rel_path, sujet, source) "
                "VALUES (?, ?, ?)", (rkey, s, source))
            added += cur.rowcount
        if commit:
            self._conn.commit()
        return added

    def sujet_memberships(self) -> list[dict]:
        """Appartenances ``(rel_path, sujet)`` pour la vue ``- Sujets``, avec
        **précédence par maturité de source** (pas une union plate) :

        - ``resume`` (sujet de CONTENU, issu du résumé) fait autorité : s'il
          existe pour un document, il **supersède** le sujet ``classify``
          provisoire (deviné du dossier d'origine) — on n'affiche que lui.
        - sinon, repli sur ``classify`` **filtré du bruit de dossier** (dates,
          ``archive-*``, ``non-organisées``…).
        - ``dedup`` (cross-filing) est toujours ajouté (contexte multi-sujet).

        Le sujet primaire de ``doc_classification`` est traité comme ``classify``
        (compat des fiches d'avant ``doc_sujets``).
        """
        by_rel: dict[str, dict[str, set]] = {}
        rows = self._conn.execute(
            "SELECT rel_path, sujet, COALESCE(source,'classify') src "
            "FROM doc_sujets WHERE TRIM(sujet) != ''").fetchall()
        for r in rows:
            by_rel.setdefault(r["rel_path"], {}).setdefault(r["src"], set()).add(r["sujet"])
        # Sujet primaire des fiches (compat) = niveau classify.
        for r in self._conn.execute(
                "SELECT rel_path, sujet FROM doc_classification "
                "WHERE sujet IS NOT NULL AND TRIM(sujet) != ''").fetchall():
            by_rel.setdefault(r["rel_path"], {}).setdefault("classify", set()).add(r["sujet"])

        out: list[dict] = []
        for rel, src_map in by_rel.items():
            primary = (src_map.get("resume")
                       or {s for s in src_map.get("classify", set())
                           if not _is_junk_sujet(s)})
            for s in primary | src_map.get("dedup", set()):
                out.append({"rel_path": rel, "sujet": s})
        out.sort(key=lambda d: (d["sujet"], d["rel_path"]))
        return out

    # --- Registre canonique d'entités (table entities) ---

    def upsert_entity(self, etype: str, slug: str, name: str,
                      aliases=None, *, inc_count: int = 0,
                      status: str = "active", commit: bool = True) -> None:
        """Insérer ou mettre à jour une entité du registre. Fusionne les aliases
        (dédup casse-insensible, jamais le nom canonique lui-même), incrémente
        ``doc_count`` de ``inc_count``. Clé = (type, slug)."""
        import json as _json
        slug = _nfc(slug)
        aliases = [a for a in (aliases or []) if a and a.strip()]
        row = self._conn.execute(
            "SELECT name, aliases FROM entities WHERE type=? AND slug=?",
            (etype, slug)).fetchone()
        if row:
            merged = list(_json.loads(row["aliases"] or "[]"))
            seen = {a.lower() for a in merged} | {(row["name"] or "").lower()}
            for a in aliases:
                if a.lower() not in seen:
                    merged.append(a); seen.add(a.lower())
            self._conn.execute(
                "UPDATE entities SET aliases=?, doc_count=doc_count+?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime') "
                "WHERE type=? AND slug=?",
                (_json.dumps(merged, ensure_ascii=False), inc_count, etype, slug))
        else:
            al, seen = [], {(name or "").lower()}
            for a in aliases:
                if a.lower() not in seen:
                    al.append(a); seen.add(a.lower())
            self._conn.execute(
                "INSERT INTO entities (type, slug, name, aliases, doc_count, status) "
                "VALUES (?,?,?,?,?,?)",
                (etype, slug, name, _json.dumps(al, ensure_ascii=False),
                 inc_count, status))
        self._invalidate_entity_index()
        if commit:
            self._conn.commit()

    def all_entities(self, types=("organismes", "personnes"),
                     limit: int | None = None) -> list[dict]:
        """Entités du registre (canonique + aliases), triées par usage décroissant.
        Source de ``known_entities()`` pour les prompts."""
        import json as _json
        ph = ",".join("?" * len(types))
        rows = self._conn.execute(
            f"SELECT type, slug, name, aliases, doc_count FROM entities "
            f"WHERE type IN ({ph}) ORDER BY doc_count DESC, name", tuple(types)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["aliases"] = _json.loads(d.get("aliases") or "[]")
            out.append(d)
        return out[:limit] if limit else out

    def _entity_index(self) -> dict:
        """Index paresseux ``slug(nom ou alias) → {type, slug, name}``.

        Le scan complet de ``entities`` (avec ``slugify`` de chaque alias) était
        refait à CHAQUE appel de ``resolve_entity`` — donc une fois par document
        dans ``classify register`` et ``organize plan``. Construit une fois,
        invalidé par les mutations du registre (``upsert_entity``,
        ``merge_entity_rows``…)."""
        if self._entity_idx is None:
            from connaissance.core.resolution import slugify
            import json as _json
            idx: dict = {}
            for r in self._conn.execute(
                    "SELECT type, slug, name, aliases FROM entities").fetchall():
                hit = {"type": r["type"], "slug": r["slug"], "name": r["name"]}
                # Le slug canonique gagne sur un alias homonyme d'une autre
                # entité (ordre : aliases d'abord, slugs ensuite écrasent).
                for a in _json.loads(r["aliases"] or "[]"):
                    idx.setdefault(slugify(a), hit)
            for r in self._conn.execute(
                    "SELECT type, slug, name FROM entities").fetchall():
                idx[r["slug"]] = {"type": r["type"], "slug": r["slug"],
                                  "name": r["name"]}
            self._entity_idx = idx
        return self._entity_idx

    def _invalidate_entity_index(self):
        self._entity_idx = None

    def resolve_entity(self, name: str):
        """Rattacher un nom brut à une entité canonique existante par **slug du
        nom OU slug d'un alias** (accents conservés). Retourne {type, slug, name}
        ou None. Sert au `register` pour détecter une variante d'entité connue."""
        if not name or not name.strip():
            return None
        from connaissance.core.resolution import slugify
        target = slugify(name)
        if not target:
            return None
        return self._entity_index().get(target)

    def merge_entity_rows(self, etype: str, from_slug: str, into_slug: str,
                          *, into_type: str | None = None,
                          commit: bool = True) -> bool:
        """Fusionner deux lignes du registre `entities` : nom + aliases du perdant
        → aliases du gardé, ``doc_count`` additionné, ligne perdante supprimée.
        Pour `entities merge`. ``into_type`` : type de l'entité gardée si la
        fusion traverse les types (personnes→organismes…) — sans lui, la ligne
        gardée était cherchée avec le type du PERDANT et la fusion inter-type
        échouait silencieusement. Retourne True si la fusion a eu lieu."""
        import json as _json
        itype = into_type or etype
        fr = self._conn.execute(
            "SELECT name, aliases, doc_count FROM entities WHERE type=? AND slug=?",
            (etype, _nfc(from_slug))).fetchone()
        to = self._conn.execute(
            "SELECT name, aliases, doc_count FROM entities WHERE type=? AND slug=?",
            (itype, _nfc(into_slug))).fetchone()
        if fr is None or to is None:
            return False
        merged = list(_json.loads(to["aliases"] or "[]"))
        seen = {a.lower() for a in merged} | {(to["name"] or "").lower()}
        for a in [fr["name"]] + list(_json.loads(fr["aliases"] or "[]")):
            if a and a.lower() not in seen:
                merged.append(a); seen.add(a.lower())
        self._conn.execute(
            "UPDATE entities SET aliases=?, doc_count=doc_count+?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime') "
            "WHERE type=? AND slug=?",
            (_json.dumps(merged, ensure_ascii=False), fr["doc_count"],
             itype, _nfc(into_slug)))
        self._conn.execute("DELETE FROM entities WHERE type=? AND slug=?",
                           (etype, _nfc(from_slug)))
        self._invalidate_entity_index()
        if commit:
            self._conn.commit()
        return True

    def relink_document(self, old_rel, new_rel, *, commit: bool = True) -> None:
        """Suivre un fichier déplacé : repointer sa fiche (signals + classement
        + sujets) de ``old_rel`` vers ``new_rel`` — la fiche survit au move.

        ``commit=False`` laisse l'écriture dans la transaction courante (pour
        grouper avec ``ledger_record`` via ``transaction()``)."""
        old_k, new_k = _nfc(old_rel), _nfc(new_rel)
        for tbl in ("doc_signals", "doc_classification", "doc_sujets"):
            self._conn.execute(f"DELETE FROM {tbl} WHERE rel_path = ?", (new_k,))
            self._conn.execute(
                f"UPDATE {tbl} SET rel_path = ? WHERE rel_path = ?",
                (new_k, old_k))
        if commit:
            self._conn.commit()

    # --- Ledger des opérations de fichiers (réversible) ---

    def ledger_record(self, entry: dict, *, commit: bool = True) -> None:
        """Enregistrer une opération de fichier appliquée (status 'applied').

        ``commit=False`` laisse l'insertion dans la transaction courante (pour
        l'atomicité ledger+fiche via ``transaction()``)."""
        self._conn.execute(
            """INSERT INTO file_ledger
               (run_id, op, old_path, new_path, sha256, size, mtime, reason, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied')""",
            (entry["run_id"], entry["op"], entry.get("old_path"),
             entry.get("new_path"), entry.get("sha256"), entry.get("size"),
             entry.get("mtime"), entry.get("reason")))
        if commit:
            self._conn.commit()

    def ledger_runs(self, limit: int = 20) -> list[dict]:
        """Lister les runs récents avec le compte d'opérations par statut."""
        rows = self._conn.execute(
            """SELECT run_id,
                      MIN(timestamp) AS started,
                      COUNT(*) AS total,
                      SUM(status = 'applied') AS applied,
                      SUM(status = 'reverted') AS reverted,
                      SUM(status = 'purged') AS purged,
                      SUM(op = 'trash') AS trashed,
                      MAX(reason) AS reason
               FROM file_ledger
               GROUP BY run_id
               ORDER BY started DESC
               LIMIT ?""", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def ledger_ops(self, run_id: str, status: str | None = None) -> list[dict]:
        """Opérations d'un run (optionnellement filtrées par statut), ordre chrono."""
        q = "SELECT * FROM file_ledger WHERE run_id = ?"
        params: list = [run_id]
        if status:
            q += " AND status = ?"
            params.append(status)
        q += " ORDER BY id"
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def ledger_all_ops(self, status: str | None = None) -> list[dict]:
        """Toutes les opérations du ledger (optionnellement par statut), ordre
        chrono — pour reconstruire l'historique (snapshots)."""
        q = "SELECT * FROM file_ledger"
        params: list = []
        if status:
            q += " WHERE status = ?"
            params.append(status)
        q += " ORDER BY id"
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def ledger_mark_reverted(self, row_id: int) -> None:
        self._conn.execute(
            "UPDATE file_ledger SET status = 'reverted' WHERE id = ?", (int(row_id),))
        self._conn.commit()

    def ledger_trash_ops(self, *, run_id: str | None = None,
                         older_than_days: int | None = None) -> list[dict]:
        """Opérations de corbeille en attente de purge (``op='trash'``,
        ``status='applied'``), filtrables par run et/ou ancienneté."""
        q = ("SELECT * FROM file_ledger "
             "WHERE op = 'trash' AND status = 'applied'")
        params: list = []
        if run_id:
            q += " AND run_id = ?"
            params.append(run_id)
        if older_than_days is not None:
            # timestamp est en heure locale ('localtime') à l'écriture.
            q += (" AND timestamp < strftime('%Y-%m-%dT%H:%M:%S', 'now', "
                  "'localtime', ?)")
            params.append(f"-{int(older_than_days)} days")
        q += " ORDER BY id"
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def ledger_mark_purged(self, row_id: int) -> None:
        self._conn.execute(
            "UPDATE file_ledger SET status = 'purged' WHERE id = ?", (int(row_id),))
        self._conn.commit()

    def purge_source_hashes(self) -> None:
        """Supprimer toutes les entrées file_type='source' (hashes de documents)."""
        self._conn.execute("DELETE FROM files WHERE file_type = 'source'")
        self._conn.commit()

    def list_all_files(self) -> list[tuple[str, str | None]]:
        """Retourner la liste `(path, file_type)` de toutes les entrées `files`."""
        rows = self._conn.execute("SELECT path, file_type FROM files").fetchall()
        return [(r[0], r[1]) for r in rows]

    def delete_files(self, paths: list[str]) -> None:
        """Supprimer les entrées `files` pour les chemins donnés (bulk)."""
        if not paths:
            return
        self._conn.executemany(
            "DELETE FROM files WHERE path = ?",
            [(p,) for p in paths],
        )
        self._conn.commit()

    def scan_and_register_stats(self, directory, extensions=None, min_size=1024):
        """Scanner un dossier et enregistrer ``(path, size, mtime)`` sans hasher.

        Mode JIT : ne lit jamais le contenu. Les hashes seront calculés à la
        demande par ``get_or_compute_hash`` quand le pipeline en aura besoin
        (collision de taille, promotion PJ, vérification source_changed).

        Returns ``(updated, unchanged, total)`` counts.
        """
        if extensions is None:
            extensions = {".pdf", ".png", ".jpg", ".jpeg", ".heic", ".webp",
                          ".tiff", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}

        directory = Path(directory)
        if not directory.exists():
            return 0, 0, 0

        updated = 0
        unchanged = 0
        total = 0
        for f in sorted(directory.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in extensions:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            size = st.st_size
            if size < min_size:
                continue

            total += 1
            row = self._conn.execute(
                "SELECT size, mtime FROM files WHERE path = ?", (str(f),)
            ).fetchone()
            if row is not None:
                d = dict(row)
                if (d.get("size") == int(size)
                        and d.get("mtime") == float(st.st_mtime)):
                    unchanged += 1
                    continue
            self.upsert_stat(f, size, st.st_mtime, file_type="source")
            updated += 1

        return updated, unchanged, total

    def missing_resumes(self, source_type=None, since=None, until=None):
        """Trouver les transcriptions sans résumé correspondant.

        ``since``/``until`` filtrent sur ``f.created`` (date métier du
        frontmatter) au format ``YYYY-MM-DD`` ; intervalle inclusif à
        gauche, exclusif à droite. Les transcriptions dont ``created``
        est NULL sont exclues quand un filtre date est actif — on préfère
        les rater que les compter à tort dans un budget temporel.
        """
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
        if since:
            query += " AND f.created IS NOT NULL AND f.created >= ?"
            params.append(since)
        if until:
            query += " AND f.created IS NOT NULL AND f.created < ?"
            params.append(until)
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

    # --- LLM usage (coûts réels) ---

    def log_usage(self, operation: str, usage: dict,
                  source_type: str | None = None,
                  source_path: str | None = None,
                  dest_path: str | None = None,
                  custom_id: str | None = None,
                  model: str | None = None,
                  mode: str | None = None,
                  batch: bool = False) -> None:
        """Enregistrer un usage LLM (tokens + coût) après un appel API.

        Utilisé par ``classify.register``, ``summarize.register_from_results_file``
        et ``synthesis.register_from_results_file`` pour tracer les coûts réels
        par opération et source. ``usage`` suit le format Anthropic
        (``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens``,
        ``cache_read_input_tokens``). ``batch=True`` applique la remise Batch
        API −50 % sur le coût. Les appels manuels (mode « dans Claude ») n'ont
        pas de usage API et ne sont pas tracés.
        """
        if not usage:
            return
        cost = compute_cost_usd(model, usage, batch=batch)
        self._conn.execute(
            """INSERT INTO llm_usage
               (operation, source_type, source_path, dest_path, custom_id,
                model, mode, input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (operation, source_type,
             str(source_path) if source_path else None,
             str(dest_path) if dest_path else None,
             custom_id, model, mode,
             usage.get("input_tokens") or 0,
             usage.get("output_tokens") or 0,
             usage.get("cache_creation_input_tokens") or 0,
             usage.get("cache_read_input_tokens") or 0,
             cost))
        self._conn.commit()
        _append_jsonl(USAGE_JOURNAL, {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "operation": operation, "source_type": source_type,
            "source_path": str(source_path) if source_path else None,
            "dest_path": str(dest_path) if dest_path else None,
            "custom_id": custom_id, "model": model, "mode": mode,
            "input_tokens": usage.get("input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens") or 0,
            "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
            "units": None, "cost_usd": cost})

    def log_ocr_usage(self, operation: str, pages: int,
                      source_path: str | None = None,
                      dest_path: str | None = None,
                      model: str | None = None,
                      mode: str = "batch") -> None:
        """Journaliser le coût d'un OCR Mistral (facturé à la **page**).

        Écrit une ligne ``llm_usage`` avec ``units=pages`` et les compteurs de
        tokens à 0 (l'OCR ne consomme pas de tokens Claude). Le coût suit
        ``MISTRAL_PAGE_COST_USD`` ($1/1000 p, déjà tarif batch). Sert à ce que
        ``pipeline costs --real`` reflète le coût OCR de bout en bout, pas
        seulement les résumés/classements Claude.
        """
        pages = int(pages or 0)
        if pages <= 0:
            return
        cost = round(pages * MISTRAL_PAGE_COST_USD, 6)
        self._conn.execute(
            """INSERT INTO llm_usage
               (operation, source_path, dest_path, model, mode,
                input_tokens, output_tokens, cache_creation_input_tokens,
                cache_read_input_tokens, units, cost_usd)
               VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?)""",
            (operation,
             str(source_path) if source_path else None,
             str(dest_path) if dest_path else None,
             model, mode, pages, cost))
        self._conn.commit()
        _append_jsonl(USAGE_JOURNAL, {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "operation": operation, "source_type": None,
            "source_path": str(source_path) if source_path else None,
            "dest_path": str(dest_path) if dest_path else None,
            "custom_id": None, "model": model, "mode": mode,
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "units": pages, "cost_usd": cost})

    # --- Restauration depuis les journaux disque (DB reconstructible) ---

    def import_ledger_journal(self, *, force: bool = False) -> dict:
        """Réimporter ``file_ledger`` depuis les JSONL disque (DB perdue/rebuild).

        Sans ``force`` : ajoute uniquement les ``run_id`` absents de la table
        (idempotent). Avec ``force`` : vide ``file_ledger`` puis réimporte tout.
        **Garde-fou** : ``force`` REFUSE de vider si aucun JSONL n'est présent
        sur disque (ne jamais remplacer des données par du vide)."""
        files = (sorted(LEDGER_JOURNAL_DIR.glob("*.jsonl"))
                 if LEDGER_JOURNAL_DIR.exists() else [])
        if force:
            current = self._conn.execute(
                "SELECT COUNT(*) FROM file_ledger").fetchone()[0]
            if not files and current:
                return {"runs_imported": 0, "rows": 0,
                        "refused": "aucun JSONL ledger sur disque — refus de "
                                   "vider une table non vide"}
            self._conn.execute("DELETE FROM file_ledger")
        existing = {r[0] for r in self._conn.execute(
            "SELECT DISTINCT run_id FROM file_ledger")}
        runs = rows = 0
        if files:
            for jf in files:
                if jf.stem in existing:
                    continue
                runs += 1
                for line in jf.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._conn.execute(
                        """INSERT INTO file_ledger
                           (run_id, op, old_path, new_path, sha256, size, mtime,
                            reason, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (e.get("run_id"), e.get("op"), e.get("old_path"),
                         e.get("new_path"), e.get("sha256"), e.get("size"),
                         e.get("mtime"), e.get("reason"),
                         e.get("status") or "applied"))
                    rows += 1
        self._conn.commit()
        return {"runs_imported": runs, "rows": rows}

    def import_usage_journal(self, *, force: bool = False) -> dict:
        """Réimporter ``llm_usage`` depuis le JSONL disque (DB perdue/rebuild).

        Sans ``force`` : n'importe que si la table est vide (évite les doublons,
        les lignes d'usage n'ayant pas de clé naturelle). Avec ``force`` : vide
        puis réimporte tout. **Garde-fou** : ``force`` REFUSE de vider si le
        JSONL est absent/vide (ne jamais remplacer des données par du vide)."""
        has_journal = USAGE_JOURNAL.exists() and USAGE_JOURNAL.stat().st_size > 0
        if force:
            current = self._conn.execute(
                "SELECT COUNT(*) FROM llm_usage").fetchone()[0]
            if not has_journal and current:
                return {"rows": 0,
                        "refused": "aucun JSONL usage sur disque — refus de "
                                   "vider une table non vide"}
            self._conn.execute("DELETE FROM llm_usage")
        n = self._conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
        if n > 0:
            return {"rows": 0, "skipped": "table non vide (utiliser --force)"}
        rows = 0
        if USAGE_JOURNAL.exists():
            for line in USAGE_JOURNAL.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._conn.execute(
                    """INSERT INTO llm_usage
                       (timestamp, operation, source_type, source_path, dest_path,
                        custom_id, model, mode, input_tokens, output_tokens,
                        cache_creation_input_tokens, cache_read_input_tokens,
                        units, cost_usd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (e.get("ts") or e.get("timestamp"), e.get("operation"),
                     e.get("source_type"), e.get("source_path"), e.get("dest_path"),
                     e.get("custom_id"), e.get("model"), e.get("mode"),
                     e.get("input_tokens") or 0, e.get("output_tokens") or 0,
                     e.get("cache_creation_input_tokens") or 0,
                     e.get("cache_read_input_tokens") or 0,
                     e.get("units"), e.get("cost_usd")))
                rows += 1
        self._conn.commit()
        return {"rows": rows}

    def usage_summary(self, since: str | None = None,
                      until: str | None = None,
                      operation: str | None = None) -> dict:
        """Agrégats de coûts réels LLM par opération et source_type.

        ``since``/``until`` : bornes YYYY-MM-DD sur ``timestamp`` (inclusif à
        gauche, exclusif à droite). ``operation`` : filtre sur le nom.
        Retourne ``{total: {...}, par_operation: {...}, par_source_type: {...},
        cache_hit_rate}``.
        """
        where = ["1=1"]
        params: list = []
        if since:
            where.append("timestamp >= ?")
            params.append(since)
        if until:
            where.append("timestamp < ?")
            params.append(until)
        if operation:
            where.append("operation = ?")
            params.append(operation)
        w = " AND ".join(where)

        def _agg(group_col: str | None) -> list[dict]:
            cols = f"{group_col}, " if group_col else ""
            row_group = group_col or "'all'"
            query = f"""SELECT {cols}
                    COUNT(*) as n,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
                    COALESCE(SUM(cache_creation_input_tokens), 0) as cache_write,
                    COALESCE(SUM(cache_read_input_tokens), 0) as cache_read,
                    COALESCE(SUM(units), 0) as units,
                    COALESCE(SUM(cost_usd), 0.0) as cost_usd
                FROM llm_usage WHERE {w}"""
            if group_col:
                query += f" GROUP BY {group_col} ORDER BY cost_usd DESC"
            else:
                query += f" GROUP BY {row_group}"
            return [dict(r) for r in self._conn.execute(query, params).fetchall()]

        total_rows = _agg(None)
        total = total_rows[0] if total_rows else {
            "n": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_write": 0, "cache_read": 0, "units": 0, "cost_usd": 0.0,
        }
        # Cache hit rate = read / (read + write + uncached). Le input_tokens
        # stocké exclut déjà les tokens cachés (semantics de l'usage Anthropic).
        total_input_with_cache = (
            (total.get("input_tokens") or 0)
            + (total.get("cache_write") or 0)
            + (total.get("cache_read") or 0)
        )
        hit_rate = (
            (total.get("cache_read") or 0) / total_input_with_cache
            if total_input_with_cache else 0.0
        )
        return {
            "total": {**total, "cache_hit_rate": round(hit_rate, 4)},
            "par_operation": _agg("operation"),
            "par_source_type": _agg("source_type"),
            "par_model": _agg("model"),
        }

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
