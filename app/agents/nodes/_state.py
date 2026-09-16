"""Re-export de `ConversationState` para los nodos del grafo.

`app/agents/state.py` (Dev A) ya esta en `main`: este modulo dejo de copiar el
contrato y solo lo reexporta, para no tocar los `import` de cada nodo.
"""

from app.agents.state import ConversationState

__all__ = ["ConversationState"]
