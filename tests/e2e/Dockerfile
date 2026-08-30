FROM mcr.microsoft.com/playwright:v1.61.1-noble

WORKDIR /work
COPY package.json package-lock.json ./
RUN npm ci
COPY playwright.config.ts tsconfig.json ./
COPY fixtures ./fixtures
COPY specs ./specs

ENTRYPOINT ["npx", "playwright", "test"]
