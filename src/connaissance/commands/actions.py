"""Module commands/actions : lister les actions ouvertes des chronologies.

Une action est une ligne `- [ ] description [— échéance YYYY-MM-DD]` dans
une `chronologie.md`. Auparavant ce check était dans audit.py sous le nom
`actions_a_reviser`, mais il ne relève pas de l'intégrité technique : c'est
du contenu métier (engagements à suivre), d'où ce module dédié.
"""

import re
from datetime import date, timedelta

from connaissance.core.paths import BASE_PATH

SYNTHESE = BASE_PATH / "Connaissance" / "Synthèse"

_PATTERN_ACTION = re.compile(
    r'^- \[ \] (.+?)(?:\s*—\s*(?:échéance\s+)?(\d{4}-\d{2}-\d{2}))?$',
    re.MULTILINE,
)


def _iter_actions():
    """Parcourir toutes les actions ouvertes de toutes les chronologies."""
    if not SYNTHESE.exists():
        return
    today = date.today()
    seuil_90j = today - timedelta(days=90)

    for chrono in SYNTHESE.rglob("chronologie.md"):
        try:
            content = chrono.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        entity_rel = str(chrono.parent.relative_to(SYNTHESE))

        for match in _PATTERN_ACTION.finditer(content):
            description = match.group(1).strip()
            echeance_str = match.group(2)

            echeance = None
            status = "ouverte"
            raison = None

            if echeance_str:
                try:
                    echeance = date.fromisoformat(echeance_str)
                    if echeance < today:
                        status = "expiree"
                        raison = "échéance dépassée"
                except ValueError:
                    pass
            else:
                try:
                    chrono_mtime = date.fromtimestamp(chrono.stat().st_mtime)
                    if chrono_mtime < seuil_90j:
                        status = "expiree"
                        raison = "ouverte > 90 jours sans mise à jour"
                except OSError:
                    pass

            yield {
                "entite": entity_rel,
                "action": description,
                "echeance": echeance_str,
                "status": status,
                "raison": raison,
                "source_path": str(chrono.relative_to(SYNTHESE.parent)),
            }


def list_actions(status: str = "all", entity: str | None = None) -> dict:
    """Lister les actions selon un statut et/ou une entité.

    `status` ∈ {"all", "ouverte", "expiree"} (défaut : all).
    `entity` : filtrer au format `type/slug` (ex : `organismes/fmrq`).
    """
    if status not in ("all", "ouverte", "expiree"):
        raise ValueError(f"status invalide : {status}")

    items = []
    for a in _iter_actions():
        if entity and a["entite"] != entity:
            continue
        if status != "all" and a["status"] != status:
            continue
        items.append(a)

    # Tri : expirées d'abord (par échéance asc), puis ouvertes
    items.sort(key=lambda a: (
        0 if a["status"] == "expiree" else 1,
        a["echeance"] or "9999",
    ))

    return {
        "items": items,
        "total": len(items),
        "filter": {"status": status, "entity": entity},
    }
