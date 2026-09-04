#!/usr/bin/env bash
# =============================================================================
# PRE-FLIGHT CHECK — Harness Engineering
# =============================================================================
# Ejecutar al inicio de cada sesión de Claude para validar el entorno.
# Uso: bash scripts/preflight.sh [--dev-a|--dev-b]
#
# Exit codes:
#   0 = Todo OK, listo para trabajar
#   1 = Faltan archivos críticos o hay inconsistencias
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ERRORS=0
WARNINGS=0

pass()  { echo -e "  ${GREEN}✓${NC} $1"; }
fail()  { echo -e "  ${RED}✗${NC} $1"; ERRORS=$((ERRORS + 1)); }
warn()  { echo -e "  ${YELLOW}!${NC} $1"; WARNINGS=$((WARNINGS + 1)); }
info()  { echo -e "  ${CYAN}→${NC} $1"; }

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        PRE-FLIGHT CHECK — Harness Engineering        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ─── 1. Archivos de gobierno ─────────────────────────────────────────────────
echo "1. Archivos de gobierno"

for f in CLAUDE.md METHODOLOGY.md PROGRESS.md MEMORY.md; do
    if [ -f "$f" ]; then
        pass "$f existe ($(wc -l < "$f") líneas)"
    else
        fail "$f NO encontrado — archivo crítico"
    fi
done
echo ""

# ─── 2. Sprint specs ────────────────────────────────────────────────────────
echo "2. Sprint specs"

SPEC_COUNT=$(find specs/ -name "sprint-*.md" 2>/dev/null | wc -l)
if [ "$SPEC_COUNT" -eq 14 ]; then
    pass "14 specs encontrados en specs/"
else
    fail "Se esperaban 14 specs, se encontraron $SPEC_COUNT"
fi

# Verificar sprint activo en PROGRESS.md
ACTIVE_SPRINT=$(grep -oP 'Sprint Activo:\s*Sprint\s+\K\d+' PROGRESS.md 2>/dev/null || echo "?")
if [ "$ACTIVE_SPRINT" != "?" ]; then
    SPEC_FILE="specs/sprint-$(printf '%02d' "$ACTIVE_SPRINT")-*.md"
    if ls $SPEC_FILE 1>/dev/null 2>&1; then
        pass "Spec del sprint activo ($ACTIVE_SPRINT) encontrado"
    else
        fail "Sprint activo es $ACTIVE_SPRINT pero no existe specs/sprint-$(printf '%02d' "$ACTIVE_SPRINT")-*.md"
    fi
else
    warn "No se pudo detectar el sprint activo de PROGRESS.md"
fi
echo ""

# ─── 3. Estructura del proyecto ──────────────────────────────────────────────
echo "3. Estructura del proyecto"

REQUIRED_DIRS=(
    "app/core"
    "app/models"
    "app/schemas"
    "app/services/messaging"
    "app/api/v1"
    "app/api/internal"
    "app/middleware"
    "app/tasks"
    "app/agents/nodes"
    "app/agents/tools"
    "tests/unit"
    "tests/integration"
    "tests/e2e"
    "supabase/init"
    "migrations/versions"
)

MISSING_DIRS=0
for d in "${REQUIRED_DIRS[@]}"; do
    if [ ! -d "$d" ]; then
        MISSING_DIRS=$((MISSING_DIRS + 1))
    fi
done

if [ "$MISSING_DIRS" -eq 0 ]; then
    pass "Todos los ${#REQUIRED_DIRS[@]} directorios requeridos existen"
else
    fail "$MISSING_DIRS directorios requeridos faltantes"
fi
echo ""

# ─── 4. Configuración ───────────────────────────────────────────────────────
echo "4. Configuración"

if [ -f ".env" ]; then
    pass ".env existe"
    # Verificar variables críticas (sin revelar valores)
    for var in DATABASE_URL REDIS_URL JWT_SECRET OPENAI_API_KEY; do
        if grep -q "^${var}=" .env 2>/dev/null; then
            pass "  $var configurada"
        else
            warn "  $var no encontrada en .env"
        fi
    done
