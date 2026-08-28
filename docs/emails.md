# Courriels : scoring & filtrage

Les courriels sont la source la plus bruyante. Plutôt qu'un import brutal, le
système **score** chaque courriel selon plusieurs signaux et ne capture que ce
qui dépasse un seuil. Tout est gouverné par un fichier de config éditable et
calibrable.

## Le scoring

[`core/filtres.py`](../src/connaissance/core/filtres.py) → `score_courriel(msg)`
retourne `(score, reasons)`. Chaque signal ajoute/retire des points et laisse
une trace lisible dans `reasons` (utile pour le debug et le calibrage).

Signaux (poids configurables, valeurs par défaut indicatives) :

| Signal | Effet | Exemple de raison |
|---|---|---|
| Domaine réseau social | − | `réseau social (facebookmail.com)` |
| Domaine marketing | − | `domaine marketing (…)` |
| Newsletter (`List-Unsubscribe`) | − | `newsletter (List-Unsubscribe)` |
| HTML-only (pas de texte) | − | `HTML-only` |
| Expéditeur `noreply` | − | `noreply` |
| Sujet promotionnel | − | `sujet promo` |
| Domaine personnel (le sien) | + | `domaine personnel` |
| Courriel envoyé (par soi) | + | `envoyé` |
| Sujet actionnable | + | `sujet actionnable` |
| Pièce jointe document | + | `PJ document` |

Au-delà des domaines, la config porte des **patterns** (regex) pour les sujets
marketing/actionnables/promotionnels, le corps actionnable, les expéditeurs
génériques/noreply, les suffixes gouvernementaux, etc.

## Les seuils

```yaml
seuils:
  capturer: 0      # score >= 0  → capturer
  ignorer: -1      # score <= -1 → ignorer
```

Entre les deux (ici la bande `]-1, 0[`, vide par défaut) = zone grise. Plus
l'écart `capturer − ignorer` est large, plus on laisse de courriels en
suspens ; plus il est serré, plus la décision est binaire.

## Prêter le scoring à une boîte vivante — `emails score`

`score_courriel()` est une **fonction pure** sur un dict : elle ne lit aucun
mbox. Ses appelants habituels (`calibrate`, `senders`) la nourrissent depuis les
archives ; `emails_cleanup` la nourrit depuis des transcriptions. `emails score`
la prête à un appelant externe :

```bash
connaissance emails score --messages '[{"id":"a","from":"info@members.netflix.com","subject":"Promo"}]'
connaissance emails score --messages-file /tmp/msgs.json
```

Rend `{seuils, repartition, results:[{id, score, decision, reasons}], sans_corps}`,
`decision` valant `capturer` / `revue` / `ignorer` selon les seuils configurés.

L'usage visé est le skill `capture` du plugin **minimaliste**, qui lit la boîte
**vivante** par MCP et ne doit jamais lire les mbox — celles-ci arrivent après
le backup, trop tard pour agir. Le skill n'a donc aucune liste à dupliquer :
la config reste la source de vérité unique, et `reasons` rend chaque écart
auditable — puis corrigeable par un atome typé de `config scoring-set`.

Deux pièges, tous deux dans la forme des entrées :

- **`from` attend une adresse nue.** C'est ce que `_parse_message` y met, via
  `email.utils.parseaddr`. Un en-tête « Nom \<adresse\> » laisserait un `>`
  collé au domaine et **tous** les signaux de domaine disparaîtraient en
  silence. `emails score` normalise les quatre formes qu'un client MCP peut
  rendre (adresse, en-tête, `{name, email}`, liste d'un élément) — mais un
  appelant direct de `score_courriel()` doit s'en charger.
- **Les signaux de corps exigent le vrai corps.** Un aperçu de 200 caractères
  paraît toujours « quasi vide » et ne contient jamais de pied de page
  d'infolettre : `corps_quasi_vide`, `corps_substantiel` et `newsletter_corps`
  mentent tous. `sans_corps` compte les messages concernés. D'où le flux en
  deux passes : expéditeur et sujet sur le lot entier, puis relecture des corps
  pour la seule bande étroite entre `ignorer` et `capturer`.

## La config : `scoring-courriels.yaml`

Sous `~/Connaissance/.config/`, modèle packagé dans
[`config/scoring-courriels.yaml`](../src/connaissance/config/scoring-courriels.yaml).
Sections principales :

- `seuils` / `poids` — seuils de décision et poids par signal.
- `seuils_numeriques` — tailles (corps substantiel, preview…).
- `domaines_personnels` / `_reseaux_sociaux` / `_marketing` — listes
  d'expéditeurs classés.
- `patterns_*` — regex par catégorie de signal.
- `dossiers_ignores` / `dossiers_envoyes` — mapping des dossiers mbox.
- `types_pieces_jointes` — extensions considérées « document ».

### On ne réécrit JAMAIS ce YAML à la main depuis le code

Les mutations passent par des **atomes typés** (groupe `config`), jamais par du
YAML composé par l'appelant :

```bash
connaissance config scoring-show
connaissance config scoring-set --add-domain-marketing exemple.fr,autre.org --dry-run
connaissance config scoring-set --add-pattern-marketing '^community@buddyboss\.com$' --dry-run
connaissance config scoring-diff       # ce que l'atome changerait
connaissance config scoring-validate   # cohérence du fichier
```

Les flags de domaines prennent une liste **séparée par des virgules** (un
flag répété ne garde que la dernière valeur). `--add-pattern-marketing` est
une regex sur l'**adresse** d'expéditeur (`patterns_marketing`, −1) : pour
pénaliser un émetteur promotionnel d'un domaine qu'on ne peut pas mettre en
liste marketing parce qu'il envoie aussi du légitime (`community@buddyboss.com`
contre les tickets `support@buddyboss.com`). `domaines_marketing` est un match
exact du domaine, pesé `adresse_marketing` (−5 depuis le 2026-08-24 : un
domaine en liste marketing doit l'emporter sur « sujet actionnable » +3, sinon
les hameçonnages « your invoice / payment approved » passent).

`ruamel.yaml` préserve les commentaires utilisateur lors de l'écriture. Voir le
contrat #3 dans [architecture.md](architecture.md).

## Gouvernance : 3 usages

Pilotés par le skill cowork `calibrer-courriels`, mais disponibles en CLI :

1. **Calibrer le scoring** — `emails calibrate --sample N` score un échantillon
   et sort un rapport : captures suspectes (score juste au-dessus du seuil) et
   ignorés suspects (juste en dessous), pour ajuster seuils et poids.
2. **Valider les expéditeurs** — `emails senders` analyse les expéditeurs
   borderline et propose whitelist/blacklist.
3. **Nettoyer rétroactivement** — `emails cleanup-obsolete` retire de la base
   les courriels qui ne passeraient plus les règles actuelles (après un
   resserrage du scoring).

## Extraction

`emails extract --since … [--dry-run]` lit les archives mbox, applique le
scoring, capture les courriels retenus comme transcriptions, et regroupe les
fils (`emails threads`). `emails stats` donne la volumétrie. Les archives mbox
elles-mêmes sont maintenues par le skill `archiver-courriels` (backup IMAP
incrémental), hors de ce CLI.

> Les transcriptions de courriels marketing peuvent être allégées (suppression
> des lignes de tracking/unsubscribe/boilerplate) pour réduire de 30–50 % leur
> taille — voir le nettoyage des lignes dans `commands/emails.py`.
