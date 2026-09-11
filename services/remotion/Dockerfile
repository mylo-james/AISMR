FROM node:22-bookworm-slim

WORKDIR /app

# Install Chromium dependencies for Remotion
RUN apt-get update && apt-get install -y \
  libnss3 \
  libdbus-1-3 \
  libatk1.0-0 \
  libgbm-dev \
  libasound2 \
  libxrandr2 \
  libxkbcommon-dev \
  libxfixes3 \
  libxcomposite1 \
  libxdamage1 \
  libatk-bridge2.0-0 \
  libpango-1.0-0 \
  libcairo2 \
  libcups2 \
  curl \
  ffmpeg \
  && rm -rf /var/lib/apt/lists/*

COPY services/remotion/package.json services/remotion/package-lock.json ./
RUN npm i

COPY services/remotion/tsconfig.json services/remotion/remotion.config.ts services/remotion/render.mjs ./
COPY services/remotion/src ./src
COPY services/remotion/api ./api
COPY services/remotion/templates ./templates
COPY services/remotion/scripts ./scripts
# Immutable renderer-owned assets. The API accepts only the preset ID and
# digest, never an application-selected path or remote month-bank URL.
COPY data/media/voice/months/teacup-whisper-months-v1 ./data/media/voice/months/teacup-whisper-months-v1
COPY data/media/voice/presets/monthly-saved-voice-v2.json ./data/media/voice/presets/monthly-saved-voice-v2.json
COPY data/media/voice/profiles/eleven-v3-whisper-en-v1.json ./data/media/voice/profiles/eleven-v3-whisper-en-v1.json

# Install Chrome Headless Shell for Remotion rendering
RUN npx remotion browser ensure

ENV NODE_ENV=production
ENV PORT=3001

EXPOSE 3001

# Run with tsx for TypeScript support without separate build step
CMD ["npx", "tsx", "api/server.ts"]
