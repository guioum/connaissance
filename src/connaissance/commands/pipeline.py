"""Module commands/pipeline : détection du travail du pipeline.

Expose :
- `detect(db=None, steps=["all"], source=None) -> PipelineDetection`
- `costs(db=None, mode="batch") -> Couts`
- `resumes_manquants, non_organises, synthese_perimee, moc_perimes,
   resumes_perimes, stats, estimer_couts` (helpers)
"""

import yaml

from connaissance.core.paths import BASE_PATH
from connaissance.core.tracking import TrackingDB

CONNAISSANCE = BASE_PATH / "Connaissance"
RESUMES = CONNAISSANCE / "Résumés"
SYNTHESE = CONNAISSANCE / "Synthèse"


def resumes_manquants(db, source_type=None, since=None, until=None):
    """Résumés manquants par source. ``since``/``until`` en YYYY-MM-DD."""
    rows = db.missing_resumes(source_type, since=since, until=until)
    by_source = {}
    for r in rows:
        st = r.get("source_type") or "inconnu"
        by_source.setdefault(st, []).append(r["path"])
    return {
        "total": len(rows),
        "par_source": {k: len(v) for k, v in by_source.items()},
        "fichiers": [r["path"] for r in rows],
    }


def non_organises(db):
    """Résumés sans entité assignée."""
    rows = db.unorganized_resumes()
    return {
        "total": len(rows),
        "fichiers": [r["path"] for r in rows],
    }


def synthese_perimee(db):
    """Entités dont la synthèse est périmée ou manquante."""
    rows = db.stale_synthesis()
    return {
        "total": len(rows),
        "entites": [
            {
                "entity_type": r["entity_type"],
                "entity_slug": r["entity_slug"],
                "latest_resume": r["latest_resume"],
                "synthesis_updated": r["synthesis_updated"],
            }
            for r in rows
        ],
    }


# Seuil MOC : nombre minimal de résumés nouveaux (plus récents que le MOC)
# avant de marquer le MOC comme « périmé ». Régénérer un MOC à chaque résumé
# est le plus gros contributeur de bruit/dépense sur les gros rattrapages.
MOC_STALE_THRESHOLD = 3


def moc_perimes(threshold: int = MOC_STALE_THRESHOLD):
    """MOC périmés ou manquants par catégorie.

    Un MOC est périmé seulement quand ≥ ``threshold`` résumés de sa catégorie
    ont une mtime postérieure à celle du MOC. Les régénérations déclenchées
    par un seul nouveau résumé étaient la cause principale des MOC régénérés
    à perte — le MOC agrège des dizaines d'items, ajouter un seul ne change
    rien d'utile à la vue globale.

    ``threshold`` peut être ramené à 1 pour restaurer l'ancien comportement
    (utile quand on veut forcer une régénération fine).
    """
    if not RESUMES.exists():
        return {"total": 0, "categories": []}

    # Scanner les catégories : mtime max + nombre de résumés par catégorie
    categories_mtime: dict[str, float] = {}
    categories_mtimes: dict[str, list[float]] = {}
    for md_file in RESUMES.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not content.startswith("---"):
            continue
        try:
            fm_text = content.split("---", 2)[1]
            fm = yaml.safe_load(fm_text)
        except (IndexError, yaml.YAMLError, ValueError):
            continue
        if not fm or not isinstance(fm, dict):
            continue
        cat = fm.get("category")
        if not cat:
            continue
        file_mtime = md_file.stat().st_mtime
        categories_mtimes.setdefault(cat, []).append(file_mtime)
        if cat not in categories_mtime or file_mtime > categories_mtime[cat]:
            categories_mtime[cat] = file_mtime

    # Comparer avec les MOC existants
    sujets_dir = SYNTHESE / "sujets"
    perimes = []
    for cat, resume_mtime in sorted(categories_mtime.items()):
        moc_path = sujets_dir / f"{cat}.md"
        if not moc_path.exists():
            perimes.append({"category": cat, "status": "manquant",
                            "nouveaux_resumes": len(categories_mtimes.get(cat, []))})
            continue
        moc_mtime = moc_path.stat().st_mtime
        if moc_mtime >= resume_mtime:
            continue
        newer = sum(1 for m in categories_mtimes.get(cat, []) if m > moc_mtime)
        if newer >= threshold:
            perimes.append({"category": cat, "status": "périmé",
                            "nouveaux_resumes": newer,
                            "seuil": threshold})

    return {"total": len(perimes), "categories": perimes,
            "seuil": threshold}


