#!/usr/bin/env bash
# =============================================================================
# healthcheck.sh — Verifica que los servicios de docker-compose.yml esten sanos
# =============================================================================
# Uso: ./scripts/healthcheck.sh [-t TIMEOUT] [-q] [-s SERVICIO] [-h]
# Ejemplo: ./scripts/healthcheck.sh -t 120
#
# Verifica los 12 servicios de docker-compose.yml (Sprint 2): traefik, api,
# redis, celery-webhooks, celery-ai, celery-documents, celery-notifications,
# celery-bulk, celery-lead-enrichment, celery-beat, prometheus, grafana.
#
# Criterio de aceptacion (specs/sprint-02-docker.md): todos los servicios
# alcanzan estado "healthy" en menos de 2 minutos. El timeout por defecto
# (120s) refleja ese criterio.
#
# NOTA: no se referencian supabase-db/supabase-auth/supabase-storage/
# supabase-realtime/pgbouncer — ADR-020 (MEMORY.md): el proyecto usa
# Supabase Cloud, esos contenedores no existen en este stack.
# =============================================================================

# No usamos "set -e": el bucle de reintentos evalua comandos que pueden
# fallar (docker compose ps, redis-cli) de forma intencional y los maneja
# explicitamente. "-u" y "pipefail" si aplican, para detectar variables sin
# definir y fallos en tuberias.
set -uo pipefail

HEALTHCHECK_cmdname="${0##*/}"

echoerr() {
    if [[ "${HEALTHCHECK_QUIET}" -ne 1 ]]; then
        echo "$@" 1>&2
    fi
}

out() {
    if [[ "${HEALTHCHECK_QUIET}" -ne 1 ]]; then
        echo "$@"
    fi
}

usage() {
    cat << USAGE >&2
Uso:
    $HEALTHCHECK_cmdname [-t TIMEOUT] [-q] [-s SERVICIO] [-h]
    -t TIMEOUT   Segundos a esperar a que todos los servicios esten sanos
                 (default 120, coherente con el criterio de <2 min).
                 0 = una sola pasada, sin esperar.
    -q           Modo silencioso: no imprime nada, solo el exit code.
    -s SERVICIO  Limita la verificacion a un solo servicio.
    -h           Muestra esta ayuda.

Exit codes:
    0 = todos los servicios sanos
    1 = timeout alcanzado con algun servicio no sano
    2 = error de uso (flags invalidos)
    3 = docker/docker compose no disponible
USAGE
    exit 2
}

# ─── Los 12 servicios de docker-compose.yml (Sprint 2) ─────────────────────
ALL_SERVICES=(
    traefik
    api
    redis
    celery-webhooks
    celery-ai
    celery-documents
    celery-notifications
    celery-bulk
    celery-lead-enrichment
    celery-beat
    prometheus
    grafana
)

# celery-beat es un scheduler (celery beat): no expone endpoint ni tiene
# healthcheck definido en docker-compose.yml. Para ese servicio exigimos
# solo estado "running", no "healthy".
NO_HEALTHCHECK_SERVICES=(
    celery-beat
)

is_no_healthcheck() {
    local svc="$1" s
    for s in "${NO_HEALTHCHECK_SERVICES[@]}"; do
        [[ "$s" == "$svc" ]] && return 0
    done
    return 1
}

is_known_service() {
    local svc="$1" s
    for s in "${ALL_SERVICES[@]}"; do
        [[ "$s" == "$svc" ]] && return 0
    done
    return 1
}

# ─── Parse arguments ────────────────────────────────────────────────────────
HEALTHCHECK_TIMEOUT=120
HEALTHCHECK_QUIET=0
HEALTHCHECK_SERVICE_FILTER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t)
            HEALTHCHECK_TIMEOUT="$2"
            shift 2
            ;;
        -q)
            HEALTHCHECK_QUIET=1
            shift 1
            ;;
        -s)
            HEALTHCHECK_SERVICE_FILTER="$2"
            shift 2
            ;;
        -h)
            usage
            ;;
        *)
            echoerr "Argumento desconocido: $1"
            usage
            ;;
    esac
done

if ! [[ "${HEALTHCHECK_TIMEOUT}" =~ ^[0-9]+$ ]]; then
    echoerr "Error: TIMEOUT debe ser un entero >= 0 (recibido: '${HEALTHCHECK_TIMEOUT}')"
    usage
fi

