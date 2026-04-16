"""Module commands/summarize : préparation et enregistrement des résumés.

Construit les prompts pour le MCP `claude-api-mcp` (outils génériques
`mcp__claude_api__submit_batch` / `query_direct`) à partir des templates
dans `connaissance/cli/prompts/` et des transcriptions sur disque.

Le serveur MCP externe n'a pas de logique métier — toute la logique vit ici.

Expose :
- `plan(db=None, source=None) -> SummarizePlan`
- `prepare(paths, mode="batch", source=None) -> SummarizePrepare`
- `register(custom_id, content, db=None) -> SummarizeRegister`
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

import yaml

from connaissance.core.paths import BASE_PATH, CONNAISSANCE_ROOT
from connaissance.core.tracking import TrackingDB

TRANSCRIPTIONS = CONNAISSANCE_ROOT / "Transcriptions"
RESUMES = CONNAISSANCE_ROOT / "Résumés"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Configuration par défaut envoyée à claude-api-mcp.
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096

# Mapping source_type → clé du template prompts/resume_*.md
_PROMPT_BY_SOURCE = {
    "document": "resume_document.md",
    "courriel": "resume_courriel.md",
    "fil": "resume_fil.md",
    "note": "resume_note.md",
}


def _load_prompt_template(source: str) -> tuple[str, str]:
    """Charger un template prompt et retourner (system_md, user_md).

    Les blocs sont délimités par `<!-- system -->` et `<!-- user -->`.
    Toute ligne avant le premier marqueur est ignorée.
    """
    filename = _PROMPT_BY_SOURCE.get(source, _PROMPT_BY_SOURCE["document"])
    path = PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Template prompt introuvable : {path}")
    content = path.read_text(encoding="utf-8")

    # Split sur les marqueurs
    parts = re.split(r"<!-- (system|user) -->\n?", content)
    # parts = [prefix, "system", system_text, "user", user_text]
    system_text = ""
    user_text = ""
    i = 1
    while i < len(parts) - 1:
        marker = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if marker == "system":
            system_text = body.strip()
        elif marker == "user":
            user_text = body.strip()
        i += 2
    return system_text, user_text


def _substitute(template: str, variables: dict) -> str:
    """Substituer `{{var}}` par les valeurs du dict (sans format_map pour éviter
    les pièges d'échappement des accolades markdown)."""
    def replace(m):
        key = m.group(1).strip()
        return str(variables.get(key, m.group(0)))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replace, template)


def _read_transcription(path: Path) -> tuple[dict, str]:
    """Lire une transcription et séparer frontmatter / body."""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 4)
    if end < 0:
        return {}, content
    try:
        fm = yaml.safe_load(content[4:end]) or {}
    except yaml.YAMLError:
        fm = {}
    body = content[end + 4:].lstrip("\n")
    return (fm if isinstance(fm, dict) else {}), body


def _custom_id(rel_path: str) -> str:
    """Générer un custom_id stable pour un résumé depuis son chemin source."""
    return hashlib.sha256(rel_path.encode()).hexdigest()[:16]


def _rel_transcription(path: str | Path) -> str:
    p = Path(path)
    try:
        return str(p.relative_to(CONNAISSANCE_ROOT))
    except ValueError:
        return str(p)


def _source_label(file_type_or_source: str) -> str:
    """Normaliser un nom de source vers la clé des templates."""
    s = (file_type_or_source or "").lower()
    if s.startswith("courriel") or s in ("email", "mail"):
        return "courriel"
    if s.startswith("note"):
        return "note"
    if s.startswith("fil") or s.startswith("thread"):
        return "fil"
    return "document"


# --- API publique ---


def plan(db: TrackingDB | None = None, source: str | None = None) -> dict:
    """Lister les résumés manquants (schema SummarizePlan).

    Délègue à pipeline.detect mais transforme la sortie en
    `{missing: [{id, path, file_type}]}` prête pour `prepare()`.
    """
    from connaissance.commands import pipeline
    result = pipeline.detect(db=db, steps=["resumes_manquants"], source=source)
    rm = result.get("resumes_manquants", {})
    missing = []
    for rel in rm.get("fichiers", []):
        # Déduire le source_type depuis le chemin miroir
        parts = Path(rel).parts
        if "Documents" in parts:
            st = "document"
        elif "Courriels" in parts:
            st = "courriel"
        elif "Notes" in parts:
            st = "note"
        else:
            st = "document"
        missing.append({
            "id": _custom_id(rel),
            "path": rel,
            "file_type": st,
        })
    return {"missing": missing}


