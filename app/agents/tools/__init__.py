"""Tools de LangChain que el scheduling agent expone al LLM via `bind_tools()`.

El paquete no reexporta las tools a proposito, mismo criterio que
`app/agents/nodes/__init__.py`: quien las necesita (`app/agents/nodes/scheduling.py`)
importa `SCHEDULING_TOOLS` de su modulo.
"""
