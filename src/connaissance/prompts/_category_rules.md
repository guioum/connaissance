## Catégorie — valeurs autorisées et règles (communes pré / final)

Choisir EXACTEMENT une valeur de cette liste — le **DOMAINE** du document, pas
son type (« Facture », « Relevé » ne sont PAS des catégories) :

| Valeur | Contenu |
|---|---|
| `achats` | achats en ligne, commandes, livraisons |
| `assurances` | assurance habitation, auto, vie, voyage |
| `banque` | relevés, virements, prêts, **placements, épargne, finances** |
| `emplois` | **relation d'emploi** : contrat de travail, bulletin de paie, CV, candidature, assurance-emploi, RH |
| `professionnel` | **produit du travail** : présentation, note/procès-verbal de réunion, plan/rapport de projet, modèle, livrable de mission, suivi de temps, stratégie, matériel de formation |
| `impots` | déclarations, avis, feuillets fiscaux |
| `juridique` | contrats légaux, notaire, procurations |
| `logement` | loyer, copropriété, déménagement, rénovations |
| `sante` | médical, dentaire, pharmacie, assurance maladie |
| `telecom` | téléphone, internet, câble |
| `transport` | auto, transports en commun, **voyages** |
| `abonnements` | streaming, magazines, services récurrents |
| `divers` | tout ce qui ne rentre pas dans les autres |

Appliquer les règles dans l'ordre, s'arrêter à la **PREMIÈRE** qui matche :

| Priorité | Condition | Catégorie |
|---|---|---|
| 1 | Facture/relevé/paiement d'une **banque** | `banque` |
| 2 | Facture/relevé/paiement d'un **assureur** | `assurances` |
| 3 | Facture/relevé/paiement d'un **telecom** | `telecom` |
| 4 | Facture/relevé/paiement d'une **autre entreprise** | `achats` |
| 5 | Déclaration/avis/feuillet **fiscal** | `impots` |
| 6 | Contrat de travail, **bulletin de paie**, CV, candidature, assurance-emploi (la RELATION d'emploi) | `emplois` |
| 7 | **Livrable/document de travail** : présentation, note de réunion, plan/rapport de projet, modèle, matériel de formation, suivi de temps (le PRODUIT du travail, pas un contrat/paie) | `professionnel` |
| 8 | Contrat légal / notaire | `juridique` |
| 9 | Médecin / pharmacie / hôpital | `sante` |
| 10 | Loyer / copropriété / rénovation | `logement` |
| 11 | Voiture / vol / voyage / transport | `transport` |
| 12 | Abonnement / service récurrent | `abonnements` |
| 13 | Commande / livraison en ligne | `achats` |
| 14 | Rien ne matche | `divers` |

Précisions importantes :

- **`emplois` vs `professionnel`** : `emplois` = ta relation d'emploi (contrat,
  **paie**, CV, candidature, assurance-emploi, RH). `professionnel` = ce que tu
  **produis/reçois dans une mission** (présentation, compte-rendu, plan de projet,
  livrable, formation, suivi de temps). Un bulletin de paie reste `emplois` ; une
  note de réunion de projet est `professionnel`.
- `abonnements` = services **RÉCURRENTS** seulement (streaming, hébergement, SaaS,
  adhésion). Un **placement/épargne** → `banque` ; une **inscription ponctuelle**
  (sport, activité) → `achats`/`divers`.
- **N'invente JAMAIS une catégorie hors de cette liste.** En particulier :
  `finances` → `banque` ; `voyages` → `transport` ; `cuisine`, `organisation`,
  `projets`, `recettes`, `maison`, `jardin` → utilise `divers` (le DOMAINE) **et
  place le thème dans le champ `sujet`** (ex. `category=divers`, `sujet=cuisine`).
