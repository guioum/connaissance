"""TypedDict des sorties JSON du CLI.

Toute fonction de `connaissance.commands.*` retourne un dict conforme à
l'un de ces TypedDict. Les outils MCP `kb_*` exposent ces mêmes structures.

Contrat VÉRIFIÉ : les fonctions publiques des modules `commands/*` sont
annotées avec ces types et pyright (mode basic, `pyrightconfig.json`)
vérifie la conformité des dicts retournés. Les types restent volontairement
permissifs (NotRequired) sur les champs qui dépendent des flags passés.

Convention `--output-file` : les commandes qui passent par
``core.output_file.write_or_inline`` retournent SOIT le payload complet,
SOIT un récap compact (clés ``output_file``/``total_bytes`` + résumé).
Les deux variantes vivent dans le MÊME TypedDict (total=False).
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from connaissance.core.secrets import SecretFinding

# Réexport délibéré : SecretFinding est défini dans core.secrets (source
# unique de la détection) mais fait partie du contrat de sortie
# (SecretFile.findings). L'ancienne définition dupliquée ici a été retirée.
_REEXPORTS = (SecretFinding,)

# --- Primitives partagées ---

Source = Literal["document", "courriel", "note"]
EntityType = Literal["personnes", "organismes", "divers", "inconnus"]
FileType = Literal["transcription", "resume", "fiche", "chronologie", "moc", "digest", "source"]
ManifestStatus = Literal["auto", "alias_match", "a_confirmer"]
Confidence = Literal["high", "low"]


class ErrorEnvelope(TypedDict):
    """Enveloppe d'erreur (stderr du CLI + wrappers MCP)."""
    error: dict  # {type: str, message: str}


# --- pipeline ---

class ResumesManquants(TypedDict):
    total: int
    par_source: dict[str, int]
    fichiers: list[str]


class ResumesPerimes(TypedDict):
    total: int
    fichiers: list[dict]  # [{resume, transcription}]
    mtime_only_ignores: NotRequired[int]  # candidats mtime écartés par le hash de contenu


class NonOrganises(TypedDict):
    total: int
    fichiers: list[str]


class StaleEntity(TypedDict):
    entity_type: str
    entity_slug: str
    latest_resume: float
    synthesis_updated: NotRequired[float | None]


class SynthesePerimee(TypedDict):
    total: int
    entites: list[StaleEntity]


class StaleMoc(TypedDict):
    category: str
    status: Literal["manquant", "périmé"]
    nouveaux_resumes: NotRequired[int]
    seuil: NotRequired[int]        # présent seulement pour status "périmé"


class MocPerimes(TypedDict):
    total: int
    categories: list[StaleMoc]
    seuil: NotRequired[int]        # absent quand Résumés/ n'existe pas


class Couts(TypedDict):
    mode: str                      # "batch" | "interactif" (non contraint par le CLI)
    resumes: dict
    synthese: dict
    moc: dict
    total: float                   # barème statique (borne haute)
    empirique: NotRequired[dict]   # projection aux coûts unitaires observés (llm_usage)
    date_range: NotRequired[dict]  # {since, until} si une plage est passée


class CoutsReels(TypedDict):
    """Sortie de `pipeline costs --real` (agrégats du journal llm_usage)."""
    total: dict
    par_operation: list[dict]
    par_source_type: list[dict]
    par_model: list[dict]
    source: str                    # "llm_usage"
    date_range: NotRequired[dict]


class Stats(TypedDict):
    transcription: int
    resume: int
    fiche: int
    chronologie: int
    moc: int
    digest: int
    operations: int


class PipelineDetection(TypedDict, total=False):
    resumes_manquants: ResumesManquants
    resumes_perimes: ResumesPerimes
    non_organises: NonOrganises
    synthese_perimee: SynthesePerimee
    moc_perimes: MocPerimes
    couts: Couts
    stats: Stats
    date_range: dict               # {since, until} si une plage est passée


# --- documents ---

class DocumentToTranscribe(TypedDict, total=False):
    source: str          # chemin canonique (identité : frontmatter, DB, register)
    read_source: str     # chemin physique à OCR/lire — miroir SSD si dispo, sinon = source
    transcription: str   # chemin miroir de sortie sous Transcriptions/Documents/
    rel: str
    size: int
    hash: str | None     # présent seulement si un hash a été calculé (JIT)
    reason: str          # ex. "source_changed"


