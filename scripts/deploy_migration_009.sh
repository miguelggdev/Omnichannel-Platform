#!/usr/bin/env bash
#
# Despliegue de la migracion 009 (indice ciego por tenant): parar, migrar, verificar,
# arrancar. Automatiza el runbook de ADR-052 (MEMORY.md).
#
# POR QUE ES UNA VENTANA Y NO UN DESPLIEGUE NORMAL
# El codigo nuevo busca por HMAC(clave, "{client_id}:{valor}"). Con los hashes viejos
# aun en la tabla no encuentra a nadie y cada mensaje entrante crea un contacto
# duplicado. El codigo viejo, si sigue corriendo despues de migrar, escribe hashes con
# la formula global. No pueden convivir con la migracion: hay que parar API y workers.
#
# USO
#   bash scripts/deploy_migration_009.sh              # SOLO comprueba (no toca nada)
#   bash scripts/deploy_migration_009.sh --execute --backup-verificado
#
# Sin --execute solo corre los chequeos previos, que son de solo lectura: se puede (y
# se debe) ejecutar antes de la ventana para descubrir un problema con tiempo.
# --execute PARA los servicios. Exige ademas --backup-verificado: una confirmacion
# explicita de que hay un backup fresco y de que `scripts/restore_test.sh` funciona
# sobre el.
#
# ENTORNO (el mismo que usa Alembic)
#   DATABASE_URL_DIRECT   conexion directa a Postgres (no el pooler)
#   ENCRYPTION_KEY        la MISMA clave que cifro la columna en la migracion 008;
#                         con otra, pgp_sym_decrypt falla y la transaccion se revierte
#
# SERVICIOS que se paran y arrancan (docker-compose.yml). Ajustalos con SERVICIOS si tu
# despliegue usa otros nombres o un orquestador distinto:
#   SERVICIOS="api celery-webhooks ..." bash scripts/deploy_migration_009.sh --execute ...

set -euo pipefail

REVISION_ANTERIOR="008_encrypt_contact_identifiers"
REVISION_OBJETIVO="009_blind_index_per_tenant"
SERVICIOS="${SERVICIOS:-api celery-webhooks celery-ai celery-documents celery-notifications celery-bulk celery-lead-enrichment celery-beat}"
COMPOSE="${COMPOSE:-docker compose}"

EJECUTAR=0
BACKUP_VERIFICADO=0
for arg in "$@"; do
    case "$arg" in
        --execute) EJECUTAR=1 ;;
        --backup-verificado) BACKUP_VERIFICADO=1 ;;
        -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
        *) echo "Argumento desconocido: $arg" >&2; exit 2 ;;
    esac
done

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
fallo() { echo "ERROR: $*" >&2; exit 1; }

cd "$(dirname "$0")/.."

# ── 1. Chequeos previos (solo lectura) ───────────────────────────────────────
log "Chequeos previos"
[ -n "${DATABASE_URL_DIRECT:-}" ] || fallo "falta DATABASE_URL_DIRECT (conexion directa, no el pooler)"
[ -n "${ENCRYPTION_KEY:-}" ] || fallo "falta ENCRYPTION_KEY (la misma que cifro la columna en la 008)"
[ "${#ENCRYPTION_KEY}" -ge 32 ] || fallo "ENCRYPTION_KEY tiene menos de 32 caracteres"
command -v alembic >/dev/null || fallo "no se encuentra 'alembic' (activa el entorno virtual)"
command -v python >/dev/null || fallo "no se encuentra 'python'"

ACTUAL="$(alembic -c migrations/alembic.ini current 2>/dev/null | awk '{print $1}' | tail -n1 || true)"
log "Revision actual de la base: ${ACTUAL:-desconocida}"
if [ "$ACTUAL" = "$REVISION_OBJETIVO" ]; then
    log "La base ya esta en $REVISION_OBJETIVO; solo se verifica."
    python scripts/verify_migration_009.py
    exit $?
fi
[ "$ACTUAL" = "$REVISION_ANTERIOR" ] || fallo "se esperaba la revision $REVISION_ANTERIOR y hay '${ACTUAL:-ninguna}'. Aplica primero las migraciones anteriores."

if [ "$EJECUTAR" -eq 0 ]; then
    echo
    log "Chequeos previos OK. No se ha tocado nada."
    echo "Para ejecutar (PARA los servicios: $SERVICIOS):"
    echo "  bash scripts/deploy_migration_009.sh --execute --backup-verificado"
    exit 0
fi

[ "$BACKUP_VERIFICADO" -eq 1 ] || fallo "--execute exige --backup-verificado: confirma que hay un backup fresco y que scripts/restore_test.sh funciona sobre el"

# ── 2. Parar, migrar, verificar, arrancar ────────────────────────────────────
log "Parando servicios: $SERVICIOS"
# shellcheck disable=SC2086
$COMPOSE stop $SERVICIOS

arrancar() {
    log "Arrancando servicios"
    # shellcheck disable=SC2086
    $COMPOSE up -d $SERVICIOS
}

log "Aplicando $REVISION_OBJETIVO"
if ! alembic -c migrations/alembic.ini upgrade "$REVISION_OBJETIVO"; then
    echo
    echo "La migracion FALLO y se revirtio entera (es transaccional): la base sigue en $REVISION_ANTERIOR." >&2
    echo "Causa probable: ENCRYPTION_KEY distinta de la que cifro la columna." >&2
    echo "Los servicios siguen PARADOS. Corrige y reintenta, o arranca la version anterior:" >&2
    echo "  $COMPOSE up -d $SERVICIOS" >&2
    exit 1
fi

log "Verificando"
if ! python scripts/verify_migration_009.py; then
    echo
    echo "La verificacion FALLO: los servicios siguen PARADOS. NO los arranques." >&2
    echo "Reversa: alembic -c migrations/alembic.ini downgrade $REVISION_ANTERIOR (tambien necesita ENCRYPTION_KEY)" >&2
    echo "y despliega el codigo anterior, en el mismo orden parar/migrar/arrancar." >&2
    exit 1
fi

arrancar
log "Migracion $REVISION_OBJETIVO desplegada y verificada."
