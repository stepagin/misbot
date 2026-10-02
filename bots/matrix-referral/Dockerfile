FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
ENV NODE_ENV=production
WORKDIR /app
COPY --chown=10002:10001 package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY --chown=10002:10001 main.js logic.js signing.js ./
COPY --chown=10002:10001 tests ./tests
USER 10002:10001
CMD ["node", "main.js"]