class DocumentsScan(TypedDict, total=False):
    to_transcribe: list[DocumentToTranscribe]
    registered_existing: list[str]
    skipped: list[dict]  # [{reason, count}]
    # variante --output-file
    output_file: str
    total_bytes: int
    total_to_transcribe: int
    total_skipped: int
    by_year: dict
    sample_to_transcribe: list


class DocumentSuspect(TypedDict):
    path: str
    rel: str
    score: int
    reasons: list[str]
    tables_count: int


class DocumentsSuspects(TypedDict):
    count: int
    suspects: list[DocumentSuspect]


class DocumentSignals(TypedDict, total=False):
    rel: str
    type: str
    origin_folder: str | None
    type_hint: str | None
    name_keywords: list[str]
    dates: dict          # {from_name, filesystem_created, filesystem_modified, metadata}
    title_meta: str | None
    author_meta: str | None
    born_digital: bool | None
    text_source: str     # none|ocr_cache|plain|office|pdf_embedded
    pdf_available: bool
    summary: dict        # {keywords, sentences, entities, chars}


class DocumentsSignals(TypedDict, total=False):
    total: int
    scanned: int
    documents: list[DocumentSignals]
    skipped: dict
    pdf_text_layer: bool
    note: str
    # variante --output-file
    output_file: str
    total_bytes: int
    by_text_source: dict
    by_born_digital: dict
    by_type: dict
    top_origin_folders: dict
    sample: list[str]


class VerifyPreserve(TypedDict):
    ok: bool
    missing_tokens: list[str]
    added_tokens: list[str]
    total_tokens_old: int
    total_tokens_new: int


class RegisterBatch(TypedDict):
    registered: int
    missing: list[dict]   # [{source, transcription, rel}] — transcription absente du disque
    content_dupes_propagated: int
    content_dupes_missing: list[dict]  # [{rel, same_as}]
    total: int
    dry_run: bool


class CategoryView(TypedDict, total=False):
    categories: dict       # {categorie: nombre}, triées par fréquence
    total: int
    no_category: int       # résumés sans champ category
    missing_source: int    # résumés dont le fichier source est introuvable
    applied: bool
    links_created: int
    cleared: bool          # présent en mode --clear
    existed: bool
    view_dir: str


class LedgerRun(TypedDict):
    run_id: str
    started: str
    total: int
    applied: int
    reverted: int
    purged: int
    trashed: int
    reason: str | None


class LedgerRuns(TypedDict):
    runs: list[LedgerRun]


class LedgerShow(TypedDict):
    run_id: str
    operations: list[dict]  # lignes file_ledger


class LedgerRevert(TypedDict):
    run_id: str
    dry_run: bool
    reverted: int
    skipped: list[dict]     # [{path, reason}]


class LedgerVerify(TypedDict):
    run_id: str
    checked: int
    ok: int
    issues: list[dict]      # [{path, reason}]


class LedgerPurge(TypedDict):
    dry_run: bool
    purged: int             # fichiers détruits définitivement (corbeille)
    freed_bytes: int
    skipped: list[dict]     # [{path, reason}]


class LedgerSnapshot(TypedDict, total=False):
    days: int               # nb de jours représentés (un dossier par jour)
    entries: int            # anciens chemins d'origine reconstruits
    linked: int             # symlinks créés (apply)
    would_link: int         # symlinks prévus (dry-run)
    gone: int               # fichiers disparus (corbeille purgée) → marqueur
    applied: bool
    view_dir: str
    cleared: bool
    existed: bool


# --- sujets (vue virtuelle) ---

class SujetView(TypedDict, total=False):
    sujets: dict            # {sujet: count}
    total: int
    missing_source: int
    applied: bool
    links_created: int
    view_dir: str
    par_annee: dict         # {sujet: {année: count}} si ventilation demandée
    cleared: bool           # présent en mode --clear
    existed: bool


class SujetExport(TypedDict):
    sujet: str
    exported: int
    missing_source: int
    dest: str
    zip: bool


class SujetList(TypedDict):
    sujets: dict            # {sujet: count}
    total_sujets: int
    total_documents: int


# --- duplicates (Phase D) ---

class Duplicates(TypedDict):
    scanned: int
    unreadable: int
    exact_clusters: list[dict]   # [{hash, rels}]
    quasi_clusters: list[dict]   # [{rels}]
    exact_duplicates: int
    quasi_duplicates: int
    threshold: int


class DuplicatesPlan(TypedDict, total=False):
    total: int
    exact: int
    quasi: int
    scanned: int
    manifest_file: str
    entries: list[dict]          # [{trash, keeper, kind, hash}]
    # variante --output-file
    output_file: str
    total_bytes: int
    sample: list[dict]


