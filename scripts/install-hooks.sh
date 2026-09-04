#!/usr/bin/env bash
# =============================================================================
# INSTALL GIT HOOKS — Constraint Harness
# =============================================================================
# Copia los hooks del proyecto a .git/hooks/ y los hace ejecutables.
# Uso: bash scripts/install-hooks.sh
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

HOOKS_SRC="scripts/hooks"
HOOKS_DST=".git/hooks"

# Verificar que estamos en la raíz del repo
if [ ! -d ".git" ]; then
    echo -e "${RED}Error: No se encontró .git/ — ejecutar desde la raíz del repositorio.${NC}"
    exit 1
fi

if [ ! -d "$HOOKS_SRC" ]; then
    echo -e "${RED}Error: No se encontró $HOOKS_SRC/ — verificar estructura del proyecto.${NC}"
    exit 1
fi

echo "Instalando git hooks..."

INSTALLED=0

for hook in "$HOOKS_SRC"/*; do
    HOOK_NAME=$(basename "$hook")

    # Backup si ya existe un hook diferente
    if [ -f "$HOOKS_DST/$HOOK_NAME" ]; then
        if ! diff -q "$hook" "$HOOKS_DST/$HOOK_NAME" >/dev/null 2>&1; then
            BACKUP="$HOOKS_DST/${HOOK_NAME}.backup.$(date +%Y%m%d%H%M%S)"
            cp "$HOOKS_DST/$HOOK_NAME" "$BACKUP"
            echo -e "  ${YELLOW}Backup:${NC} $HOOK_NAME → $(basename "$BACKUP")"
        fi
    fi

    cp "$hook" "$HOOKS_DST/$HOOK_NAME"
    chmod +x "$HOOKS_DST/$HOOK_NAME"
    echo -e "  ${GREEN}✓${NC} $HOOK_NAME instalado"
    INSTALLED=$((INSTALLED + 1))
done

if [ "$INSTALLED" -eq 0 ]; then
    echo -e "${YELLOW}No se encontraron hooks en $HOOKS_SRC/${NC}"
else
    echo -e "${GREEN}$INSTALLED hook(s) instalado(s) correctamente.${NC}"
fi