if [[ -n "${HEALTHCHECK_SERVICE_FILTER}" ]] && ! is_known_service "${HEALTHCHECK_SERVICE_FILTER}"; then
    echoerr "Error: servicio desconocido '${HEALTHCHECK_SERVICE_FILTER}'."
    echoerr "Servicios validos: ${ALL_SERVICES[*]}"
    usage
fi

if [[ -n "${HEALTHCHECK_SERVICE_FILTER}" ]]; then
    TARGET_SERVICES=("${HEALTHCHECK_SERVICE_FILTER}")
else
    TARGET_SERVICES=("${ALL_SERVICES[@]}")
fi

# ─── Verificar disponibilidad de docker / docker compose / python3 ─────────
if ! command -v docker >/dev/null 2>&1; then
    echoerr "Error: docker no esta instalado o no esta en el PATH."
    exit 3
fi

if ! docker info >/dev/null 2>&1; then
    echoerr "Error: el daemon de Docker no responde (¿esta Docker corriendo?)."
    exit 3
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
else
    echoerr "Error: no se encontro 'docker compose' ni 'docker-compose'."
    exit 3
fi

# El parseo de "docker compose ps --format json" se hace con python3 (ya usado
# en scripts/smoke-test.sh de este repo) porque el formato puede variar entre
# un array JSON y JSON Lines segun la version de docker compose.
if ! command -v python3 >/dev/null 2>&1; then
    echoerr "Error: python3 es requerido para parsear la salida de 'docker compose ps'."
    exit 3
fi

# ─── Script de parseo (JSON array o JSON Lines -> "servicio|estado|salud") ──
read -r -d '' HEALTHCHECK_PARSE_PY << 'PY_EOF' || true
import json
import sys


def emit(item):
    service = item.get("Service") or item.get("Name") or ""
    state = item.get("State", "")
    health = item.get("Health", "")
    if service:
        print(f"{service}|{state}|{health}")


raw = sys.stdin.read().strip()
if not raw:
    sys.exit(0)

try:
    parsed = json.loads(raw)
    items = parsed if isinstance(parsed, list) else [parsed]
    for entry in items:
        emit(entry)
except json.JSONDecodeError:
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            emit(json.loads(line))
        except json.JSONDecodeError:
            continue
PY_EOF

# ─── Verificacion extra opcional de Redis (no afecta el resultado final) ───
# No se hardcodea la contrasena: se toma de REDIS_PASSWORD en el entorno o,
# si no esta definida, de la linea REDIS_PASSWORD= en .env. Si no se
# encuentra en ninguno de los dos lugares, se omite con un aviso.
check_redis_auth() {
    local pw="${REDIS_PASSWORD:-}"

    if [[ -z "$pw" && -f .env ]]; then
        pw="$(grep -E '^REDIS_PASSWORD=' .env | head -n 1 | cut -d '=' -f2-)"
    fi

    if [[ -z "$pw" ]]; then
        out "     (redis) aviso: REDIS_PASSWORD no disponible en el entorno ni en .env;"
        out "             se omite la verificacion de autenticacion."
        return 0
    fi

    if "${COMPOSE_CMD[@]}" exec -T redis redis-cli -a "$pw" --no-auth-warning ping 2>/dev/null | grep -q "PONG"; then
        out "     (redis) autenticacion verificada (PONG)."
    else
        out "     (redis) aviso: no se pudo verificar autenticacion con REDIS_PASSWORD"
        out "             (no afecta el resultado de salud del contenedor)."
    fi
}

# ─── Bucle principal de verificacion ────────────────────────────────────────
HEALTHCHECK_start_ts=$(date +%s)