class EntitiesCandidates(TypedDict):
    total_entities: int
    candidates: list[dict]       # [{type, a, b, score, reasons}]
    count: int


# Clé JSON réelle "from" (mot réservé) → syntaxe fonctionnelle obligatoire.
EntitiesMerge = TypedDict("EntitiesMerge", {
    "dry_run": bool,
    "from": str,
    "into": str,
    # dry-run
    "docs_to_reassign": int,
    "documents_to_move": int,
    "aliases_to_add": list[str],
    "from_fiche_exists": bool,
    # apply
    "reassigned": int,
    "resumes_moved": int,
    "documents_moved": int,
    "aliases_added": list[str],
    "from_fiche_trashed": bool,
    "ledger_run": str,
    "error": str,
}, total=False)


EntitiesRename = TypedDict("EntitiesRename", {
    "dry_run": bool,
    "from": str,
    "new_slug": str,
    # dry-run
    "documents": int,
    "sujet_refs": int,
    # apply
    "documents_relocated": int,
    "files_moved": int,
    "db": dict,
    "fiche_updated": bool,
    "ledger_run": str,
    "error": str,
}, total=False)


class MediaPlan(TypedDict, total=False):
    total: int
    by_year: dict
    manifest_file: str
    entries: list[dict]          # [{source, dest}]
    # variante --output-file
    output_file: str
    total_bytes: int
    sample: list[dict]


class MediaApply(TypedDict, total=False):
    dry_run: bool
    planned: int
    moved: int
    would_move: int
    skipped: list[dict]
    errors: list[dict]
    moves: list[dict]
    ledger_run: str


class DuplicatesApply(TypedDict, total=False):
    dry_run: bool
    planned: int
    trashed: int
    would_trash: int
    sujets_captured: int     # appartenances multi-sujet capturées des copies
    skipped: list[dict]
    errors: list[dict]
    moves: list[dict]
    ledger_run: str


class Triage(TypedDict, total=False):
    total_files: int        # vrac + groupés + conteneurs
    loose_files: int        # fichiers en vrac, à classer
    grouped_files: int      # fichiers dans les dossiers thématiques groupés
    # payload : listes détaillées ; récap --output-file : compteurs (int).
    grouped_folders: list | int   # [{path, theme, docs, files}] — collections à garder unies
    grouped_candidates: list | int  # [{path, docs, files}] — collections cohérentes SANS thème
    groups: dict            # décompte EN VRAC : {A_documents, B_exports, C_media, D_code, autre}
    containers: dict        # {files_total, repos_code, bundles, archives}.
    # archives : [{path, files, docs_extracted, archived}] — une archive met de
    # côté son résidu (archived) mais ses documents sont extraits vers le groupe A.
    by_extension: dict
    documents_sample: list[str]
    # variante --output-file
    output_file: str
    total_bytes: int
    container_files: int
    repos_code: int
    bundles: int
    archives: int


class SecretFile(TypedDict):
    rel: str
    severity: Literal["high", "medium"]
    filename_signal: str | None
    findings: list[SecretFinding]
    findings_count: int


class SecretsScan(TypedDict, total=False):
    flagged: int
    files: list[SecretFile]
    scanned: int
    containers_skipped: int   # repos/bundles non parcourus (comme le triage)
    skipped: dict        # {dataless, too_big, binary, read_error}
    note: str
    # variante --output-file
    output_file: str
    total_bytes: int
    high_severity: int
    sample: list[str]


class SecretsQuarantine(TypedDict, total=False):
    quarantine_file: str
    added: int
    already_present: int
    total_quarantined: int
    high: int
    medium: int
    added_sample: list[str]
    note: str


class SecretsRelocate(TypedDict, total=False):
    dry_run: bool
    candidates: int
    moved: int
    would_move: int
    skipped: list[dict]
    dest_root: str
    sample: list[dict]           # [{from, to}]
    ledger_run: str


class ClassifyPrepare(TypedDict, total=False):
    total: int
    model: str
    transit_file: str
    known_entities_count: int
    requests: list[dict]   # [{custom_id, system, user, model, max_tokens}]
    # variante --output-file
    output_file: str
    total_bytes: int
    sample_prompts: list[dict]


class ClassifyEntry(TypedDict, total=False):
    custom_id: str
    source: str
    status: Literal["auto", "attente"]
    dest: str | None
    entity: str | None
    entity_type: str
    entity_slug: str
    category: str | None
    date: str | None
    date_approx: bool
    title: str
    sujet: str | None
    confidence: Literal["high", "low"]
    reasons: list[str]


