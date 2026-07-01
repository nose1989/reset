# Mobile Messages Client

An **independent** mobile-first messaging client for the Digiseller admin
backend. It shares **no code** with `digiseller_admin.py` (the PC admin). It only
talks to the backend over HTTP through a small JSON data API and renders its own
mobile UI. Conversations from every source platform are shown uniformly — no
platform-source labels, icons, or names appear anywhere in the UI.

## Architecture

Two independent servers on two different ports:

```
PC admin      digiseller_admin.py            :8765   (pages + /api/m/* JSON API)
Mobile        mobile/serve.py (serves dist)   :8080   (proxies /api,/assets -> :8765)
```

The mobile server serves the pre-built static files and proxies data requests to
the PC backend, so the mobile app is always same-origin (no CORS) yet runs as its
own process on its own port.

Backend endpoints consumed (added to `digiseller_admin.py`, data-only, CORS on):

- `GET  /api/m/conversations`        aggregated conversation list
- `GET  /api/m/messages?platform&id` messages for one conversation
- `POST /api/m/translate`            batch translate buyer messages to Chinese
- `POST /api/m/send`                 send a reply

The `platform` field is used only to route follow-up requests; it is never
displayed.

## Run — two independent ports (recommended)

`dist/` is committed, so **after pulling code you never run a build**. Start the
two servers (each in its own terminal):

```bash
# terminal 1 — PC admin
python3 digiseller_admin.py            # http://127.0.0.1:8765

# terminal 2 — mobile (own port, no npm needed)
python3 mobile/serve.py                # http://127.0.0.1:8080
```

Open **http://127.0.0.1:8080/** (or your phone at `http://<pc-lan-ip>:8080/`).
Configure with env vars:

```bash
MOBILE_PORT=9000 DIGISELLER_ADMIN_ORIGIN=http://127.0.0.1:8765 python3 mobile/serve.py
```

## Develop with hot reload (optional, needs Node)

Only when editing mobile source and you want live reload without committing a
build:

```bash
cd mobile && npm install               # once
npm run dev                            # http://localhost:5173
```

Edits hot-reload automatically. The dev server proxies `/api` and `/assets` to
the backend (`DIGISELLER_ADMIN_ORIGIN` to point elsewhere). After finishing,
rebuild and commit the updated `dist/` so the plain `serve.py` flow stays
current:

```bash
npm run build
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
