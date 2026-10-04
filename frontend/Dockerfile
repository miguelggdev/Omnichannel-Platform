# =============================================================================
# Frontend (Next.js standalone) — build multi-stage
# =============================================================================
# Imagen final: solo el servidor standalone de Next, sin node_modules de desarrollo,
# como usuario sin privilegios.
# =============================================================================

# ─── deps: dependencias exactas del lockfile ─────────────────────────────────
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ─── builder ─────────────────────────────────────────────────────────────────
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# `rewrites()` de next.config.mjs se evalua al COMPILAR: el destino de /api/v1/* queda
# fijado en la imagen. Por eso es un ARG y no una variable de entorno de ejecucion.
ARG API_INTERNAL_URL=http://api:8000
ARG NEXT_PUBLIC_DEFAULT_LOCALE=es
ENV API_INTERNAL_URL=${API_INTERNAL_URL} \
    NEXT_PUBLIC_DEFAULT_LOCALE=${NEXT_PUBLIC_DEFAULT_LOCALE} \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ─── runner ──────────────────────────────────────────────────────────────────
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs

COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:3000/api/health || exit 1

CMD ["node", "server.js"]
