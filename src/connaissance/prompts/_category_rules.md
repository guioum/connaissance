## Catégorie — valeurs autorisées et règles (communes pré / final)

Choisir EXACTEMENT une valeur de cette liste — le **DOMAINE** du document, pas
son type (« Facture », « Relevé » ne sont PAS des catégories) :

| Valeur | Contenu |
|---|---|
| `achats` | achats en ligne, commandes, livraisons |
| `assurances` | assurance habitation, auto, vie, voyage |
| `banque` | relevés, virements, prêts, **placements, épargne** |
| `emplois` | contrats, paie, relations professionnelles, **formation, bourse** |
| `impots` | déclarations, avis, feuillets fiscaux |
| `juridique` | contrats légaux, notaire, procurations |
| `logement` | loyer, copropriété, déménagement, rénovations |
| `sante` | médical, dentaire, pharmacie, assurance maladie |
| `telecom` | téléphone, internet, câble |
| `transport` | auto, transports en commun, voyages |
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
| 6 | Contrat de travail / paie / emploi / **formation** | `emplois` |
| 7 | Contrat légal / notaire | `juridique` |
| 8 | Médecin / pharmacie / hôpital | `sante` |
| 9 | Loyer / copropriété / rénovation | `logement` |
| 10 | Voiture / vol / transport | `transport` |
| 11 | Abonnement / service récurrent | `abonnements` |
| 12 | Commande / livraison en ligne | `achats` |
| 13 | Rien ne matche | `divers` |

Précisions (ne fais PAS de `abonnements` un fourre-tout) :

- `abonnements` = services **RÉCURRENTS** facturés périodiquement (streaming,
  hébergement web, logiciel SaaS, adhésion à renouvellement).
- Un **placement / épargne** (REER, CELI, fonds) → `banque`.
- Une **bourse** ou une **formation** → `emplois`.
- Une **inscription ponctuelle** (sport, activité, événement) → `achats` ou `divers`.
