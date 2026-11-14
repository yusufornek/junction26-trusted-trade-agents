FROM node:20-slim AS build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
# Explicit copies keep local dependencies, generated files and secrets out.
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src/ ./src/
# Favicons and the share image: Vite copies public/ into dist as-is.
COPY frontend/public/ ./public/
# Optional one-click demo sign-in; empty leaves the button out of the bundle.
ARG VITE_DEMO_OPERATOR_TOKEN=""
ENV VITE_DEMO_OPERATOR_TOKEN=$VITE_DEMO_OPERATOR_TOKEN
RUN npm run build

FROM caddy:2
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/frontend/dist/ /srv/
EXPOSE 80 443
