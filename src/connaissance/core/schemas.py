"""TypedDict des sorties JSON du CLI.

Toute fonction de `connaissance.commands.*` retourne un dict conforme à
l'un de ces TypedDict. Les outils MCP `kb_*` exposent ces mêmes structures.

Les types sont volontairement permissifs (NotRequired) sur les champs qui
dépendent des flags passés — on teste la forme à l'appel, pas au typage.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


# --- Primitives partagées ---

Source = Literal["document", "courriel", "note"]
EntityType = Literal["personnes", "organismes", "divers", "inconnus"]
FileType = Literal["transcription", "resume", "fiche", "chronologie", "moc", "digest", "source"]
ManifestStatus = Literal["auto", "alias_match", "a_confirmer"]
Confidence = Literal["high", "low"]


class ErrorEnvelope(TypedDict):
    """Enveloppe d'erreur retournée par les wrappers MCP."""
    error: dict  # {type: str, message: str}


# --- pipeline ---

class ResumesManquants(TypedDict):
    total: int
    par_source: dict[str, int]
    fichiers: list[str]


class ResumesPerimes(TypedDict):
    total: int
    fichiers: list[dict]  # [{resume, transcription}]


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


class MocPerimes(TypedDict):
    total: int
    categories: list[StaleMoc]


class Couts(TypedDict):
    mode: Literal["batch", "interactif"]
    resumes: dict
    synthese: dict
    moc: dict
    total: float


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


# --- documents ---

class DocumentToTranscribe(TypedDict):
    path: str
    rel: str
    size: int
    mtime: float


class DocumentsScan(TypedDict):
    to_transcribe: list[DocumentToTranscribe]
    registered_existing: list[str]
    skipped: list[dict]  # [{path, reason}]


class DocumentSuspect(TypedDict):
    path: str
    rel: str
    score: int
    reasons: list[str]
    tables_count: int


class DocumentsSuspects(TypedDict):
    count: int
    suspects: list[DocumentSuspect]


class VerifyPreserve(TypedDict):
    ok: bool
    missing_tokens: list[str]
    added_tokens: list[str]


# --- emails ---

class EmailsStats(TypedDict):
    folders: list[dict]  # [{name, count, size}]
    totals: dict          # {count, size}


class EmailsExtract(TypedDict):
    extracted: int
    dedup_skipped: int
    filtered: list[dict]  # [{reason, count}]
    written: list[str]


class EmailThread(TypedDict):
    message_ids: list[str]
    paths: list[str]
    latest_date: str


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
    set_weight: dict[str, int]
    set_seuil: dict[str, int]


class EmailsCalibrate(TypedDict):
    sample: int
    seuils: dict
    repartition: dict
    candidats: dict  # {whitelist, blacklist, revue}
    proposed_mutations: ScoringMutation


class EmailsCleanupObsolete(TypedDict):
    would_archive: list[dict]
    archived_to: str
    manifest_path: str


# --- notes ---

class NotesCopy(TypedDict):
    copied: int
    skipped: int
    errors: list[str]


# --- organize ---

class OrganizeEntry(TypedDict, total=False):
    id: str
    source: str  # documents|courriels|notes
    resume_path: str
    entity_type: EntityType
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


class OrganizeApply(TypedDict):
    moved: int
    skipped: int
    errors: list[str]


class OrganizeResolve(TypedDict):
    slug: str
    filename: str
    alias_match: NotRequired[str | None]


# --- optimize ---

class OptimizePlan(TypedDict):
    promotable: list[dict]
    duplicates: list[dict]


class OptimizeApply(TypedDict):
    promoted: int
    deduped: int
    freed_bytes: int


# --- summarize ---

class SummarizeRequest(TypedDict):
    custom_id: str
    system: str
    user: str
    model: str
    max_tokens: int


class SummarizePrepare(TypedDict):
    requests: list[SummarizeRequest]
    total: int
    estimated_input_tokens: int


class SummarizeRegister(TypedDict):
    path: str
    file_type: FileType
    entity_type: NotRequired[str | None]
    entity_slug: NotRequired[str | None]
    frontmatter_injected: bool


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
    co_mentions: int
    support_resumes: list[str]


class RelationsCandidates(TypedDict):
    entity: str
    candidates: list[RelationCandidate]


class SynthesisPlan(TypedDict):
    stale_entities: list[StaleEntity]
    stale_mocs: list[StaleMoc]


class SynthesisRegister(TypedDict):
    registered: int
    file_type: FileType


# --- audit ---

class AuditCheck(TypedDict):
    name: str
    status: Literal["ok", "issues"]
    issues: list[dict]


class AuditResult(TypedDict):
    checks: list[AuditCheck]
    status: Literal["ok", "issues"]


class AuditReindex(TypedDict):
    rescanned: int
    reinserted: int


class AuditRepairAttachments(TypedDict):
    broken_refs: int
    fixed: int
    still_broken: list[str]


class AuditArchiveNonDocuments(TypedDict):
    archived: int
    list: list[str]


# --- scope ---

class ScopeFolder(TypedDict):
    name: str
    file_count: int
    size: int
    status: Literal["included", "excluded", "unknown"]


class ScopeScan(TypedDict):
    root: str
    folders: list[ScopeFolder]


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


class ScoringValidate(TypedDict):
    ok: bool
    errors: list[str]


# --- manifest ---

class ManifestPatchItem(TypedDict, total=False):
    id: str
    set: dict
    delete: bool


class ManifestPatchNotFound(TypedDict, total=False):
    target: str
    patch: dict
    reason: str


class ManifestPatchResult(TypedDict, total=False):
    manifest_path: str
    patches: list[ManifestPatchItem]
    updated: int
    not_found: list[ManifestPatchNotFound]
