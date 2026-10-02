# syntax=docker/dockerfile:1.7
# Next.js UI (apps/web) as a standalone Node server; non-root, no dev dependencies at runtime.
# Default build = same-origin proxy mode (/backend/* → FPL_API_INTERNAL_URL with FPL_API_KEY at
# runtime). Passing NEXT_PUBLIC_API_BASE instead makes the browser call the API directly.

FROM node:22-alpine AS deps
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --ignore-scripts

FROM node:22-alpine AS build
WORKDIR /web
ARG NEXT_PUBLIC_API_BASE=
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /web/node_modules node_modules
COPY apps/web ./
RUN npm run typecheck && npm run build

FROM node:22-alpine AS runtime
WORKDIR /web
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /web/.next/standalone ./
COPY --from=build --chown=app:app /web/.next/static ./.next/static
USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["node", "-e", "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