elif [ -f ".env.example" ]; then
    warn ".env no existe (usar .env.example como base)"
else
    fail "Ni .env ni .env.example encontrados"
fi

if [ -f "requirements.txt" ]; then
    pass "requirements.txt existe"
else
    fail "requirements.txt no encontrado"
fi
echo ""

# ─── 5. Git status ──────────────────────────────────────────────────────────
echo "5. Git"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    BRANCH=$(git branch --show-current 2>/dev/null || echo "detached")
    pass "Repositorio git inicializado (branch: $BRANCH)"

    DIRTY=$(git status --porcelain 2>/dev/null | wc -l)
    if [ "$DIRTY" -gt 0 ]; then
        warn "$DIRTY archivos sin commitear"
    else
        pass "Working tree limpio"
    fi

    if git remote get-url origin >/dev/null 2>&1; then
        pass "Remote 'origin' configurado"
    else
        warn "No hay remote 'origin' configurado"
    fi

    # Verificar hooks
    if [ -f ".git/hooks/pre-commit" ] && [ -x ".git/hooks/pre-commit" ]; then
        pass "Pre-commit hook instalado"
    else
        warn "Pre-commit hook no instalado (ejecutar: scripts/install-hooks.sh)"
    fi
else
    warn "Git no inicializado — ejecutar: git init"
fi
echo ""

# ─── 6. Herramientas de desarrollo ──────────────────────────────────────────
echo "6. Herramientas"

for tool in python3 pip ruff pytest mypy; do
    if command -v "$tool" >/dev/null 2>&1; then
        VERSION=$($tool --version 2>&1 | head -1)
        pass "$tool: $VERSION"
    else
        warn "$tool no encontrado en PATH"
    fi
done
echo ""

# ─── 7. PROGRESS.md — Tareas pendientes ─────────────────────────────────────
echo "7. Estado del sprint"

if [ -f "PROGRESS.md" ]; then
    PENDING=$(grep -c '^\- \[ \]' PROGRESS.md 2>/dev/null || echo "0")
    DONE=$(grep -c '^\- \[x\]' PROGRESS.md 2>/dev/null || echo "0")
    info "Sprint $ACTIVE_SPRINT: $DONE completadas, $PENDING pendientes"
fi
echo ""

# ─── 8. Dev Role Check ──────────────────────────────────────────────────────
DEV_ROLE="${1:-}"
if [ -n "$DEV_ROLE" ]; then
    echo "8. Validación de rol: $DEV_ROLE"
    case "$DEV_ROLE" in
        --dev-a)
            info "Dev A (Foundations): DDL, models, schemas, core, infra"
            info "Archivos a NO tocar: app/middleware/, app/api/, app/tasks/, tests/"
            ;;
        --dev-b)
            info "Dev B (Integration): middleware, API, services, tasks, tests"
            info "Archivos a NO tocar: app/models/, app/schemas/, app/core/, supabase/"
            ;;
        *)
            warn "Rol no reconocido. Usar: --dev-a o --dev-b"
            ;;
    esac
    echo ""
fi

# ─── Resultado ───────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════"
if [ "$ERRORS" -gt 0 ]; then
    echo -e "${RED}RESULTADO: $ERRORS errores, $WARNINGS advertencias${NC}"
    echo -e "${RED}Corregir los errores antes de empezar a codificar.${NC}"
    exit 1
elif [ "$WARNINGS" -gt 0 ]; then
    echo -e "${YELLOW}RESULTADO: 0 errores, $WARNINGS advertencias${NC}"
    echo -e "${YELLOW}Puedes continuar, pero revisa las advertencias.${NC}"
    exit 0
else
    echo -e "${GREEN}RESULTADO: Todo OK — listo para trabajar${NC}"
    exit 0
fi
