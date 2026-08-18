FROM node:20-alpine

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

COPY index.mjs ./

# Persist the WhatsApp companion-device credentials here (mount a volume).
ENV WA_AUTH_DIR=/data/auth
VOLUME ["/data"]

EXPOSE 8788
CMD ["node", "index.mjs"]
