"""connaissance — CLI de la base de connaissances personnelle.

Pipeline : transcrire → résumer → organiser → optimiser → synthétiser.

Le CLI expose 12 groupes de commandes (`documents`, `emails`, `notes`,
`pipeline`, `organize`, `optimize`, `summarize`, `synthesis`, `audit`,
`scope`, `config`, `manifest`) via un dispatcher argparse. Toutes les
sorties sont du JSON typé (voir `connaissance.core.schemas`) sauf si
`--human` est demandé pour debug.
"""
__version__ = "2.1.0"
