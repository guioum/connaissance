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


def resumes_manquants(db, source_type=None):
    """Résumés manquants par source."""
    rows = db.missing_resumes(source_type)
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


def moc_perimes():
    """MOC périmés ou manquants par catégorie."""
    if not RESUMES.exists():
        return {"total": 0, "categories": []}

    # Scanner les catégories et leur mtime max dans les résumés
    categories_mtime = {}
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
        if cat not in categories_mtime or file_mtime > categories_mtime[cat]:
            categories_mtime[cat] = file_mtime

    # Comparer avec les MOC existants
    sujets_dir = SYNTHESE / "sujets"
    perimes = []
    for cat, resume_mtime in sorted(categories_mtime.items()):
        moc_path = sujets_dir / f"{cat}.md"
        if not moc_path.exists():
            perimes.append({"category": cat, "status": "manquant"})
        elif moc_path.stat().st_mtime < resume_mtime:
            perimes.append({"category": cat, "status": "périmé"})

    return {"total": len(perimes), "categories": perimes}


def estimer_couts(db, mode="batch"):
    """Estimer les coûts du pipeline."""
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
    manquants = db.missing_resumes()
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
           source: str | None = None, mode: str = "batch") -> dict:
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
        result["resumes_manquants"] = resumes_manquants(db, source)
    if "resumes_perimes" in active:
        result["resumes_perimes"] = resumes_perimes(db)
    if "non_organises" in active:
        result["non_organises"] = non_organises(db)
    if "synthese_perimee" in active:
        result["synthese_perimee"] = synthese_perimee(db)
    if "moc_perimes" in active:
        result["moc_perimes"] = moc_perimes()
    if "couts" in active:
        result["couts"] = estimer_couts(db, mode)
    if "stats" in active:
        result["stats"] = stats(db)

    if owns_db:
        db.close()
    return result


def costs(db: TrackingDB | None = None, mode: str = "batch") -> dict:
    """Estimation des coûts du pipeline (schema Couts)."""
    owns_db = db is None
    if db is None:
        db = TrackingDB()
    try:
        return estimer_couts(db, mode)
    finally:
        if owns_db:
            db.close()
