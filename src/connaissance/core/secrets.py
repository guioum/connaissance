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
]

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

    # Signal tabulaire : un en-tête (1res lignes) listant des mots de passe.
    for i, line in enumerate(lines[:5], start=1):
        if line.count(",") + line.count(";") + line.count("\t") >= 1:
            cols = re.split(r"[,;\t]", line)
            hits = [c.strip() for c in cols if _TABULAR_HEADER_RE.search(c)]
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
        for kind, rx in _TOKEN_PATTERNS:
            m = rx.search(line)
            if m:
                findings.append({
                    "kind": kind,
                    "severity": "high",
                    "line": lineno,
                    "evidence": redact(m.group(0)),
                })
        am = _ASSIGNMENT_RE.search(line)
        if am and _looks_secretish(am.group("val")):
            findings.append({
                "kind": f"assignment:{am.group(1).lower().replace(' ', '_')}",
                "severity": "medium",
                "line": lineno,
                "evidence": redact(am.group("val")),
            })

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
