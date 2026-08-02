"""Estimation des coûts : calibration empirique depuis llm_usage."""
from connaissance.commands import pipeline as P


def _seed_usage(db, operation, source_type, n, unit_cost, mode="batch"):
    for i in range(n):
        db._conn.execute(
            """INSERT INTO llm_usage (operation, source_type, mode, cost_usd,
                                      input_tokens, output_tokens,
                                      cache_creation_input_tokens,
                                      cache_read_input_tokens, units)
               VALUES (?, ?, ?, ?, 0, 0, 0, 0, 1)""",
            (operation, source_type, mode, unit_cost))
    db._conn.commit()


def test_observed_unit_costs_seuil_echantillons(tracking_db):
    _seed_usage(tracking_db, "resume", "document", 40, 0.005)
    _seed_usage(tracking_db, "resume", "courriel", 5, 0.002)   # < min_samples
    obs = tracking_db.observed_unit_costs(min_samples=30)
    assert obs["resume"]["n"] == 45
    assert "document" in obs["resume"]["par_source"]
    assert "courriel" not in obs["resume"]["par_source"]   # trop peu de points
    assert abs(obs["resume"]["par_source"]["document"]["unit_cost"] - 0.005) < 1e-9


def test_estimer_couts_projette_l_empirique(tracking_db, monkeypatch):
    """L'estimateur expose une projection aux coûts observés à côté du barème
    statique (qui surestimait ~3× : cache + mix Haiku ignorés)."""
    _seed_usage(tracking_db, "resume", "document", 100, 0.005)
    _seed_usage(tracking_db, "synthesis", None, 40, 0.016)
    monkeypatch.setattr(tracking_db, "missing_resumes",
                        lambda since=None, until=None: [
                            {"source_type": "document"}] * 10)
    monkeypatch.setattr(tracking_db, "stale_synthesis", lambda: ["e"] * 5)
    monkeypatch.setattr(P, "moc_perimes", lambda db=None: {"total": 0})

    res = P.estimer_couts(tracking_db, mode="batch")
    # Barème statique inchangé (compat) : 10 docs × 0.03 × 0.5
    assert res["resumes"]["cout"] == 0.15
    # Projection empirique : 10 × 0.005 et 5 × 0.016
    assert res["empirique"]["resumes"] == 0.05
    assert res["empirique"]["synthese"] == 0.08
    assert res["empirique"]["total"] == 0.13
    assert res["empirique"]["calibration"]["resume"]["n"] == 100


def test_estimer_couts_sans_historique_replie_sur_bareme(tracking_db, monkeypatch):
    monkeypatch.setattr(tracking_db, "missing_resumes",
                        lambda since=None, until=None: [
                            {"source_type": "document"}] * 4)
    monkeypatch.setattr(tracking_db, "stale_synthesis", lambda: ["e"] * 2)
    monkeypatch.setattr(P, "moc_perimes", lambda db=None: {"total": 1})
    res = P.estimer_couts(tracking_db, mode="batch")
    assert res["empirique"]["resumes"] == res["resumes"]["cout"]
    assert res["empirique"]["synthese"] == res["synthese"]["cout"]
    assert res["empirique"]["moc"] == res["moc"]["cout"]