def estimer_couts(db, mode="batch", since=None, until=None):
    """Estimer les coûts du pipeline. ``since``/``until`` en YYYY-MM-DD
    appliquent au périmètre des résumés manquants uniquement ; les
    synthèses et MOC périmés ne sont pas filtrés par date (leur
    'périmé' est déjà relatif aux mtime filesystem)."""
    # Coûts unitaires (prix standard)
    PRIX = {
        "document": 0.03,
        "courriel": 0.02,
        "note": 0.025,
        "entite": 0.05,
        "moc": 0.05,
    }
    facteur = 0.5 if mode == "batch" else 1.0

    # Résumés manquants par source
    manquants = db.missing_resumes(since=since, until=until)
    by_source = {}
    for r in manquants:
        st = r.get("source_type") or "inconnu"
        by_source[st] = by_source.get(st, 0) + 1

    cout_resumes = (
        by_source.get("document", 0) * PRIX["document"] * facteur
        + by_source.get("courriel", 0) * PRIX["courriel"] * facteur
        + by_source.get("note", 0) * PRIX["note"] * facteur
    )

    # Synthèse périmée
    stale = db.stale_synthesis()
    cout_synthese = len(stale) * PRIX["entite"]

    # MOC périmés
    moc = moc_perimes()
    cout_moc = moc["total"] * PRIX["moc"]

    cout_total = cout_resumes + cout_synthese + cout_moc

    return {
        "mode": mode,
        "resumes": {
            "par_source": by_source,
            "facteur": facteur,
            "cout": round(cout_resumes, 2),
        },
        "synthese": {
            "entites_perimees": len(stale),
            "cout": round(cout_synthese, 2),
        },
        "moc": {
            "perimes": moc["total"],
            "cout": round(cout_moc, 2),
        },
        "total": round(cout_total, 2),
    }


def resumes_perimes(db):
    """Résumés dont la transcription source a été modifiée depuis."""
    rows = db.stale_resumes()
    return {
        "total": len(rows),
        "fichiers": [
            {"resume": r["resume_path"], "transcription": r["trans_path"]}
            for r in rows
        ],
    }


def stats(db):
    """Statistiques globales de la base."""
    return db.stats()


# --- API publique ---


_STEP_ALL = ("resumes_manquants", "resumes_perimes", "non_organises",
             "synthese_perimee", "moc_perimes", "couts", "stats")


def detect(db: TrackingDB | None = None, steps: list[str] | None = None,
           source: str | None = None, mode: str = "batch",
           since: str | None = None, until: str | None = None,
           moc_threshold: int | None = None) -> dict:
    """Détecter le travail du pipeline (schema PipelineDetection).

    Parameters
    ----------
    steps : list[str] | None
        Sous-ensemble de {resumes_manquants, resumes_perimes, non_organises,
        synthese_perimee, moc_perimes, couts, stats, all}.
    source : str | None
        Filtrer `resumes_manquants` par source_type.
    mode : str
        Mode pour l'estimation des coûts ("batch" ou "interactif").
    since, until : str | None
        Intervalle de date (YYYY-MM-DD) appliqué à `resumes_manquants` et
        aux coûts associés. Filtre sur le champ `created` du frontmatter
        de la transcription.
    """
    owns_db = db is None
    if db is None:
        db = TrackingDB()

    if not steps or "all" in steps:
        active = set(_STEP_ALL)
    else:
        active = set(steps)

    result: dict = {}
    if "resumes_manquants" in active:
        result["resumes_manquants"] = resumes_manquants(
            db, source, since=since, until=until)
    if "resumes_perimes" in active:
        result["resumes_perimes"] = resumes_perimes(db)
    if "non_organises" in active:
        result["non_organises"] = non_organises(db)
    if "synthese_perimee" in active:
        result["synthese_perimee"] = synthese_perimee(db)
    if "moc_perimes" in active:
        result["moc_perimes"] = moc_perimes(
            threshold=moc_threshold if moc_threshold is not None else MOC_STALE_THRESHOLD)
    if "couts" in active:
        result["couts"] = estimer_couts(db, mode, since=since, until=until)
    if "stats" in active:
        result["stats"] = stats(db)
    # Signaler la plage si appliquée — facilite l'interprétation côté appelant
    if since or until:
        result["date_range"] = {"since": since, "until": until}

    if owns_db:
        db.close()
    return result


def costs(db: TrackingDB | None = None, mode: str = "batch",
          since: str | None = None, until: str | None = None,
          real: bool = False) -> dict:
    """Estimation des coûts du pipeline (schema Couts).

    ``since``/``until`` (YYYY-MM-DD) restreignent le périmètre des résumés
    manquants (mode estimation) ou la plage du journal ``llm_usage``
    (mode ``real``).

    ``real=True`` : retourne les coûts **réels** observés (tokens et USD
    depuis ``llm_usage``) plutôt qu'une estimation forfaitaire. Utile pour
    mesurer le gain effectif du prompt caching et calibrer le routing
    Haiku/Sonnet avec des chiffres et pas des approximations.
    """
    owns_db = db is None
    if db is None:
        db = TrackingDB()
    try:
        if real:
            result = db.usage_summary(since=since, until=until)
            result["source"] = "llm_usage"
            if since or until:
                result["date_range"] = {"since": since, "until": until}
            return result
        result = estimer_couts(db, mode, since=since, until=until)
        if since or until:
            result["date_range"] = {"since": since, "until": until}
        return result
    finally:
        if owns_db:
            db.close()
