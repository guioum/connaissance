"""Détection de secrets (clés, mots de passe, jetons) — zéro dépendance.

But : repérer les fichiers de ``~/Documents`` qui contiennent des identifiants
sensibles, pour les **mettre en quarantaine** — ne jamais les classer en clair,
ne jamais les indexer (qmd) ni les envoyer à un service externe (OCR Mistral,
Batch API). C'est un garde-fou de sécurité du chantier de réorganisation.

Ce N'EST PAS de la détection de PII (noms, emails, adresses) : c'est du
**secret scanning**, volontairement léger — patterns ciblés + entropie de
Shannon — fidèle au core (stdlib uniquement). Presidio (PII, lourd : spaCy +
modèle NLP) a été écarté : mauvais outil pour des clés/mots de passe.

Deux signaux complémentaires, en lecture seule :

  1. **Nom/chemin sensible** (``credentials.csv``, ``.env``, ``id_rsa``,
     ``*.pem``, ``*.pfx``, ``*.kdbx``…) : suspect même sans lire le fichier.
  2. **Contenu** : jetons à préfixe connu (AWS ``AKIA``, GitHub ``ghp_``,
     OpenAI/Anthropic ``sk-``, Google ``AIza``, Slack ``xox…``, Stripe…),
     blocs PEM ``BEGIN … PRIVATE KEY``, JWT, URLs ``user:pass@host``, et
     affectations ``password = …`` / ``api_key = …`` dont la valeur n'est pas
     un placeholder et a une entropie élevée. Plus un signal **tabulaire**
     (en-tête CSV contenant ``password`` / ``mot de passe`` / ``secret`` …) qui
     attrape le cas ``credentials.csv``.

Toute correspondance est **caviardée** dans la sortie : jamais le secret en
clair dans un rapport JSON.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Literal, TypedDict

Severity = Literal["high", "medium"]


class SecretFinding(TypedDict):
    kind: str        # type de détection (nom du pattern)
    severity: Severity
    line: int        # 1-based ; 0 = signal au niveau fichier (nom, en-tête)
    evidence: str    # toujours caviardé


# --- Jetons à préfixe connu (haute confiance) -------------------------------
# Chaque entrée : (nom, regex). Ancrés sur des frontières pour limiter le bruit.
_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_block",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack_webhook",
     re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("google_oauth", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}\b")),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("basic_auth_url",
     re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]{3,}@[^/\s]+", re.I)),
    # Cloud / SaaS supplémentaires (recall, haute précision : formats spécifiques)
    ("azure_storage_key", re.compile(r"AccountKey=[A-Za-z0-9+/=]{80,}")),
    ("azure_connection_string",
     re.compile(r"DefaultEndpointsProtocol=https?;AccountName=[A-Za-z0-9]+;AccountKey=")),
    ("azure_sas_token", re.compile(r"\bsig=[A-Za-z0-9%/+]{43,}")),
    ("twilio_api_key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("twilio_account_sid", re.compile(r"\bAC[0-9a-fA-F]{32}\b")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b")),
    ("mailchimp_key", re.compile(r"\b[0-9a-f]{32}-us[0-9]{1,2}\b")),
    ("square_oauth", re.compile(r"\bsq0(?:csp|atp)-[0-9A-Za-z_-]{22,}\b")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi_token", re.compile(r"\bpypi-AgEIcHlwaS[A-Za-z0-9_-]{50,}\b")),
    ("slack_app_token", re.compile(r"\bxapp-[0-9]-[A-Za-z0-9-]{10,}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("discord_bot_token",
     re.compile(r"\b[MN][A-Za-z0-9_-]{23}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}\b")),
    ("gcp_service_account", re.compile(r'"type"\s*:\s*"service_account"')),
    ("putty_private_key", re.compile(r"PuTTY-User-Key-File-\d")),
    ("rfc1918_password_in_xml",
     re.compile(r"<(?:password|secret|api[_-]?key)>[^<\s]{6,}</")),
]

# --- Détecteur d'ENTROPIE, gated par CONTEXTE (recall sans le bruit) --------
# Une chaîne Base64/Hex à fort aléa n'est flaguée QUE si un mot-clé secret est
# présent sur la même ligne. Sans ce garde-fou, l'entropie libre sur un corpus
# personnel (notes markdown, pages web, lockfiles, hashes) est massivement
# bruyante. Avec : on attrape les clés opaques sans préfixe connu (ex.
# `apikey: a9F…`, `Authorization: Bearer …`) tout en restant précis.
_B64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,120}")
_HEX_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{32,128}\b")
_ENTROPY_B64_MIN = 4.5
_ENTROPY_HEX_MIN = 3.2

# Vocabulaire de contexte (frontières de mots). Volontairement sans « token »
# ni « key » seuls (trop fréquents en prose) ; on garde les formes composées.
_SECRET_CONTEXT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|mot\s+de\s+passe|secret|secret[_-]?key|"
    r"api[_-]?key|apikey|access[_-]?key|client[_-]?secret|auth[_-]?token|"
    r"bearer|credentials?|passphrase|private[_-]?key)\b"
)


def _scan_entropy(line: str, lineno: int,
                  severity: Severity = "high") -> list[SecretFinding]:
    """Chaînes à forte entropie d'une ligne (Base64/Hex). Findings caviardés.

    À appeler seulement quand la ligne porte un mot-clé secret (gating amont).
    Évite quand même le markup, les data-URI et les lignes très longues.
    """
    if len(line) > 400 or ("<" in line and ">" in line):
        return []
    if "data:" in line and "base64" in line:
        return []
    out: list[SecretFinding] = []
    seen: set[str] = set()
    for rx, kind, thr, minlen in (
        (_HEX_TOKEN_RE, "hex", _ENTROPY_HEX_MIN, 32),
        (_B64_TOKEN_RE, "base64", _ENTROPY_B64_MIN, 24),
    ):
        for m in rx.finditer(line):
            tok = m.group(0).strip("=_-")
            if len(tok) < minlen or tok in seen:
                continue
            if kind == "base64" and not (
                    re.search(r"[A-Za-z]", tok) and re.search(r"[0-9]", tok)):
                continue   # un mot ou un long nombre, pas une clé
            if shannon_entropy(tok) < thr:
                continue
            seen.add(tok)
            out.append({"kind": f"high_entropy_{kind}", "severity": severity,
                        "line": lineno, "evidence": redact(tok)})
            if len(out) >= 3:
                return out
    return out

# --- Affectations « mot-clé = valeur » (confiance moyenne, gated entropie) ---
_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(password|passwd|mot[\s_-]?de[\s_-]?passe|pwd|secret|secret[_-]?key|
       api[_-]?key|access[_-]?key|auth[_-]?token|token|client[_-]?secret|
       private[_-]?key|passphrase)
    \s*[:=]\s*
    ["']?(?P<val>[^\s"'`]{6,})["']?
    """,
)

