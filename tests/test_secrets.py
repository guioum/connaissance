"""Tests de la détection de secrets (core/secrets.py + commands/secrets.py).

Le cœur (``core.secrets``) est pur et testable sans environnement. La commande
est testée via ``monkeypatch`` de ``DOCUMENTS_DIR`` (comme le triage).
"""
from connaissance.core import secrets as S
from connaissance.commands import secrets as CMD


# --- core : entropie & caviardage ------------------------------------------

def test_entropy_low_for_repetition_high_for_random():
    assert S.shannon_entropy("aaaaaaaa") < 1.0
    assert S.shannon_entropy("aB3$xK9pLm2Q") > 3.0


def test_redact_never_leaks_full_value():
    secret = "AKIAIOSFODNN7EXAMPLE"
    out = S.redact(secret)
    assert secret not in out
    assert out.startswith("AKI")


# --- core : jetons à préfixe connu -----------------------------------------

def test_detects_aws_access_key():
    f = S.scan_text("aws_key = AKIAIOSFODNN7EXAMPLE")
    kinds = {x["kind"] for x in f}
    assert "aws_access_key_id" in kinds
    assert all("AKIAIOSFODNN7EXAMPLE" not in x["evidence"] for x in f)


def test_detects_private_key_block():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n"
    f = S.scan_text(text)
    assert any(x["kind"] == "private_key_block" for x in f)


def test_detects_github_and_anthropic_tokens():
    text = ("token=ghp_" + "a" * 36 + "\n"
            "key=sk-ant-" + "b" * 30)
    kinds = {x["kind"] for x in S.scan_text(text)}
    assert "github_token" in kinds
    assert "anthropic_key" in kinds


def test_basic_auth_url_flagged():
    f = S.scan_text("db=postgres://admin:s3cr3tP4ss@db.example.com:5432/app")
    assert any(x["kind"] == "basic_auth_url" for x in f)


# --- core : affectations (gated entropie) ----------------------------------

def test_assignment_high_entropy_flagged():
    f = S.scan_text('password = "a7Fk9Zx2Qp4Lm8Rt"')
    assert any(x["kind"].startswith("assignment:") for x in f)


def test_camelcase_key_assignment_flagged():
    # Variantes camelCase manquées par api_key/apikey (cas réel : production.md).
    assert any(x["kind"].startswith("assignment:")
               for x in S.scan_text('apiSiteKey: "85b1c0d2e3f4a5b6c7d8e9f0a1b2c3d4"'))
    assert any(x["kind"].startswith("assignment:")
               for x in S.scan_text('clientSecret = "a7Fk9Zx2Qp4Lm8RtB3nW"'))


def test_camelcase_non_secret_key_ignored():
    # sortKey/cacheKey (préfixe non sensible) ou valeur non-secrète → rien.
    assert S.scan_text('sortKey: "name-asc"') == []
    assert S.scan_text('userKey: "name-asc"') == []   # valeur faible entropie


def test_assignment_placeholder_ignored():
    for line in ('password = changeme', 'api_key = <YOUR_KEY>',
                 'token = ${ENV_TOKEN}', 'password = postgres'):
        assert S.scan_text(line) == [], line


# --- core : nouveaux patterns providers ------------------------------------

def test_detects_extra_providers():
    cases = {
        "sendgrid_key": "key=SG." + "a" * 22 + "." + "b" * 43,
        "gcp_service_account": '  "type": "service_account",',
        "twilio_api_key": "SK" + "0" * 32,
        "telegram_bot_token": "12345678:" + "A" * 35,
    }
    for expected, line in cases.items():
        kinds = {x["kind"] for x in S.scan_text(line)}
        assert expected in kinds, (expected, kinds)


# --- core : entropie gated par contexte ------------------------------------

def test_gated_entropy_flags_keyword_plus_opaque_token():
    # « bearer » (mot-clé) + jeton opaque hors syntaxe d'affectation.
    f = S.scan_text("envoie le bearer a7Fk9Zx2Qp4Lm8RtB3nW6vC1hD5jE au serveur")
    assert any(x["kind"].startswith("high_entropy") for x in f)


def test_entropy_ignored_without_keyword():
    # Même jeton, mais en prose sans mot-clé secret → pas de finding (anti-bruit
    # : recettes, README, lockfiles ne doivent pas remonter).
    assert S.scan_text("ma note contient a7Fk9Zx2Qp4Lm8RtB3nW6vC1hD5jE ici") == []


def test_entropy_ignored_for_low_entropy_token():
    assert S.scan_text("password hint aaaaaaaaaaaaaaaaaaaaaaaa here") == []


# --- core : signal tabulaire (cas credentials.csv) -------------------------

def test_tabular_password_header():
    f = S.scan_text("site,login,password\nexample.com,bob,hunter2\n")
    assert any(x["kind"] == "tabular_password_column" for x in f)


# --- core : signal nom de fichier ------------------------------------------

def test_filename_signal_private_key_material():
    assert S.filename_signal("id_rsa")[1] == "high"
    assert S.filename_signal("server.pem")[1] == "high"
    assert S.filename_signal("backup.kdbx")[1] == "high"


def test_keynote_dot_key_not_flagged_as_secret():
    # `.key` = Keynote dans ~/Documents : ne doit PAS être pris pour une clé.
    assert S.filename_signal("Présentation.key") is None


def test_credentials_name_hint():
    sig = S.filename_signal("credentials.csv")
    assert sig is not None and sig[0] == "name_hint"


# --- commande : scan d'un arbre tmp ----------------------------------------

def test_command_flags_content_and_name(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    (root / "Classer").mkdir(parents=True)
    (root / "Classer" / "notes.txt").write_text(
        "mon AKIAIOSFODNN7EXAMPLE perdu ici", encoding="utf-8")
    (root / "Classer" / "id_rsa").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n", encoding="utf-8")
    (root / "Classer" / "facture.pdf").write_bytes(b"%PDF-1.4 binary")

    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    res = CMD.scan()

    rels = {f["rel"] for f in res["files"]}
    assert "Classer/notes.txt" in rels
    assert "Classer/id_rsa" in rels
    assert "Classer/facture.pdf" not in rels   # binaire sans nom suspect
    assert res["flagged"] == 2


def test_command_skips_already_classified_top_dirs(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    (root / "organismes" / "banque").mkdir(parents=True)
    (root / "organismes" / "banque" / "creds.txt").write_text(
        "password = a7Fk9Zx2Qp4Lm8Rt", encoding="utf-8")
    (root / "facture.txt").write_text("rien ici", encoding="utf-8")
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    res = CMD.scan()
    assert res["flagged"] == 0   # le dossier déjà classé est ignoré