class ClassifyRegister(TypedDict, total=False):
    total: int
    auto: int
    attente: int
    manifest_file: str
    entries: list[ClassifyEntry]
    # variante --output-file
    output_file: str
    total_bytes: int
    by_entity_type: dict
    by_category: dict
    attente_reasons: dict
    auto_low_confidence: int
    sample_auto: list[dict]


class ClassifyStatus(TypedDict, total=False):
    # résumé corpus (sans --path)
    total: int
    by_status: dict
    by_category: dict
    by_entity_type: dict
    by_entity: dict
    # fiche d'un document (avec --path)
    rel: str
    found: bool
    quarantined: bool
    signals: dict | None
    classification: dict | None


class ClassifyApply(TypedDict, total=False):
    dry_run: bool
    auto_total: int
    moved: int
    planned: int
    attente: int
    reconciled: int   # relinks réparés par la réconciliation post-crash
    skipped: list[dict]
    errors: list[dict]
    moves: list[dict]
    db_snapshot: str  # snapshot DB pris avant un apply réel
    ledger_run: str   # présent si des fichiers ont bougé (pour `ledger revert`)
    ledger_report: str  # vue Markdown du run, à côté du JSONL


# --- emails ---

class EmailsStats(TypedDict):
    folders: list[dict]  # [{name, count, size}]
    totals: dict          # {count, size}


class EmailsExtract(TypedDict):
    extracted: int
    dedup_skipped: int
    filtered: list[dict]  # [{reason, count}]
    written: list[str]
    dry_run: bool


class EmailThread(TypedDict):
    message_ids: list[str]
    paths: list[str]
    latest_date: str | None


class EmailsThreads(TypedDict):
    threads: list[EmailThread]
    orphans: list[dict]
    filtered_below_score: list[dict]


class ScoringMutation(TypedDict, total=False):
    add_domain_marketing: list[str]
    remove_domain_marketing: list[str]
    add_domain_personnel: list[str]
    add_pattern_actionnable: list[str]
    add_pattern_promotionnel: list[str]
    add_pattern_marketing: list[str]
    set_weight: dict[str, int]
    set_seuil: dict[str, int]


class EmailsCalibrate(TypedDict):
    sample: int
    seuils: dict
    repartition: dict
    candidats: dict  # {whitelist, blacklist, revue}
    proposed_mutations: ScoringMutation
    rapport_path: str


class EmailsCleanupObsolete(TypedDict):
    would_archive: list[dict]
    archived_to: str
    manifest_path: str
    ledger_run: str          # handle ledger pour `ledger revert` ("" en dry-run)
    total_scanned: int
    dry_run: bool


# --- notes ---

class NotesCopy(TypedDict):
    copied: int
    skipped: int
    errors: list[str]
    attachments_copied: NotRequired[int]
    dry_run: NotRequired[bool]


# --- organize ---

class OrganizeEntry(TypedDict, total=False):
    id: str
    source: str  # documents|courriels|notes
    resume_path: str
    entity_type: str  # EntityType attendu, mais lu du frontmatter (non garanti)
    entity_slug: str
    entity_name: str
    new_name: str
    confidence: Confidence
    status: ManifestStatus
    qmd_candidates: list[dict]  # injecté par organize enrich


class OrganizePlan(TypedDict):
    total: int
    auto: int
    alias_match: int
    a_confirmer: int
    manifest_path: str
    entries: list[OrganizeEntry]


class OrganizeApply(TypedDict, total=False):
    moved: int
    skipped: int
    errors: int
    sync_warnings: list[dict]   # échecs post-move (frontmatter source: / DB) non fatals
    manifest: str
    dry_run: bool
    ledger_run: str   # run_id du ledger (présent si des fichiers ont bougé) — pour `ledger revert`


class OrganizeResolve(TypedDict):
    slug: str
    filename: str
    alias_match: NotRequired[str | None]


# --- optimize ---

class OptimizePlan(TypedDict):
    promotable: list[dict]
    duplicates: list[dict]
    orphan_attachments: list[dict]


class OptimizeApply(TypedDict):
    promoted: int
    deduped: int
    freed_bytes: int
    orphans_removed: int
    empty_dirs_removed: int
    dry_run: bool
    ledger_run: NotRequired[str]
    trashed_recoverable: NotRequired[int]


# --- summarize ---