def prepare(paths: list[str] | str = "all", mode: str = "batch",
            source: str | None = None, db: TrackingDB | None = None) -> dict:
    """Construire les requêtes pour `mcp__claude_api__submit_batch`.

    Parameters
    ----------
    paths : list[str] | "all"
        Liste de chemins relatifs de transcriptions OU "all" pour
        tout résumer les transcriptions manquantes.
    mode : str
        "batch" (défaut, -50 %) ou "direct".
    source : str | None
        Filtre optionnel par source_type.

    Returns
    -------
    dict conforme au schema SummarizePrepare : `{requests, total, estimated_input_tokens}`.
    """
    if paths == "all" or paths is None:
        plan_result = plan(db=db, source=source)
        target_paths = [m["path"] for m in plan_result["missing"]]
    else:
        target_paths = list(paths)

    requests = []
    total_input_chars = 0

    for rel_path in target_paths:
        trans_path = CONNAISSANCE_ROOT / rel_path
        if not trans_path.exists():
            trans_path = Path(rel_path)
            if not trans_path.exists():
                continue

        fm, body = _read_transcription(trans_path)
        source_type = _source_label(fm.get("type") or source or "document")

        try:
            system_tmpl, user_tmpl = _load_prompt_template(source_type)
        except FileNotFoundError:
            continue

        variables = {
            "source": _rel_transcription(trans_path),
            "created": str(fm.get("created", "")),
            "modified": str(fm.get("modified", "")),
            "title": str(fm.get("title", "")),
            "date": str(fm.get("date", "")),
            "from": str(fm.get("from", "")),
            "message_id": str(fm.get("message-id", "")),
            "content": body,
            "thread_count": str(fm.get("message-count", "")),
            "message_ids_yaml": "",
        }

        user_text = _substitute(user_tmpl, variables)
        total_input_chars += len(system_tmpl) + len(user_text)

        requests.append({
            "custom_id": _custom_id(_rel_transcription(trans_path)),
            "system": system_tmpl,
            "user": user_text,
            "model": DEFAULT_MODEL,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "source_type": source_type,
            "source_path": _rel_transcription(trans_path),
        })

    return {
        "requests": requests,
        "total": len(requests),
        "estimated_input_tokens": total_input_chars // 4,  # ~4 chars/token
        "mode": mode,
    }


def register(custom_id: str, content: str,
             source_path: str | None = None,
             db: TrackingDB | None = None) -> dict:
    """Enregistrer le résultat d'un batch dans la base (schema SummarizeRegister).

    Le contenu `content` est le markdown produit par claude-api-mcp. Son
    frontmatter YAML doit contenir le champ `source:` pointant vers la
    transcription d'origine — on dérive le chemin de destination du résumé
    depuis ce champ (miroir dans `Résumés/{Source}/`).
    """
    if db is None:
        db = TrackingDB()

    # Parser le frontmatter pour extraire `source` et `type`
    fm: dict = {}
    body = content
    if content.startswith("---"):
        end = content.find("\n---", 4)
        if end > 0:
            try:
                fm = yaml.safe_load(content[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = content[end + 4:].lstrip("\n")

    source_rel = fm.get("source") or source_path
    if not source_rel:
        return {
            "path": "",
            "file_type": "resume",
            "frontmatter_injected": False,
            "error": "pas de champ source dans le frontmatter",
        }

    # Construire le chemin miroir du résumé
    src_path = Path(str(source_rel))
    try:
        rel = src_path.relative_to("Transcriptions")
        resume_rel = Path("Résumés") / rel
    except ValueError:
        # source déjà sous Résumés/ ? ou absolu ?
        resume_rel = Path("Résumés") / src_path.name

    resume_abs = CONNAISSANCE_ROOT / resume_rel
    resume_abs.parent.mkdir(parents=True, exist_ok=True)
    resume_abs.write_text(content, encoding="utf-8")

    # Déduire source_type
    fm_type = (fm.get("type") or "").lower()
    if fm_type == "courriel":
        source_type = "courriel"
    elif fm_type == "fil":
        source_type = "courriel"
    elif fm_type == "note":
        source_type = "note"
    else:
        source_type = "document"

    db.register_file(
        str(resume_rel),
        "resume",
        source_type=source_type,
        source_path=str(source_rel),
    )
    db.log("connaissance", "resume",
           source_type=source_type,
           source_path=str(source_rel),
           dest_path=str(resume_rel),
           details={"custom_id": custom_id})

    return {
        "path": str(resume_rel),
        "file_type": "resume",
        "source_type": source_type,
        "frontmatter_injected": True,
    }
