#!/usr/bin/env bash
# =============================================================================
# wait-for-it.sh — Espera a que un servicio TCP este disponible
# =============================================================================
# Uso: ./scripts/wait-for-it.sh host:port [-t timeout] [-- command args]
# Ejemplo: ./scripts/wait-for-it.sh supabase-db:5432 -t 60 -- echo "DB lista"
# =============================================================================

set -e

WAITFORIT_cmdname="${0##*/}"

echoerr() {
    if [[ "${WAITFORIT_QUIET}" -ne 1 ]]; then
        echo "$@" 1>&2
    fi
}

usage() {
    cat << USAGE >&2
Uso:
    $WAITFORIT_cmdname host:port [-s] [-t timeout] [-- command args]
    -h HOST | --host=HOST       Host o IP a verificar
    -p PORT | --port=PORT       Puerto TCP a verificar
    -s | --strict               Solo ejecutar comando si la espera fue exitosa
    -q | --quiet                No mostrar mensajes de estado
    -t TIMEOUT | --timeout=TIMEOUT
                                Timeout en segundos (0 = sin limite)
    -- COMMAND ARGS             Comando a ejecutar despues de la espera
USAGE
    exit 1
}

wait_for() {
    if [[ "${WAITFORIT_TIMEOUT}" -gt 0 ]]; then
        echoerr "$WAITFORIT_cmdname: esperando $WAITFORIT_HOST:$WAITFORIT_PORT por ${WAITFORIT_TIMEOUT}s..."
    else
        echoerr "$WAITFORIT_cmdname: esperando $WAITFORIT_HOST:$WAITFORIT_PORT sin timeout..."
    fi

    WAITFORIT_start_ts=$(date +%s)
    while :; do
        if [[ "${WAITFORIT_ISBUSY}" -eq 1 ]]; then
            nc -z "$WAITFORIT_HOST" "$WAITFORIT_PORT" >/dev/null 2>&1
            WAITFORIT_result=$?
        else
            (echo -n > "/dev/tcp/${WAITFORIT_HOST}/${WAITFORIT_PORT}") >/dev/null 2>&1
            WAITFORIT_result=$?
        fi

        if [[ "${WAITFORIT_result}" -eq 0 ]]; then
            WAITFORIT_end_ts=$(date +%s)
            echoerr "$WAITFORIT_cmdname: $WAITFORIT_HOST:$WAITFORIT_PORT disponible en $((WAITFORIT_end_ts - WAITFORIT_start_ts))s"
            break
        fi

        WAITFORIT_end_ts=$(date +%s)
        if [[ "${WAITFORIT_TIMEOUT}" -gt 0 ]] && [[ $((WAITFORIT_end_ts - WAITFORIT_start_ts)) -ge "${WAITFORIT_TIMEOUT}" ]]; then
            echoerr "$WAITFORIT_cmdname: timeout esperando $WAITFORIT_HOST:$WAITFORIT_PORT despues de ${WAITFORIT_TIMEOUT}s"
            return 1
        fi

        sleep 1
    done
    return 0
}

wait_for_wrapper() {
    if [[ "${WAITFORIT_QUIET}" -eq 1 ]]; then
        timeout "${WAITFORIT_BUSYTIMEFLAG}" "${WAITFORIT_TIMEOUT}" "$0" \
            --quiet --child --host="$WAITFORIT_HOST" --port="$WAITFORIT_PORT" --timeout="$WAITFORIT_TIMEOUT" &
    else
        timeout "${WAITFORIT_BUSYTIMEFLAG}" "${WAITFORIT_TIMEOUT}" "$0" \
            --child --host="$WAITFORIT_HOST" --port="$WAITFORIT_PORT" --timeout="$WAITFORIT_TIMEOUT" &
    fi
    WAITFORIT_PID=$!
    trap 'kill -INT ${WAITFORIT_PID}' INT
    wait "${WAITFORIT_PID}"
    WAITFORIT_RESULT=$?
    if [[ "${WAITFORIT_RESULT}" -ne 0 ]]; then
        echoerr "$WAITFORIT_cmdname: timeout o error esperando $WAITFORIT_HOST:$WAITFORIT_PORT"
    fi
    return "${WAITFORIT_RESULT}"
}

# ─── Parse arguments ────────────────────────────────────────────────────────
WAITFORIT_TIMEOUT=30
WAITFORIT_STRICT=0
WAITFORIT_CHILD=0
WAITFORIT_QUIET=0

# Detectar si nc soporta -z
if nc -z localhost 0 >/dev/null 2>&1; then
    WAITFORIT_ISBUSY=1
else
    WAITFORIT_ISBUSY=0
fi

# Check para timeout flag
WAITFORIT_BUSYTIMEFLAG=""
if timeout --help 2>&1 | grep -q -- '-t '; then
    WAITFORIT_BUSYTIMEFLAG="-t"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        *:* )
            WAITFORIT_hostport=(${1//:/ })
            WAITFORIT_HOST=${WAITFORIT_hostport[0]}
            WAITFORIT_PORT=${WAITFORIT_hostport[1]}
            shift 1
            ;;
        --child)
            WAITFORIT_CHILD=1
            shift 1
            ;;
        -q | --quiet)
            WAITFORIT_QUIET=1
            shift 1
            ;;
        -s | --strict)
            WAITFORIT_STRICT=1
            shift 1
            ;;
        -h)
            WAITFORIT_HOST="$2"
            shift 2
            ;;
        --host=*)
            WAITFORIT_HOST="${1#*=}"
            shift 1
            ;;
        -p)
            WAITFORIT_PORT="$2"
            shift 2
            ;;
        --port=*)
            WAITFORIT_PORT="${1#*=}"
            shift 1
            ;;
        -t)
            WAITFORIT_TIMEOUT="$2"
            shift 2
            ;;
        --timeout=*)
            WAITFORIT_TIMEOUT="${1#*=}"
            shift 1
            ;;
        --)
            shift
            WAITFORIT_CLI=("$@")
            break
            ;;
        *)
            echoerr "Argumento desconocido: $1"
            usage
            ;;
    esac
done

if [[ -z "$WAITFORIT_HOST" ]] || [[ -z "$WAITFORIT_PORT" ]]; then
    echoerr "Error: host y port son requeridos"
    usage
fi

WAITFORIT_RESULT=0

if [[ "${WAITFORIT_CHILD}" -gt 0 ]]; then
    wait_for
    WAITFORIT_RESULT=$?
else
    if [[ "${WAITFORIT_TIMEOUT}" -gt 0 ]]; then
        wait_for_wrapper
        WAITFORIT_RESULT=$?
    else
        wait_for
        WAITFORIT_RESULT=$?
    fi
fi

if [[ "${WAITFORIT_CLI[*]}" != "" ]]; then
    if [[ "${WAITFORIT_RESULT}" -ne 0 ]] && [[ "${WAITFORIT_STRICT}" -eq 1 ]]; then
        echoerr "$WAITFORIT_cmdname: modo estricto, no se ejecuta el comando"
        exit "${WAITFORIT_RESULT}"
    fi
    exec "${WAITFORIT_CLI[@]}"
else
    exit "${WAITFORIT_RESULT}"
fi
