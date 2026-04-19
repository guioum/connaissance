"""Module commands/config : mutations typées de scoring-courriels.yaml.

Toutes les mutations passent par des atomes typés
(`add_domain_marketing`, `set_weight`, `set_seuil`, etc.), jamais par
du YAML composé par Claude. Préserve les commentaires utilisateur via
`ruamel.yaml` (fallback PyYAML en dev si ruamel non installé).

Expose :
- `scoring_show()` : lire le YAML utilisateur.
- `scoring_set(dry_run=True, **atoms)` : appliquer des mutations atomiques.
- `scoring_diff()` : diff vs template.
- `scoring_validate()` : valider la config.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from connaissance.core.paths import CONNAISSANCE_ROOT

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_SCORING = PACKAGE_ROOT / "config" / "scoring-courriels.yaml"
USER_SCORING = CONNAISSANCE_ROOT / ".config" / "scoring-courriels.yaml"


def _load_yaml_preserve_comments(path: Path):
    """Charger un YAML en préservant les commentaires. Utilise ruamel.yaml
    si disponible, sinon fallback PyYAML (commentaires perdus).

    Retourne (parsed, yaml_instance_or_none).
    """
    try:
        from ruamel.yaml import YAML  # pyright: ignore[reportMissingImports]
        yaml_inst = YAML()
        yaml_inst.preserve_quotes = True
        yaml_inst.indent(mapping=2, sequence=4, offset=2)
        with open(path, "r", encoding="utf-8") as f:
            return yaml_inst.load(f), yaml_inst
    except ImportError:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")), None


def _dump_yaml_preserve_comments(data, path: Path, yaml_inst) -> None:
    """Écrire un YAML en préservant les commentaires si ruamel est disponible."""
    if yaml_inst is not None:
        with open(path, "w", encoding="utf-8") as f:
            yaml_inst.dump(data, f)
        return
    import yaml
    path.write_text(
        yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _ensure_user_scoring() -> Path:
    """Copier le template si absent et retourner le chemin utilisateur."""
    if USER_SCORING.exists():
        return USER_SCORING
    if TEMPLATE_SCORING.exists():
        USER_SCORING.parent.mkdir(parents=False, exist_ok=True)
        import shutil
        shutil.copy2(TEMPLATE_SCORING, USER_SCORING)
    return USER_SCORING


# --- API publique ---


def scoring_show() -> dict:
    """Retourner la config scoring utilisateur en dict."""
    path = _ensure_user_scoring()
    data, _ = _load_yaml_preserve_comments(path)
    return dict(data) if data else {}


def scoring_set(dry_run: bool = True,
                add_domain_marketing: list[str] | None = None,
                remove_domain_marketing: list[str] | None = None,
                add_domain_personnel: list[str] | None = None,
                remove_domain_personnel: list[str] | None = None,
                add_pattern_actionnable: list[str] | None = None,
                add_pattern_promotionnel: list[str] | None = None,
                set_weight: dict[str, int] | None = None,
                set_seuil: dict[str, int] | None = None) -> dict:
    """Appliquer des mutations atomiques à scoring-courriels.yaml.

    Toutes les mutations sont appliquées en une transaction. Retourne un
    diff détaillé + le statut `written` + `regex_errors` pour les patterns
    invalides + `post_validation_ok` après mutation.

    Si `dry_run=True` (défaut), ne touche pas au fichier. Sinon écrit
    via ruamel.yaml pour préserver les commentaires.
    """
    path = _ensure_user_scoring()
    data, yaml_inst = _load_yaml_preserve_comments(path)
    if data is None:
        return {
            "diff": [],
            "written": False,
            "regex_errors": [],
            "post_validation_ok": False,
            "error": "config introuvable",
        }

    diff: list[dict] = []
    regex_errors: list[str] = []

    def _append_to_list(section: str, items: list[str], op_label: str = "add"):
        current = data.get(section) or []
        before = list(current)
        added = []
        for item in items:
            if item not in current:
                current.append(item)
                added.append(item)
        if added:
            data[section] = current
            diff.append({"key": section, "op": op_label, "before": before, "after": list(current)})

    def _remove_from_list(section: str, items: list[str]):
        current = data.get(section) or []
        before = list(current)
        removed = [i for i in items if i in current]
        for item in removed:
            current.remove(item)
        if removed:
            data[section] = current
            diff.append({"key": section, "op": "remove", "before": before, "after": list(current)})

    def _validate_patterns(items: list[str]):
        for p in items:
            try:
                re.compile(p)
            except re.error as exc:
                regex_errors.append(f"{p}: {exc}")

    # Valider AVANT toute mutation — un seul pattern regex invalide doit
    # rejeter le lot sans laisser de mutations partielles dans `data`.
    if add_pattern_actionnable:
        _validate_patterns(add_pattern_actionnable)
    if add_pattern_promotionnel:
        _validate_patterns(add_pattern_promotionnel)
    if regex_errors:
        return {
            "diff": [],
            "written": False,
            "regex_errors": regex_errors,
            "post_validation_ok": False,
            "error": "patterns regex invalides — aucune mutation appliquée",
        }

    if add_domain_marketing:
        _append_to_list("domaines_marketing", [d.lower() for d in add_domain_marketing])
    if remove_domain_marketing:
        _remove_from_list("domaines_marketing", [d.lower() for d in remove_domain_marketing])
    if add_domain_personnel:
        _append_to_list("domaines_personnels", [d.lower() for d in add_domain_personnel])
    if remove_domain_personnel:
        _remove_from_list("domaines_personnels", [d.lower() for d in remove_domain_personnel])
    if add_pattern_actionnable:
        _append_to_list("patterns_sujet_actionnable", add_pattern_actionnable)
    if add_pattern_promotionnel:
        _append_to_list("patterns_sujet_promotionnel", add_pattern_promotionnel)

    if set_weight:
        poids = data.get("poids") or {}
        for k, v in set_weight.items():
            before = poids.get(k)
            if before != v:
                diff.append({"key": f"poids.{k}", "op": "set", "before": before, "after": v})
                poids[k] = v
        data["poids"] = poids

    if set_seuil:
        seuils = data.get("seuils") or {}
        for k, v in set_seuil.items():
            before = seuils.get(k)
            if before != v:
                diff.append({"key": f"seuils.{k}", "op": "set", "before": before, "after": v})
                seuils[k] = v
        data["seuils"] = seuils

    if not diff or dry_run:
        return {
            "diff": diff,
            "written": False,
            "regex_errors": [],
            "post_validation_ok": True,
            "dry_run": dry_run,
        }

    _dump_yaml_preserve_comments(data, path, yaml_inst)

    validation = scoring_validate()
    return {
        "diff": diff,
        "written": True,
        "regex_errors": [],
        "post_validation_ok": validation["ok"],
        "dry_run": False,
    }


def scoring_diff() -> dict:
    """Diff entre la config utilisateur et le template."""
    path = _ensure_user_scoring()
    user_data, _ = _load_yaml_preserve_comments(path)
    if not TEMPLATE_SCORING.exists():
        return {"changes": []}
    template_data, _ = _load_yaml_preserve_comments(TEMPLATE_SCORING)

    changes: list[dict] = []

    def walk(user_d: Any, tmpl_d: Any, prefix: str = ""):
        if isinstance(user_d, dict) and isinstance(tmpl_d, dict):
            for k in sorted(set(list(user_d.keys()) + list(tmpl_d.keys()))):
                key = f"{prefix}.{k}" if prefix else k
                if k not in tmpl_d:
                    changes.append({"key": key, "op": "add", "before": None, "after": user_d[k]})
                elif k not in user_d:
                    changes.append({"key": key, "op": "remove", "before": tmpl_d[k], "after": None})
                elif user_d[k] != tmpl_d[k]:
                    if isinstance(user_d[k], (dict, list)) and isinstance(tmpl_d[k], (dict, list)):
                        walk(user_d[k], tmpl_d[k], key)
                    else:
                        changes.append({"key": key, "op": "set", "before": tmpl_d[k], "after": user_d[k]})
        elif isinstance(user_d, list) and isinstance(tmpl_d, list):
            added = [i for i in user_d if i not in tmpl_d]
            removed = [i for i in tmpl_d if i not in user_d]
            for i in added:
                changes.append({"key": prefix, "op": "add", "before": None, "after": i})
            for i in removed:
                changes.append({"key": prefix, "op": "remove", "before": i, "after": None})

    walk(user_data or {}, template_data or {})
    return {"changes": changes}


def scoring_validate() -> dict:
    """Vérifier que la config est bien formée (regex valides, seuils cohérents)."""
    path = _ensure_user_scoring()
    data, _ = _load_yaml_preserve_comments(path)
    if not data:
        return {"ok": False, "errors": ["config introuvable"]}

    errors: list[str] = []

    # Vérifier les patterns regex
    pattern_sections = [
        "patterns_sujet_actionnable", "patterns_sujet_promotionnel",
        "patterns_sujet_notification_banale", "patterns_corps_actionnable",
        "patterns_newsletter_corps", "patterns_marketing", "patterns_noreply",
    ]
    for section in pattern_sections:
        patterns = data.get(section) or []
        for p in patterns:
            if isinstance(p, list):
                for sub in p:
                    try:
                        re.compile(sub)
                    except re.error as exc:
                        errors.append(f"{section}: regex invalide '{sub}' — {exc}")
                continue
            try:
                re.compile(p)
            except re.error as exc:
                errors.append(f"{section}: regex invalide '{p}' — {exc}")

    # Cohérence des seuils
    seuils = data.get("seuils") or {}
    capturer = seuils.get("capturer")
    ignorer = seuils.get("ignorer")
    if capturer is not None and ignorer is not None and capturer <= ignorer:
        errors.append(
            f"seuils.capturer ({capturer}) doit être > seuils.ignorer ({ignorer})"
        )

    return {"ok": not errors, "errors": errors}
