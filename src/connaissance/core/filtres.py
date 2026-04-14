"""Système de filtres unifié pour la transcription.

Charge ~/Connaissance/.config/filtres.yaml et expose des fonctions
de filtrage pour les 3 sources (documents, courriels, notes).

Usage :
    from connaissance.core.filtres import Filtres

    f = Filtres()
    ok, reason = f.filter_document(path, since=since, until=until)
    ok, reason = f.filter_courriel(msg_dict, since=since, until=until)
    ok, reason = f.filter_note(path, content, since=since, until=until)
    score, reasons = f.score_courriel(msg_dict)
"""

import fnmatch
import re
import yaml
from datetime import datetime, timezone
from pathlib import Path

from connaissance.core.paths import BASE_PATH, CONNAISSANCE_ROOT, require_connaissance_root

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_FILTRES = PACKAGE_ROOT / "config" / "filtres.yaml"
USER_FILTRES = CONNAISSANCE_ROOT / ".config" / "filtres.yaml"

TEMPLATE_SCORING = PACKAGE_ROOT / "config" / "scoring-courriels.yaml"
USER_SCORING = CONNAISSANCE_ROOT / ".config" / "scoring-courriels.yaml"

DOCUMENTS_DIR = BASE_PATH / "Documents"


def _load_yaml(path):
    """Charger un fichier YAML."""
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def _ensure_user_config(template, user_path):
    """Copier le template vers le config utilisateur si absent.

    Prérequis : ~/Connaissance/ doit déjà exister. Créé .config/ si besoin
    mais jamais Connaissance/ elle-même.
    """
    if not user_path.exists() and template.exists():
        require_connaissance_root()
        user_path.parent.mkdir(parents=False, exist_ok=True)
        import shutil
        shutil.copy2(template, user_path)
    return user_path


