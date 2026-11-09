# Hubline dashboard (Next.js)

See the root [README](../README.md) for the full project. Quick reference:

```bash
npm install
cp .env.example .env.local      # fill DASHBOARD_PASSWORD, AUTH_SECRET, BACKEND_API_TOKEN
npm run dev                     # http://localhost:3000 (backend must run on :8000)

npm run lint && npm run typecheck && npm test && npm run build
npm run e2e                     # Playwright smoke + screenshots (needs docker compose db)
npm run gen:api                 # regenerate lib/api-types.ts from openapi.json
```

Node 26.x (`.nvmrc`, `engines`), the Vercel default runtime. Cache Components is deliberately
off (every page is per-request and live). Only server code talks to the backend
(`lib/backend.ts`, `import "server-only"`); mutations are server actions (`app/actions`).
