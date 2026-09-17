#!/usr/bin/env bash
#
# Backup de la base a S3 (o MinIO), con retencion.
#
# Se ejecuta a diario desde Celery Beat via `app.tasks.bulk_run_backup`, y
# tambien a mano para verificar la configuracion.
#
# Variables de entorno requeridas:
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE   conexion a PostgreSQL
#   S3_BUCKET                                        bucket destino
# Opcionales:
#   S3_ENDPOINT          endpoint alternativo (MinIO); vacio = AWS S3
#   BACKUP_DIR           directorio de trabajo (default /tmp/backups)
#   BACKUP_KEEP_LOCAL    cuantos backups conservar en disco (default 3)
#   BACKUP_RETAIN_DAILY  dias de retencion de los diarios en S3 (default 30)
#   BACKUP_RETAIN_WEEKLY semanas de retencion de los semanales (default 12)
#
# Nota sobre Supabase Cloud (ADR-020): PGHOST debe ser la conexion DIRECTA, no
# el pooler del puerto 6543. `pg_dump` abre una sesion larga y usa sentencias
# que el pooler en modo transaccion no soporta bien.

set -euo pipefail

# ─── Configuracion ──────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/tmp/backups}"
BACKUP_KEEP_LOCAL="${BACKUP_KEEP_LOCAL:-3}"
BACKUP_RETAIN_DAILY="${BACKUP_RETAIN_DAILY:-30}"
BACKUP_RETAIN_WEEKLY="${BACKUP_RETAIN_WEEKLY:-12}"
S3_ENDPOINT="${S3_ENDPOINT:-}"

DATE="$(date -u +%Y-%m-%d_%H-%M-%S)"
DAY_OF_WEEK="$(date -u +%u)"  # 1=lunes ... 7=domingo
# Extension .dump y no .sql.gz: --format=custom produce un binario comprimido
# que solo lee pg_restore, no un SQL plano. Llamarlo .sql.gz invitaria a que
# alguien intentara `gunzip | psql` y no funcionara.
BACKUP_FILE="backup_${PGDATABASE}_${DATE}.dump"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILE}"

log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"
}

# `aws` con el endpoint solo si esta definido: pasar --endpoint-url "" hace
# fallar al CLI en vez de caer al endpoint por defecto de AWS.
aws_s3() {
  if [ -n "$S3_ENDPOINT" ]; then
    aws s3 "$@" --endpoint-url "$S3_ENDPOINT"
  else
    aws s3 "$@"
  fi
}

# ─── Comprobaciones previas ─────────────────────────────────────────────────
for var in PGHOST PGPORT PGUSER PGDATABASE S3_BUCKET; do
  if [ -z "${!var:-}" ]; then
    log "ERROR: falta la variable de entorno ${var}"
    exit 1
  fi
done

for binario in pg_dump aws; do
  if ! command -v "$binario" >/dev/null 2>&1; then
    log "ERROR: '${binario}' no esta instalado"
    exit 1
  fi
done

mkdir -p "$BACKUP_DIR"

# ─── Dump ───────────────────────────────────────────────────────────────────
log "Iniciando backup de ${PGDATABASE} en ${PGHOST}..."

pg_dump \
  -h "$PGHOST" \
  -p "$PGPORT" \
  -U "$PGUSER" \
  -d "$PGDATABASE" \
  --format=custom \
  --compress=9 \
  --no-owner \
  --file="$BACKUP_PATH"

TAMANO="$(du -h "$BACKUP_PATH" | cut -f1)"
log "Dump local completado: ${BACKUP_FILE} (${TAMANO})"

# Un dump de 0 bytes es un fallo silencioso: pg_dump puede salir con 0 y dejar
# un archivo vacio si el filtro no selecciona nada. Mejor romper aqui que subir
# basura y descubrirlo el dia de la restauracion.
if [ ! -s "$BACKUP_PATH" ]; then
  log "ERROR: el dump quedo vacio"
  exit 1
fi

# ─── Subida ─────────────────────────────────────────────────────────────────
aws_s3 cp "$BACKUP_PATH" "s3://${S3_BUCKET}/daily/${BACKUP_FILE}"
log "Subido a s3://${S3_BUCKET}/daily/${BACKUP_FILE}"

if [ "$DAY_OF_WEEK" -eq 7 ]; then
  aws_s3 cp "$BACKUP_PATH" "s3://${S3_BUCKET}/weekly/${BACKUP_FILE}"
  log "Copia semanal creada."
fi

# ─── Retencion local ────────────────────────────────────────────────────────
# `ls -t | tail -n +N` deja los N-1 mas recientes y borra el resto.
find "$BACKUP_DIR" -maxdepth 1 -name 'backup_*.dump' -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn \
  | tail -n "+$((BACKUP_KEEP_LOCAL + 1))" \
  | cut -d' ' -f2- \
  | xargs -r rm -f
log "Retencion local aplicada (se conservan ${BACKUP_KEEP_LOCAL})."

# ─── Retencion en S3 ────────────────────────────────────────────────────────
# La fecha sale del nombre del archivo, no de la fecha de modificacion del
# objeto: una recopia (por ejemplo al migrar de bucket) cambiaria la segunda y
# resucitaria backups ya vencidos.
limpiar_prefijo() {
  local prefijo="$1"
  local dias="$2"
  local ahora
  ahora="$(date -u +%s)"

  aws_s3 ls "s3://${S3_BUCKET}/${prefijo}/" | awk '{print $4}' | while read -r archivo; do
    [ -n "$archivo" ] || continue
    local fecha
    fecha="$(echo "$archivo" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)"
    [ -n "$fecha" ] || continue

    local edad
    edad=$(( (ahora - $(date -u -d "$fecha" +%s)) / 86400 ))
    if [ "$edad" -gt "$dias" ]; then
      aws_s3 rm "s3://${S3_BUCKET}/${prefijo}/${archivo}"
      log "Eliminado ${prefijo}/${archivo} (${edad} dias)"
    fi
  done
}

limpiar_prefijo daily "$BACKUP_RETAIN_DAILY"
limpiar_prefijo weekly "$((BACKUP_RETAIN_WEEKLY * 7))"

log "Backup completado correctamente."