class Filtres:
    """Système de filtres unifié pour les 3 sources."""

    def __init__(self, config_path=None):
        require_connaissance_root()
        config_file = config_path or _ensure_user_config(TEMPLATE_FILTRES, USER_FILTRES)
        self._config = _load_yaml(config_file)
        self._scoring_config = None  # chargé à la demande

    @property
    def docs_config(self):
        return self._config.get("documents", {})

    @property
    def courriels_config(self):
        return self._config.get("courriels", {})

    @property
    def notes_config(self):
        return self._config.get("notes", {})

    @property
    def scoring_config(self):
        if self._scoring_config is None:
            cfg_file = _ensure_user_config(TEMPLATE_SCORING, USER_SCORING)
            self._scoring_config = _load_yaml(cfg_file)
        return self._scoring_config

    # --- Documents ---

    def filter_document(self, path, since=None, until=None):
        """Filtrer un document. Retourne (accepté, raison_rejet)."""
        path = Path(path)
        cfg = self.docs_config

        # Extension
        extensions = set(cfg.get("extensions", []))
        if extensions and path.suffix.lower() not in extensions:
            return False, "extension"

        # Dossiers techniques
        tech_dirs = set(cfg.get("dossiers_techniques", []))
        if any(d in path.parts for d in tech_dirs):
            return False, "dossier_technique"

        # Périmètre
        try:
            rel = path.relative_to(DOCUMENTS_DIR)
            rel_str = str(rel)
        except ValueError:
            rel_str = str(path)

        # Dossier racine commençant par "- " → workflow, exclu
        root_dir = Path(rel_str).parts[0] if Path(rel_str).parts else ""
        if root_dir.startswith("- "):
            return False, "dossier_workflow"

        # Inclusions (prioritaires)
        for incl in cfg.get("dossiers_inclus", []):
            if rel_str.startswith(incl):
                break
        else:
            # Exclusions
            for excl in cfg.get("dossiers_exclus", []):
                if rel_str.startswith(excl):
                    return False, "perimetre_exclu"

            # Patterns d'exclusion (sur chaque composant du chemin)
            for pattern in cfg.get("patterns_exclus", []):
                for part in Path(rel_str).parts:
                    if fnmatch.fnmatch(part, pattern):
                        return False, "pattern_exclu"

        # Date
        if since or until:
            ok = self._check_date_file(path, since, until)
            if not ok:
                return False, "hors_date"

        return True, ""

    def _check_date_file(self, path, since, until):
        """Vérifier si un fichier est dans la tranche de dates (mtime/birthtime)."""
        try:
            st = path.stat()
        except OSError:
            return True  # inclure par défaut

        dates = [datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)]
        if hasattr(st, "st_birthtime") and st.st_birthtime > 0:
            dates.append(datetime.fromtimestamp(st.st_birthtime, tz=timezone.utc))

        for d in dates:
            if since and d < since:
                continue
            if until and d >= until:
                continue
            return True
        return False

    # --- Courriels ---

    def filter_courriel(self, msg_dict, since=None, until=None):
        """Filtrer un courriel. Retourne (accepté, raison_rejet)."""
        cfg = self.courriels_config

        # Date
        if since or until:
            date = msg_dict.get("date")
            if date:
                if since and date < since:
                    return False, "hors_date"
                if until and date >= until:
                    return False, "hors_date"
            elif since:  # pas de date + filtre actif → exclure
                return False, "sans_date"

        # Scoring
        if cfg.get("scoring", False):
            score, reasons = self.score_courriel(msg_dict)
            seuil_ignorer = cfg.get("scoring_seuil_ignorer", -1)
            if score <= seuil_ignorer:
                return False, f"scoring:{score} ({', '.join(reasons[:3])})"

        return True, ""

    def is_courriel_folder_ignored(self, folder_name):
        """Vérifier si un dossier IMAP est ignoré."""
        ignored = self.courriels_config.get("dossiers_ignores", [])
        return folder_name.lower() in [d.lower() for d in ignored]

    def score_courriel(self, msg_dict):
        """Scorer un courriel. Retourne (score, raisons)."""
        cfg = self.scoring_config
        if not cfg:
            return 0, []

        score = 0
        reasons = []
        poids = cfg.get("poids", {})

        from_addr = (msg_dict.get("from", "") or "").lower()
        from_display = (msg_dict.get("from_display", "") or "").lower()
        subject = (msg_dict.get("subject", "") or "")
        body = (msg_dict.get("body", "") or "")
        attachments = msg_dict.get("attachments", [])
        folder = (msg_dict.get("folder", "") or "").lower()
        is_html_only = msg_dict.get("is_html_only", False)

        domain = from_addr.split("@")[-1] if "@" in from_addr else ""

        # --- Signaux négatifs ---

        # Réseaux sociaux
        if domain in [d.lower() for d in cfg.get("domaines_reseaux_sociaux", [])]:
            w = poids.get("reseau_social", -3)
            score += w
            reasons.append(f"réseau social ({domain}) [{w:+d}]")

        # Marketing
        if domain in [d.lower() for d in cfg.get("domaines_marketing", [])]:
            w = poids.get("adresse_marketing", -2)
            score += w
            reasons.append(f"domaine marketing ({domain}) [{w:+d}]")

        # Newsletter (List-Unsubscribe)
        headers = msg_dict.get("headers", {})
        if headers.get("list-unsubscribe"):
            w = poids.get("newsletter", -2)
            score += w
            reasons.append(f"newsletter (List-Unsubscribe) [{w:+d}]")

        # HTML-only
        if is_html_only:
            w = poids.get("courriel_html_only", -2)
            score += w
            reasons.append(f"HTML-only [{w:+d}]")

        # Noreply
        if "noreply" in from_addr or "no-reply" in from_addr:
            w = poids.get("noreply", -1)
            score += w
            reasons.append(f"noreply [{w:+d}]")

        # Sujet promotionnel
        for pattern in cfg.get("patterns_sujet_promotionnel", []):
            if re.search(pattern, subject, re.IGNORECASE):
                w = poids.get("sujet_promotionnel", -2)
                score += w
                reasons.append(f"sujet promo [{w:+d}]")
                break

        # --- Signaux positifs ---

        # Domaine personnel
        if domain in [d.lower() for d in cfg.get("domaines_personnels", [])]:
            w = poids.get("domaine_personnel", 2)
            score += w
            reasons.append(f"domaine personnel [{w:+d}]")

        # Courriel envoyé (dossier Sent)
        if folder in ("sent", "envoyés", "envoy&aoq-s"):
            w = poids.get("courriel_envoye", 2)
            score += w
            reasons.append(f"envoyé [{w:+d}]")

        # Sujet actionnable
        for pattern in cfg.get("patterns_sujet_actionnable", []):
            if re.search(pattern, subject, re.IGNORECASE):
                w = poids.get("sujet_actionnable", 3)
                score += w
                reasons.append(f"sujet actionnable [{w:+d}]")
                break

        # Pièces jointes documents
        doc_exts = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
        if any(Path(a.get("filename", "")).suffix.lower() in doc_exts for a in attachments):
            w = poids.get("piece_jointe_document", 2)
            score += w
            reasons.append(f"PJ document [{w:+d}]")

        # Domaine gouvernemental
        for suffix in cfg.get("suffixes_gouvernementaux", []):
            if domain.endswith(suffix.lower().lstrip(".")):
                w = poids.get("gouvernemental", 2)
                score += w
                reasons.append(f"gouvernemental [{w:+d}]")
                break

        # Réponse/transfert
        if subject.lower().startswith(("re:", "fwd:", "tr:", "ref:")):
            w = poids.get("reponse_conversation", 1)
            score += w
            reasons.append(f"réponse/transfert [{w:+d}]")

        # --- Signaux Priorité 1 ---

        seuils_num = cfg.get("seuils_numeriques", {})

        # Corps quasi-vide
        body_len = len(body.strip())
        corps_min = seuils_num.get("corps_min", 50)
        if body_len < corps_min:
            w = poids.get("corps_quasi_vide", -1)
            score += w
            reasons.append(f"corps quasi-vide ({body_len} chars) [{w:+d}]")

        # Notification banale (OTP, sign-in, password reset)
        for pattern in cfg.get("patterns_sujet_notification_banale", []):
            if re.search(pattern, subject, re.IGNORECASE):
                w = poids.get("notification_banale", -1)
                score += w
                reasons.append(f"notification banale [{w:+d}]")
                break

        # Corps actionnable (montants, références, échéances)
        corps_preview = body[:seuils_num.get("corps_preview", 1000)]
        corps_matches = 0
        for pattern in cfg.get("patterns_corps_actionnable", []):
            if re.search(pattern, corps_preview, re.IGNORECASE):
                corps_matches += 1
        if corps_matches >= 2:
            w = poids.get("corps_actionnable_multiple", 2)
            score += w
            reasons.append(f"corps multi-actionnable ({corps_matches}) [{w:+d}]")
        elif corps_matches == 1:
            w = poids.get("corps_actionnable_simple", 1)
            score += w
            reasons.append(f"corps actionnable [{w:+d}]")

        # Noreply sans contenu actionnable
        noreply_patterns = cfg.get("patterns_noreply", ["noreply", "no-reply", "donotreply", "nepasrepondre"])
        if isinstance(noreply_patterns, list) and noreply_patterns and isinstance(noreply_patterns[0], str) and noreply_patterns[0].startswith("("):
            # Pattern regex unique (ex: "(?:noreply|no-reply|...)")
            is_noreply = bool(re.search(noreply_patterns[0], from_addr, re.IGNORECASE))
        else:
            is_noreply = any(p in from_addr for p in noreply_patterns)
        if is_noreply and corps_matches == 0:
            w = poids.get("noreply_sans_actionnable", -1)
            score += w
            reasons.append(f"noreply sans actionnable [{w:+d}]")

        # Marketing patterns (regex sur l'adresse)
        for pattern in cfg.get("patterns_marketing", []):
            if re.search(pattern, from_addr, re.IGNORECASE):
                w = poids.get("sous_domaine_marketing", -1)
                score += w
                reasons.append(f"pattern marketing [{w:+d}]")
                break

        # Expéditeur personnel (prenom.nom@domain)
        pattern_perso = cfg.get("pattern_expediteur_personnel", r"^[a-z]+[.\-][a-z]+@")
        patterns_gen = [p for group in cfg.get("patterns_generiques", []) for p in
                        (group if isinstance(group, list) else [group])]
        is_generic = any(re.match(p, from_addr, re.IGNORECASE) for p in patterns_gen) if patterns_gen else False
        if re.match(pattern_perso, from_addr) and not is_generic and not is_noreply:
            w = poids.get("expediteur_personnel", 1)
            score += w
            reasons.append(f"expéditeur personnel [{w:+d}]")

        # --- Signaux Priorité 2 (nouveaux) ---

        # Newsletter body (patterns unsubscribe dans le corps)
        for pattern in cfg.get("patterns_newsletter_corps", []):
            if re.search(pattern, corps_preview, re.IGNORECASE):
                w = poids.get("newsletter_corps", -1)
                score += w
                reasons.append(f"newsletter corps [{w:+d}]")
                break

        # User en CC seulement (pas en To)
        to_field = (msg_dict.get("to", "") or "").lower()
        cc_field = (msg_dict.get("cc", "") or "").lower()
        user_domains = [d.lower() for d in cfg.get("domaines_personnels", [])]
        if user_domains:
            in_to = any(d in to_field for d in user_domains)
            in_cc = any(d in cc_field for d in user_domains)
            if in_cc and not in_to:
                w = poids.get("cc_seulement", -1)
                score += w
                reasons.append(f"CC seulement [{w:+d}]")

        # Adresse générique (info@, support@, notification@)
        for pattern_group in cfg.get("patterns_generiques", []):
            patterns = pattern_group if isinstance(pattern_group, list) else [pattern_group]
            for pattern in patterns:
                if re.match(pattern, from_addr, re.IGNORECASE):
                    w = poids.get("adresse_generique", -1)
                    score += w
                    reasons.append(f"adresse générique [{w:+d}]")
                    break
            else:
                continue
            break

        return score, reasons

    # --- Notes ---

    def filter_note(self, path, content=None, since=None, until=None):
        """Filtrer une note. Retourne (accepté, raison_rejet)."""
        path = Path(path)
        cfg = self.notes_config

        # Dossiers ignorés
        ignored = cfg.get("dossiers_ignores", [])
        for d in ignored:
            if d in path.parts:
                return False, f"dossier_ignore:{d}"

        # Date (mtime/birthtime du fichier, comme pour les documents)
        if since or until:
            ok = self._check_date_file(path, since, until)
            if not ok:
                return False, "hors_date"

        return True, ""

    def _check_date_frontmatter(self, content, since, until):
        """Vérifier les dates created/modified du frontmatter."""
        if not content.startswith("---"):
            return True  # pas de frontmatter → inclure
        try:
            end = content.index("---", 3)
            fm_text = content[3:end]
        except ValueError:
            return True

        dates = []
        for field in ("created", "modified"):
            match = re.search(rf'^{field}:\s*(\d{{4}}-\d{{2}}-\d{{2}})', fm_text, re.MULTILINE)
            if match:
                try:
                    dates.append(datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc))
                except ValueError:
                    pass

        if not dates:
            return True  # pas de dates → inclure

        for d in dates:
            if since and d < since:
                continue
            if until and d >= until:
                continue
            return True
        return False

    # --- Utilitaires ---

    def min_attachment_size(self):
        """Taille minimale des PJ images pour les courriels."""
        return self.courriels_config.get("min_attachment_size", 50000)
