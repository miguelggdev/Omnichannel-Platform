"""Nodos del grafo de conversacion.

Cada modulo expone una unica funcion `*_node(state) -> dict` que LangGraph
registra como nodo. Ningun nodo muta el estado: todos devuelven un dict parcial
con los campos que modifican (PAT-003 de `specs/sprint-06-langgraph.md`).

El paquete no reexporta los nodos a proposito: el grafo (`app/agents/graph.py`,
entrega de Dev A) importa cada uno de su modulo, y asi un fallo de import en un
nodo no tumba a los demas.
"""
