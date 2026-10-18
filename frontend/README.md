# Svarah.AI — frontend

npm workspace for the website:

| Workspace | Path | What it is |
| --- | --- | --- |
| `@svarah/web` | `apps/web` | React 18 + Vite website and voice workspace. Talks to the FastAPI backend in `../backend`. Deployed on Vercel (`vercel.json`). |

## Run locally

Requirements: Node.js 20+. Start the backend first (see the root
[`README.md`](../README.md)).

```bash
npm install
npm run dev          # web app → http://localhost:5173 (proxies /api to :8000)
npm run build        # production build → apps/web/dist
```

## Web routes

| Route | Screen |
| --- | --- |
| `/` | Landing page |
| `/login`, `/signup` | Sign in with Google or a phone number |
| `/workspace` | Voice-only campaign workspace (also `/dashboard`, `/app`, `/account`) |
| `/studio` | Step-by-step campaign studio for the same campaigns |
| `/web-demo` | Product demo video |
| `/download` | Android app download — serves `apps/web/public/downloads/svarah-ai.apk` when present |
| `/connected` | Return page after linking a social account |
