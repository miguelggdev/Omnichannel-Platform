#!/usr/bin/env bash
# =============================================================================
# SMOKE TEST — Integration Harness
# =============================================================================
# Valida que los contract files (interfaces entre Dev A y Dev B) sean
# importables y expongan los símbolos esperados.
#
# Uso: bash scripts/smoke-test.sh [sprint-number]
#   Sin argumento: valida todos los contratos disponibles
#   Con argumento:  valida solo el sprint indicado (ej: 3)
#
# Exit codes:
#   0 = Todos los contratos válidos
#   1 = Al menos un contrato roto
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ERRORS=0
CHECKS=0
SKIPPED=0

pass()  { echo -e "  ${GREEN}✓${NC} $1"; CHECKS=$((CHECKS + 1)); }
fail()  { echo -e "  ${RED}✗${NC} $1"; ERRORS=$((ERRORS + 1)); CHECKS=$((CHECKS + 1)); }
skip()  { echo -e "  ${YELLOW}⊘${NC} $1"; SKIPPED=$((SKIPPED + 1)); }
info()  { echo -e "  ${CYAN}→${NC} $1"; }

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        SMOKE TEST — Contract Validation              ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

SPRINT_FILTER="${1:-}"

# Verificar que estamos en la raíz del proyecto
if [ ! -d "contracts" ]; then
    echo -e "${RED}Error: directorio contracts/ no encontrado.${NC}"
    echo "Ejecutar desde la raíz del proyecto."
    exit 1
fi

# Buscar archivos de contrato
if [ -n "$SPRINT_FILTER" ]; then
    CONTRACT_FILES=$(find contracts/ -name "sprint-$(printf '%02d' "$SPRINT_FILTER").json" 2>/dev/null || true)
    if [ -z "$CONTRACT_FILES" ]; then
        echo -e "${YELLOW}No se encontró contrato para Sprint $SPRINT_FILTER${NC}"
        exit 0
    fi
else
    CONTRACT_FILES=$(find contracts/ -name "sprint-*.json" 2>/dev/null | sort || true)
fi

if [ -z "$CONTRACT_FILES" ]; then
    echo -e "${YELLOW}No se encontraron archivos de contrato en contracts/${NC}"
    exit 0
fi

# Verificar que python3 y jq están disponibles
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}Error: python3 no encontrado en PATH${NC}"
    exit 1
fi

# Usar python para parsear JSON si jq no está disponible
parse_json() {
    python3 -c "
import json, sys
data = json.load(open('$1'))
sprint = data.get('sprint', '?')
name = data.get('name', '?')
contracts = data.get('contracts', [])
print(f'SPRINT:{sprint}')
print(f'NAME:{name}')
for c in contracts:
    f = c.get('file', '')
    t = c.get('type', '')
    exports = c.get('exports', [])
    print(f'CONTRACT:{f}|{t}|{\";\".join(exports)}')
"
}

# Iterar por cada archivo de contrato
for contract_file in $CONTRACT_FILES; do
    # Parsear el contrato
    CONTRACT_DATA=$(parse_json "$contract_file" 2>/dev/null || true)

    if [ -z "$CONTRACT_DATA" ]; then
        fail "No se pudo parsear $contract_file"
        continue
    fi

    SPRINT_NUM=$(echo "$CONTRACT_DATA" | grep "^SPRINT:" | cut -d: -f2)
    SPRINT_NAME=$(echo "$CONTRACT_DATA" | grep "^NAME:" | cut -d: -f2-)

    echo "Sprint $SPRINT_NUM: $SPRINT_NAME"

    # Verificar cada contrato
    echo "$CONTRACT_DATA" | grep "^CONTRACT:" | while IFS='|' read -r file_raw type exports_raw; do
        FILE=$(echo "$file_raw" | sed 's/^CONTRACT://')
        EXPORTS="$exports_raw"

        if [ ! -f "$FILE" ]; then
            skip "$FILE (archivo no existe aún)"
            continue
        fi

        case "$type" in
            python-module)
                # Verificar que el módulo es importable
                MODULE_PATH=$(echo "$FILE" | sed 's/\//./g' | sed 's/\.py$//')
                IMPORT_RESULT=$(python3 -c "import $MODULE_PATH" 2>&1 || true)

                if echo "$IMPORT_RESULT" | grep -q "ModuleNotFoundError\|ImportError\|SyntaxError"; then
                    fail "$FILE — no importable: $(echo "$IMPORT_RESULT" | tail -1)"
                else
                    pass "$FILE — importable"

                    # Verificar exports
                    if [ -n "$EXPORTS" ]; then
                        IFS=';' read -ra EXPORT_LIST <<< "$EXPORTS"
                        for exp in "${EXPORT_LIST[@]}"; do
                            exp=$(echo "$exp" | xargs)  # trim whitespace
                            if [ -z "$exp" ]; then continue; fi

                            HAS_EXPORT=$(python3 -c "
import $MODULE_PATH
if hasattr($MODULE_PATH, '$exp'):
    print('OK')
else:
    print('MISSING')
" 2>/dev/null || echo "ERROR")

                            if [ "$HAS_EXPORT" = "OK" ]; then
                                pass "  └─ export: $exp"
                            elif [ "$HAS_EXPORT" = "MISSING" ]; then
                                fail "  └─ export faltante: $exp"
                            else
                                skip "  └─ no se pudo verificar: $exp"
                            fi
                        done
                    fi
                fi
                ;;
            ddl)
                # Para DDL solo verificar que el archivo existe y tiene contenido
                LINES=$(wc -l < "$FILE")
                if [ "$LINES" -gt 10 ]; then
                    pass "$FILE ($LINES líneas)"
                else
                    fail "$FILE — parece vacío o incompleto ($LINES líneas)"
                fi
                ;;
            *)
                skip "$FILE (tipo '$type' no validable automáticamente)"
                ;;
        esac
    done

    echo ""
done

# ─── Resultado ───────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════"
if [ "$ERRORS" -gt 0 ]; then
    echo -e "${RED}RESULTADO: $ERRORS fallos, $((CHECKS - ERRORS)) OK, $SKIPPED omitidos${NC}"
    echo -e "${RED}Hay contratos rotos — verificar con el otro dev.${NC}"
    exit 1
elif [ "$CHECKS" -eq 0 ] && [ "$SKIPPED" -gt 0 ]; then
    echo -e "${YELLOW}RESULTADO: Sin contratos verificables ($SKIPPED omitidos)${NC}"
    echo -e "${YELLOW}Los archivos de contrato aún no existen — normal si el sprint no ha empezado.${NC}"
    exit 0
else
    echo -e "${GREEN}RESULTADO: $CHECKS OK, $SKIPPED omitidos — contratos válidos${NC}"
    exit 0
fi
