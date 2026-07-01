# Mobile Messages Client

An **independent** mobile-first messaging client for the Digiseller admin
backend. It shares **no code** with `digiseller_admin.py` (the PC admin). It only
talks to the backend over HTTP through a small JSON data API and renders its own
mobile UI. Conversations from every source platform are shown uniformly — no
platform-source labels, icons, or names appear anywhere in the UI.

## Architecture

```
mobile/ (React + Vite SPA)  --HTTP-->  digiseller_admin.py  (/api/m/* JSON API)
```

Backend endpoints consumed (added to `digiseller_admin.py`, data-only, CORS on):

- `GET  /api/m/conversations`        aggregated conversation list
- `GET  /api/m/messages?platform&id` messages for one conversation
- `POST /api/m/translate`            batch translate buyer messages to Chinese
- `POST /api/m/send`                 send a reply

The `platform` field is used only to route follow-up requests; it is never
displayed.

## Develop

Start the backend (PC admin) first:

```bash
cd ..
python3 digiseller_admin.py        # serves the API at http://127.0.0.1:8765
```

Then start the mobile dev server:

```bash
npm install
npm run dev                         # http://localhost:5173
```

The Vite dev server proxies `/api` to the backend, so the browser stays
same-origin (no CORS needed in dev). Point it at a different backend with:

```bash
DIGISELLER_ADMIN_ORIGIN=http://127.0.0.1:9000 npm run dev
```

## Build

```bash
npm run build                       # outputs static files to dist/
npm run preview                     # preview the production build
```

For a cross-origin production deployment (SPA served from a different host than
the backend), set `VITE_API_BASE` at build time to the backend origin; the
backend already sends permissive CORS headers on `/api/m/*`.

```bash
VITE_API_BASE=https://admin.example.com npm run build
```

## Lint / typecheck

```bash
npm run lint                        # tsc --noEmit
```