class SummarizeRequest(TypedDict):
    custom_id: str
    system: str
    user: str
    model: str
    max_tokens: int
    source_type: str
    source_path: str
    model_tier: str
    model_reason: str


class SummarizePrepare(TypedDict, total=False):
    requests: list[SummarizeRequest]
    total: int
    user_excluded: int
    estimated_input_tokens: int
    mode: str
    preference: str
    # variante --output-file
    output_file: str
    total_bytes: int
    source_types: dict
    model_tiers: dict


class SummarizeRegister(TypedDict):
    path: str
    file_type: FileType
    source_type: NotRequired[str]
    frontmatter_injected: bool
    error: NotRequired[str]


class SummarizePlan(TypedDict):
    missing: list[dict]  # [{id, path, file_type}]


# --- synthesis ---

class AliasCandidate(TypedDict):
    alias: str
    support_resumes: int
    kind: str  # "name" | "from" | "domain"


class AliasesCandidates(TypedDict):
    entity: str
    existing_aliases: list[str]
    candidates: list[AliasCandidate]


class RelationCandidate(TypedDict):
    other: str  # "type/slug"
    title: str  # nom affichable de l'entité cible (fiche cible, sinon dé-slugifié)
    link: str  # cible de lien markdown bundle-relative : "/Synthèse/{other}/fiche.md"
    co_mentions: int
    support_resumes: list[str]


class RelationsCandidates(TypedDict):
    entity: str
    candidates: list[RelationCandidate]


class SynthesisPlan(TypedDict):
    stale_entities: list[StaleEntity]
    stale_mocs: list[StaleMoc]


class SynthesisRegister(TypedDict, total=False):
    registered: int
    file_type: str      # mode hérité : fiche|chronologie|moc|digest|synthese|resume
    kind: str           # mode moderne : fiche|chronologie|moc|digest|index
    entity: str | None
    path: str
    abs_path: str
    bytes: int
    error: str


# --- audit ---

class AuditCheck(TypedDict):
    name: str
    status: Literal["ok", "issues"]
    issues: list[dict]


class AuditResult(TypedDict):
    checks: list[AuditCheck]
    status: Literal["ok", "issues"]
    total_issues: int


class AuditReindex(TypedDict):
    rescanned: int
    reinserted: int
    details: dict   # {transcriptions, resumes, synthese, orphans, hashes}
    dry_run: bool


class AuditRepairAttachments(TypedDict):
    scanned: int
    repaired: int
    missing: int
    already_ok: int


class AuditArchiveNonDocuments(TypedDict):
    archived: int
    list: list  # scope : [{source, dest}] ; manifeste : [{famille, files, bytes, units, ledger_run}]
    dry_run: bool
    errors: NotRequired[list]
    ledger_run: NotRequired[str]
    error: NotRequired[str]
    trashed: NotRequired[int]          # --from-manifest : fichiers envoyés en corbeille ledger
    archives_root: NotRequired[str]    # --from-manifest
    index: NotRequired[str]            # --from-manifest : chemin de _index.md


# --- scope ---

class ScopeScan(TypedDict):
    root: str
    total_dirs_scanned: int
    already_decided: int
    to_present: int
    by_category: dict   # {categorie: {count, total_files, items}}
    summary: dict       # {categorie: nombre de dossiers}
    report_path: str


class ScopeMutate(TypedDict):
    added: list[str]
    filtres_yaml_mutated: bool


# --- config (scoring) ---

class ScoringDiffChange(TypedDict):
    key: str
    op: Literal["add", "remove", "set"]
    before: object
    after: object


class ScoringDiff(TypedDict):
    changes: list[ScoringDiffChange]


class ScoringSet(TypedDict):
    diff: list[ScoringDiffChange]
    written: bool
    regex_errors: list[str]
    post_validation_ok: bool
    dry_run: NotRequired[bool]
    error: NotRequired[str]


class ScoringValidate(TypedDict):
    ok: bool
    errors: list[str]


# --- manifest ---

class ManifestPatchItem(TypedDict, total=False):
    id: str
    set: dict
    delete: bool
    filter: dict   # patchs en masse : prédicats appliqués
    count: int     # suppression en masse : nb d'entrées retirées


class ManifestPatchNotFound(TypedDict, total=False):
    target: str
    patch: dict
    reason: str


class ManifestPatchResult(TypedDict, total=False):
    manifest_path: str
    patches: list[ManifestPatchItem]
    updated: int
    not_found: list[ManifestPatchNotFound]
    error: str
