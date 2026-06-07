## Discipline d'entité (règles communes pré-classement / classement final)

Ces règles sont identiques au pré-classement (signaux) et au classement final
(transcription OCR complète) : une même pièce doit résoudre la MÊME entité dans
les deux passes, sinon elle est rangée deux fois à des endroits différents.

- **Normalise le nom contre la liste d'« entités connues » fournie** : si le
  document correspond à l'une d'elles même approximativement, réutilise SON nom
  exact (ex. « BNC », « Banque nationale du Canada » → « Banque Nationale »).
  Sinon, propose un nom propre concis et cohérent. Jamais un type de document
  comme entité (« Relevé », « Facture » ne sont PAS des entités).
- **Ne force PAS un sigle vers une entité connue qui n'a que le sigle en commun.**
  En particulier : **« BNC » = Banque Nationale (du Canada)**, à NE PAS confondre
  avec **« BDC » = Banque de développement du Canada** (organisme distinct). Dans
  le doute sur un sigle, garde le nom tel qu'il apparaît plutôt que de l'aligner
  à tort.
- Un **document de travail** (livrable, présentation, note de projet, client de
  mission) n'est PAS émis par une banque : ne lui attribue pas une entité
  bancaire au prétexte d'un sigle. Si l'émetteur réel est un client/employeur,
  c'est lui l'entité ; sinon `entity_type=divers`.
- Un document **au sujet d'une personne mais émis par un organisme** (diplôme
  délivré par une université, relevé d'un assureur) prend l'**organisme** comme
  entité (ex. diplôme McGill → entité « McGill », `entity_type=organismes` —
  McGill est une université, pas une personne).
- **L'entité doit être NOMMÉE dans le document lui-même** (en-tête, logo,
  coordonnées, signature, ou clairement dans le texte). Si l'émetteur n'est PAS
  explicitement nommé — document de travail générique, matériel de formation,
  note interne, gabarit, exercice — tu DOIS mettre **`entity_type=divers`**, MÊME
  si le dossier d'origine, le sujet ou le « contexte professionnel » suggèrent une
  entreprise. **N'assigne JAMAIS une entité « par défaut », « par contexte » ou au
  hasard** : le contenu du dossier n'est pas une preuve d'émetteur. Si le mot
  « défaut » ou « contexte » te vient pour justifier l'entité, la bonne réponse
  est `divers`. Mieux vaut `divers` qu'un faux rattachement.
- **Le slug d'entité conserve les accents** (`Revenu Québec` → `revenu-québec`,
  `Ville de Montréal` → `ville-de-montréal`) — pas de translittération é→e. Il
  sera de toute façon recalculé depuis le nom à l'organisation : garde-le
  cohérent avec le nom.