# --- En-tête tabulaire (CSV/TSV) listant des mots de passe -------------------
_TABULAR_HEADER_RE = re.compile(
    r"(?i)\b(password|passwd|mot\s+de\s+passe|secret|api[\s_-]?key|"
    r"pwd|cl[eé]\s+(?:priv[eé]e|api)|passphrase)\b"
)

# Valeurs à ignorer dans une affectation : placeholders, refs d'env, gabarits.
_PLACEHOLDER_RE = re.compile(
    r"""(?ix)^(?:
        x{3,}|\*{3,}|\.{3,}|-+|_+|              # masques
        <[^>]+>|\{\{[^}]+\}\}|\$\{[^}]+\}|      # <PLACEHOLDER>, {{ }}, ${ }
        \$[a-z_][a-z0-9_]*|%[a-z_]+%|           # $VAR, %VAR%
        (?:your|my|the|some|test|example|sample|changeme|change_me|
           none|null|true|false|todo|fixme|xxx|yyy|placeholder|redacted|
           hidden|secret|password|email|username|user)
    )$"""
)


def shannon_entropy(s: str) -> float:
    """Entropie de Shannon (bits/caractère) d'une chaîne. 0 si vide."""
    if not s:
        return 0.0
    n = len(s)
    counts = Counter(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def redact(s: str, *, keep: int = 3) -> str:
    """Caviarder un secret : garde au plus ``keep`` premiers caractères.

    Ne laisse jamais filtrer la valeur entière. ``"AKIA12...XY"`` → ``"AKI…"``.
    """
    s = s.strip()
    if len(s) <= keep:
        return "•" * len(s)
    return s[:keep] + "…" + f"[{len(s)} car.]"


def _looks_secretish(value: str) -> bool:
    """Une valeur d'affectation ressemble-t-elle à un vrai secret ?

    Heuristique de précision : assez longue, pas un placeholder, mélange de
    classes de caractères et entropie suffisante (écarte ``password=postgres``
    ou ``token=true`` tout en gardant ``token=a7Fk9…``).
    """
    v = value.strip().strip("\"'`")
    if len(v) < 8 or _PLACEHOLDER_RE.match(v):
        return False
    if len(set(v)) <= 3:                       # "aaaaaaaa", "12121212"
        return False
    if "/" in v and not re.search(r"[0-9]", v):  # chemin, pas un secret
        return False
    classes = sum(bool(re.search(p, v)) for p in
                  (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2 and shannon_entropy(v) >= 3.0


def scan_text(text: str, *, max_findings: int = 50) -> list[SecretFinding]:
    """Détecter les secrets dans un texte. Liste de findings caviardés.

    S'arrête à ``max_findings`` (un fichier truffé de clés n'a pas besoin
    d'être énuméré exhaustivement pour être mis en quarantaine).
    """
    findings: list[SecretFinding] = []
    lines = text.splitlines()

    # Signal tabulaire : un VRAI en-tête CSV/TSV (1res lignes) listant des mots
    # de passe. Garde-fous de précision pour ne PAS se déclencher sur de la prose
    # ou des clippings web (.html) : pas de balise markup, ligne courte, au moins
    # 2 colonnes, et une majorité de colonnes « courtes » (en-tête, pas du texte).
    for i, line in enumerate(lines[:5], start=1):
        if "<" in line or ">" in line or not (0 < len(line) <= 300):
            continue
        if line.count(",") + line.count(";") + line.count("\t") < 1:
            continue
        cols = [c.strip() for c in re.split(r"[,;\t]", line)]
        if len(cols) < 2 or sum(len(c) <= 40 for c in cols) < max(2, len(cols) * 0.6):
            continue
        hits = [c for c in cols if c and len(c) <= 40 and _TABULAR_HEADER_RE.search(c)]
        if hits:
            findings.append({
                "kind": "tabular_password_column",
                "severity": "high",
                "line": i,
                "evidence": "colonne(s) : " + ", ".join(hits[:4]),
            })
            break

    for lineno, line in enumerate(lines, start=1):
        if len(findings) >= max_findings:
            break
        had_token = False
        for kind, rx in _TOKEN_PATTERNS:
            m = rx.search(line)
            if m:
                had_token = True
                findings.append({
                    "kind": kind,
                    "severity": "high",
                    "line": lineno,
                    "evidence": redact(m.group(0)),
                })
        am = _ASSIGNMENT_RE.search(line)
        if am and _looks_secretish(am.group("val")):
            had_token = True
            findings.append({
                "kind": f"assignment:{am.group(1).lower().replace(' ', '_')}",
                "severity": "medium",
                "line": lineno,
                "evidence": redact(am.group("val")),
            })
        # Entropie : seulement si (a) aucun pattern connu n'a déjà capté la
        # ligne et (b) un mot-clé secret y figure (gating de précision).
        if not had_token and _SECRET_CONTEXT_RE.search(line):
            findings.extend(_scan_entropy(line, lineno))

    return findings[:max_findings]


# --- Signal au niveau du NOM de fichier -------------------------------------
# (label, severity). On NE traite PAS `.key` comme une clé : en pratique c'est
# une présentation Keynote dans ~/Documents (faux positif quasi systématique).
_FILENAME_EXACT: dict[str, tuple[str, Severity]] = {
    ".env": ("dotenv", "medium"),
    ".netrc": ("netrc", "high"),
    ".pgpass": ("pgpass", "high"),
    ".htpasswd": ("htpasswd", "high"),
    ".git-credentials": ("git_credentials", "high"),
    "id_rsa": ("ssh_private_key", "high"),
    "id_dsa": ("ssh_private_key", "high"),
    "id_ecdsa": ("ssh_private_key", "high"),
    "id_ed25519": ("ssh_private_key", "high"),
}
_FILENAME_SUFFIX: list[tuple[str, str, Severity]] = [
    (".pem", "pem_key", "high"),
    (".pfx", "pkcs12", "high"),
    (".p12", "pkcs12", "high"),
    (".ppk", "putty_key", "high"),
    (".jks", "java_keystore", "high"),
    (".keystore", "keystore", "high"),
    (".kdbx", "keepass_db", "high"),
    (".kdb", "keepass_db", "high"),
    (".asc", "pgp_armored", "medium"),
    (".gpg", "pgp_encrypted", "medium"),
]
_FILENAME_PATTERN = re.compile(
    r"(?i)(?:^|[\s_./-])(credential|credentials|secret|secrets|password|"
    r"passwords|mot[\s_-]?de[\s_-]?passe|api[\s_-]?key)s?(?:[\s_./-]|$)"
)


def filename_signal(name: str) -> tuple[str, Severity] | None:
    """Le NOM seul indique-t-il un fichier sensible ? (label, severity) ou None."""
    low = name.lower()
    if low in _FILENAME_EXACT:
        return _FILENAME_EXACT[low]
    for suf, label, sev in _FILENAME_SUFFIX:
        if low.endswith(suf):
            return (label, sev)
    if low.startswith("id_rsa") or low.endswith("_rsa") or low.endswith("_ed25519"):
        return ("ssh_private_key", "high")
    if _FILENAME_PATTERN.search(low):
        return ("name_hint", "medium")
    return None


def is_probably_binary(data: bytes) -> bool:
    """Heuristique : présence d'octet NUL dans les premiers Ko = binaire."""
    return b"\x00" in data[:8192]
