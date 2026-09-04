# Loop Failure Report

> Copiar este template a `docs/loop-failures/YYYY-MM-DD-sprint-XX-descripcion.md`

---

## Metadata

| Campo | Valor |
|---|---|
| **Fecha** | YYYY-MM-DD |
| **Sprint** | Sprint XX |
| **Dev** | Dev A / Dev B |
| **Módulo** | `app/path/to/module.py` |
| **Tipo de fallo** | test / lint / runtime / merge / logic |
| **Severidad** | baja / media / alta / crítica |
| **Resuelto** | sí / no |

---

## Descripción del Problema

_¿Qué pasó? ¿Cuál era el comportamiento esperado vs. el observado?_

## Contexto del Loop

- **Iteración del loop:** X de N (ej: intento 2 de 3 en debug loop)
- **Archivos involucrados:**
  - `app/...`
  - `tests/...`
- **Comando que falló:** `pytest tests/unit/test_X.py -v`

## Error Output

```
Pegar el error/traceback relevante aquí
```

## Análisis de Causa Raíz

_¿Por qué ocurrió? ¿Fue un error de lógica, una dependencia faltante, un contrato roto?_

## Resolución

_¿Cómo se resolvió? Si no se resolvió, ¿cuál es el plan?_

```python
# Código relevante del fix (si aplica)
```

## Lecciones Aprendidas

- _¿Qué se debería hacer diferente la próxima vez?_
- _¿Se necesita actualizar MEMORY.md con este patrón?_
- _¿Se necesita un nuevo check en el pre-commit hook?_

## Acciones de Seguimiento

- [ ] Actualizar MEMORY.md
- [ ] Agregar test de regresión
- [ ] Actualizar contrato si aplica
- [ ] Notificar al otro dev si afecta su trabajo