while :; do
    declare -A HC_TOTAL=()
    declare -A HC_RUNNING=()
    declare -A HC_HEALTHY=()

    HEALTHCHECK_raw="$("${COMPOSE_CMD[@]}" ps --all --format json 2>/dev/null)"

    if [[ -n "${HEALTHCHECK_raw}" ]]; then
        while IFS='|' read -r svc state health; do
            [[ -z "$svc" ]] && continue
            HC_TOTAL[$svc]=$(( ${HC_TOTAL[$svc]:-0} + 1 ))
            [[ "$state" == "running" ]] && HC_RUNNING[$svc]=$(( ${HC_RUNNING[$svc]:-0} + 1 ))
            [[ "$health" == "healthy" ]] && HC_HEALTHY[$svc]=$(( ${HC_HEALTHY[$svc]:-0} + 1 ))
        done < <(printf '%s' "${HEALTHCHECK_raw}" | python3 -c "${HEALTHCHECK_PARSE_PY}")
    fi

    HEALTHCHECK_now_ts=$(date +%s)
    HEALTHCHECK_elapsed=$(( HEALTHCHECK_now_ts - HEALTHCHECK_start_ts ))

    HEALTHCHECK_all_ok=1
    for svc in "${TARGET_SERVICES[@]}"; do
        total=${HC_TOTAL[$svc]:-0}
        running=${HC_RUNNING[$svc]:-0}
        healthy=${HC_HEALTHY[$svc]:-0}

        if is_no_healthcheck "$svc"; then
            [[ $total -gt 0 && $running -eq $total ]] || HEALTHCHECK_all_ok=0
        else
            [[ $total -gt 0 && $healthy -eq $total ]] || HEALTHCHECK_all_ok=0
        fi
    done

    if [[ "${HEALTHCHECK_all_ok}" -eq 1 ]]; then
        HEALTHCHECK_will_retry=0
    elif [[ "${HEALTHCHECK_TIMEOUT}" -eq 0 ]]; then
        HEALTHCHECK_will_retry=0
    elif [[ "${HEALTHCHECK_elapsed}" -ge "${HEALTHCHECK_TIMEOUT}" ]]; then
        HEALTHCHECK_will_retry=0
    else
        HEALTHCHECK_will_retry=1
    fi

    # ─── Imprimir tabla (interina si se reintentara, final si no) ─────────
    if [[ "${HEALTHCHECK_will_retry}" -eq 1 ]]; then
        out ""
        out "Esperando servicios sanos... (${HEALTHCHECK_elapsed}s / ${HEALTHCHECK_TIMEOUT}s)"
    fi

    out ""
    printf_line() { out "$(printf '  %-24s %-10s %s' "$1" "$2" "$3")"; }
    printf_line "SERVICIO" "ESTADO" "DETALLE"

    HEALTHCHECK_ok_count=0
    for svc in "${TARGET_SERVICES[@]}"; do
        total=${HC_TOTAL[$svc]:-0}
        running=${HC_RUNNING[$svc]:-0}
        healthy=${HC_HEALTHY[$svc]:-0}

        if is_no_healthcheck "$svc"; then
            if [[ $total -gt 0 && $running -eq $total ]]; then
                estado="OK"
                detalle="running ${running}/${total} (sin healthcheck: scheduler)"
                HEALTHCHECK_ok_count=$(( HEALTHCHECK_ok_count + 1 ))
            elif [[ "${HEALTHCHECK_will_retry}" -eq 1 ]]; then
                estado="ESPERANDO"
                detalle="running ${running}/${total}"
            else
                estado="FALLO"
                if [[ $total -eq 0 ]]; then
                    detalle="contenedor no encontrado"
                else
                    detalle="running ${running}/${total} (sin healthcheck: scheduler)"
                fi
            fi
        else
            if [[ $total -gt 0 && $healthy -eq $total ]]; then
                estado="OK"
                detalle="healthy ${healthy}/${total}"
                HEALTHCHECK_ok_count=$(( HEALTHCHECK_ok_count + 1 ))
            elif [[ "${HEALTHCHECK_will_retry}" -eq 1 ]]; then
                estado="ESPERANDO"
                detalle="healthy ${healthy}/${total}, running ${running}/${total}"
            else
                estado="FALLO"
                if [[ $total -eq 0 ]]; then
                    detalle="contenedor no encontrado"
                else
                    detalle="healthy ${healthy}/${total}, running ${running}/${total}"
                fi
            fi
        fi

        printf_line "$svc" "$estado" "$detalle"

        if [[ "$svc" == "redis" && "$estado" == "OK" && "${HEALTHCHECK_will_retry}" -eq 0 ]]; then
            check_redis_auth
        fi
    done

    if [[ "${HEALTHCHECK_will_retry}" -eq 0 ]]; then
        out ""
        out "Resumen: ${HEALTHCHECK_ok_count}/${#TARGET_SERVICES[@]} servicios sanos (${HEALTHCHECK_elapsed}s transcurridos)"
        break
    fi

    sleep 5
done

if [[ "${HEALTHCHECK_all_ok}" -eq 1 ]]; then
    exit 0
else
    exit 1
fi
