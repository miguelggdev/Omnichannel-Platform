#!/usr/bin/env bash
#
# Prueba de restauracion: baja el ultimo backup, lo restaura en una base
# desechable y comprueba que el contenido sirve.
#
# Un backup que nunca se restaura no es un backup. Esta prueba corre el dia 1
# de cada mes desde Celery Beat (`app.tasks.bulk_run_restore_test`).
#
# Variables de entorno requeridas:
#   S3_BUCKET                    bucket de donde bajar el backup
#   RESTORE_PGHOST, RESTORE_PGPORT, RESTORE_PGUSER, RESTORE_PGPASSWORD
# Opcionales:
#   S3_ENDPOINT     endpoint alternativo (MinIO)
#   RESTORE_DIR     directorio de trabajo (default /tmp/restore_test)
#
# IMPORTANTE — por que RESTORE_* y no PG*:
# La restauracion crea y destruye una base entera. Apuntarla al mismo servidor
# de produccion exige CREATE DATABASE, que el rol de la aplicacion no tiene (ni
# debe tener), y en Supabase Cloud directamente no esta disponible. La prueba va
# contra un PostgreSQL desechable —el contenedor de CI, o uno del host de
# backups— y por eso lleva variables propias: que nadie pueda apuntarla a
# produccion por descuido heredando las PG* del entorno.

set -euo pipefail

RESTORE_DIR="${RESTORE_DIR:-/tmp/restore_test}"
S3_ENDPOINT="${S3_ENDPOINT:-}"
TEMP_DB="restore_test_$(date -u +%s)"

log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"
}

aws_s3() {
  if [ -n "$S3_ENDPOINT" ]; then
    aws s3 "$@" --endpoint-url "$S3_ENDPOINT"
  else
    aws s3 "$@"
  fi
}

# ─── Comprobaciones previas ─────────────────────────────────────────────────
for var in S3_BUCKET RESTORE_PGHOST RESTORE_PGPORT RESTORE_PGUSER; do
  if [ -z "${!var:-}" ]; then
    log "ERROR: falta la variable de entorno ${var}"
    exit 1
  fi
done

for binario in pg_restore psql createdb dropdb aws; do
  if ! command -v "$binario" >/dev/null 2>&1; then
    log "ERROR: '${binario}' no esta instalado"
    exit 1
  fi
done

export PGPASSWORD="${RESTORE_PGPASSWORD:-}"

psql_temp() {
  psql -h "$RESTORE_PGHOST" -p "$RESTORE_PGPORT" -U "$RESTORE_PGUSER" \
    -d "$TEMP_DB" -t -A -c "$1"
}

# La base temporal se borra pase lo que pase: si el script muere a mitad, dejar
# una base huerfana por mes acabaria llenando el disco del servidor.
limpiar() {
  local codigo=$?
  if [ -n "${DB_CREADA:-}" ]; then
    log "Limpiando base temporal ${TEMP_DB}..."
    dropdb -h "$RESTORE_PGHOST" -p "$RESTORE_PGPORT" -U "$RESTORE_PGUSER" \
      --if-exists "$TEMP_DB" || log "AVISO: no se pudo borrar ${TEMP_DB}"
  fi
  rm -rf "$RESTORE_DIR"
  exit "$codigo"
}
trap limpiar EXIT

# ─── Descargar el ultimo backup ─────────────────────────────────────────────
mkdir -p "$RESTORE_DIR"

# El nombre lleva la fecha en formato ISO, asi que el orden alfabetico coincide
# con el cronologico.
ULTIMO="$(aws_s3 ls "s3://${S3_BUCKET}/daily/" | awk '{print $4}' | grep -v '^$' | sort | tail -1)"

if [ -z "$ULTIMO" ]; then
  log "ERROR: no hay backups en s3://${S3_BUCKET}/daily/"
  exit 1
fi

log "Ultimo backup: ${ULTIMO}"
aws_s3 cp "s3://${S3_BUCKET}/daily/${ULTIMO}" "${RESTORE_DIR}/${ULTIMO}"

# ─── Restaurar ──────────────────────────────────────────────────────────────
log "Creando base temporal ${TEMP_DB} en ${RESTORE_PGHOST}..."
createdb -h "$RESTORE_PGHOST" -p "$RESTORE_PGPORT" -U "$RESTORE_PGUSER" "$TEMP_DB"
DB_CREADA=1

log "Restaurando..."
# --no-owner porque el rol dueno en produccion no existe en la base desechable.
# --exit-on-error: sin el, pg_restore avisa de los fallos y termina con 0, y la
# prueba daria por bueno un backup a medias.
pg_restore \
  -h "$RESTORE_PGHOST" \
  -p "$RESTORE_PGPORT" \
  -U "$RESTORE_PGUSER" \
  -d "$TEMP_DB" \
  --no-owner \
  --exit-on-error \
  "${RESTORE_DIR}/${ULTIMO}"

# ─── Verificar ──────────────────────────────────────────────────────────────
log "Verificando contenido restaurado..."

FALLOS=0

# 1. Las tablas del nucleo existen y se pueden consultar.
for tabla in clients users contacts conversations messages audit_logs; do
  if ! filas="$(psql_temp "SELECT count(*) FROM ${tabla};" 2>/dev/null)"; then
    log "  FALLO: la tabla ${tabla} no existe o no se puede leer"
    FALLOS=$((FALLOS + 1))
    continue
  fi
  log "  ${tabla}: ${filas} filas"
done

# 2. Las politicas de RLS viajaron en el dump. Un backup que restaura los datos
#    pero pierde el aislamiento entre tenants no sirve para recuperarse: la
#    primera consulta de la aplicacion devolveria datos de todos los clientes.
POLITICAS="$(psql_temp "SELECT count(*) FROM pg_policies WHERE schemaname = 'public';")"
log "  Politicas RLS: ${POLITICAS}"
if [ "${POLITICAS:-0}" -lt 18 ]; then
  log "  FALLO: se esperaban al menos 18 politicas RLS, hay ${POLITICAS}"
  FALLOS=$((FALLOS + 1))
fi

# 3. El trigger de auditoria tambien.
TRIGGERS="$(psql_temp "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'audit_%' AND NOT tgisinternal;")"
log "  Triggers de auditoria: ${TRIGGERS}"
if [ "${TRIGGERS:-0}" -lt 1 ]; then
  log "  FALLO: no se restauro ningun trigger de auditoria"
  FALLOS=$((FALLOS + 1))
fi

# 4. Alembic quedo en una revision conocida.
REVISION="$(psql_temp "SELECT version_num FROM alembic_version;" 2>/dev/null || echo '')"
log "  Revision de Alembic: ${REVISION:-<ninguna>}"
if [ -z "$REVISION" ]; then
  log "  FALLO: la base restaurada no tiene tabla alembic_version"
  FALLOS=$((FALLOS + 1))
fi

if [ "$FALLOS" -gt 0 ]; then
  log "Restore test FALLIDO: ${FALLOS} comprobacion(es) no pasaron."
  exit 1
fi

log "Restore test EXITOSO (backup ${ULTIMO})."
