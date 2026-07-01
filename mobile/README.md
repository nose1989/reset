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

## Normal use — one process (recommended)

The backend serves the built mobile app at **`/m`**, so you only run one thing:

```bash
# once (and again whenever mobile code changes)
cd mobile && npm install && npm run build

# then just run the backend as usual
cd .. && python3 digiseller_admin.py
```

Open **http://127.0.0.1:8765/m/** (or your phone at
`http://<pc-lan-ip>:8765/m/`). No second server, no proxy, no CORS.

After you pull new code, rebuild once with `npm run build` and refresh the page.
If you open `/m/` without a build, the backend shows a reminder to run the build.

## Develop with hot reload (optional)

For live editing without rebuilding, run the Vite dev server alongside the
backend:

```bash
# terminal 1
python3 digiseller_admin.py            # http://127.0.0.1:8765

# terminal 2
cd mobile && npm run dev               # http://localhost:5173/m/
```

Edits hot-reload automatically — no need to re-run anything per change. The dev
server proxies `/api` and `/assets` to the backend. Point it at a different
backend with:

```bash
DIGISELLER_ADMIN_ORIGIN=http://127.0.0.1:9000 npm run dev
```

## Cross-origin deployment (optional)

To host the SPA on a different origin than the backend, build with the backend
origin baked in (the backend already sends permissive CORS headers on
`/api/m/*`):

```bash
VITE_API_BASE=https://admin.example.com npm run build
```

## Lint / typecheck

```bash
npm run lint                        # tsc --noEmit
```
