# Contract Files — Interface-First Development

> Los contratos definen las interfaces entre Dev A y Dev B.
> Dev A produce los archivos con implementación; Dev B los consume.

## Cómo funciona

1. **Dev A** crea el modelo/schema y commitea
2. **Dev B** importa el modelo/schema y lo usa en middleware/API/tests
3. Si Dev A rompe una interfaz, el smoke test (`scripts/smoke-test.sh`) falla

## Archivos de contrato por sprint

Cada sprint define qué archivos son "contratos" — interfaces compartidas
entre ambos devs. Estos archivos están listados abajo y en cada
`contracts/sprint-XX.json`.

## Reglas

- Un contrato se considera **estable** cuando está en `develop`
- Dev A puede cambiar un contrato solo si:
  1. Avisa en el issue del sprint
  2. Actualiza el contrato aquí
  3. Dev B confirma que no rompe su código
- Los smoke tests validan importabilidad automáticamente
