# Digiseller Local Admin

Local web admin for Digiseller/GGSEL operations. API keys are read from `.env`; do not commit real keys.

## Install

```bash
python3 -m pip install --user httpx certifi
```

## Configure

Create `.env` next to `digiseller_admin.py`:

```bash
DIGISELLER_SELLER_ID=1437041
DIGISELLER_API_KEY=PUT_YOUR_WEBMONEY_API_KEY_HERE
DIGISELLER_ADMIN_HOST=127.0.0.1
DIGISELLER_ADMIN_PORT=8765
DIGISELLER_KEEP_ONLINE=1
DIGISELLER_KEEP_ONLINE_INTERVAL=15
DIGISELLER_ONLINE_VALUE=1
DIGISELLER_ONLINE_VERIFY_TYPE=seller
DIGISELLER_PUBLIC_SELLER_URL=https://plati.market/seller/hello1989/1437041/?lang=en-US
DIGISELLER_CHAT_KEEPALIVE_URL=https://chat.digiseller.com/asp/messenger.asp?mode=s
DIGISELLER_CHAT_OPEN_BROWSER=1
DIGISELLER_COMMON_PHRASE_PUBLIC_BASE_URL=
GGSEL_API_KEY=PUT_YOUR_GGSEL_API_KEY_HERE
GGSEL_API_BASE=https://seller.ggsel.com/api_sellers/api
GGSEL_SELLER_ID=132809753
GGSEL_PARTNER_ID=
GGSEL_KEEP_ONLINE=1
GGSEL_ONLINE_VALUE=1
GGSEL_ONLINE_VERIFY_TYPE=seller
FUNPAY_GOLDEN_KEY=PUT_YOUR_FUNPAY_GOLDEN_KEY_COOKIE_HERE
```

If any API key was pasted into chat or exposed publicly, rotate it in the provider dashboard before saving it in `.env`.

## Run

```bash
python3 digiseller_admin.py
```

Open:

```text
http://127.0.0.1:8765
```

## Main pages

- Dashboard: API login and keepalive status.
- Sales: recent orders, product, amount, partner ID, referer.
- Messages / Chats: buyer conversations and reply editor.
- Unread / Admin: unread buyer/admin messages.
- Product / Stock: product details and stock upload tools.
- GGSEL: GGSEL catalog/seller products, searchable by product ID or name.

## GGSEL integration

`/ggsel` reads `GGSEL_API_KEY` from `.env` and never stores it in code. Optional settings:

- `GGSEL_SELLER_ID`: filter the catalog to one seller when needed.
- `GGSEL_SELLER_COOKIE`: optional seller-office login cookie, required only for the chat button that sends and removes one GGSEL stock item.
- `GGSEL_PARTNER_ID`: generate product links with `ai` when needed.
- `GGSEL_API_BASE`: defaults to `https://seller.ggsel.com/api_sellers/api`.
- `GGSEL_KEEP_ONLINE`: keep the GGSEL seller chat online with `setonlinesetting` and heartbeat APIs.
- `GGSEL_ONLINE_VALUE` / `GGSEL_ONLINE_VERIFY_TYPE`: override the GGSEL online setting value and verification corr type.

The page also exposes `/api/ggsel-products?page=1&count=50&q=` JSON for wiring GGSEL data into other admin modules.

## FunPay chat integration

`/chats` also reads `FUNPAY_GOLDEN_KEY` from `.env` and lists recent FunPay conversations alongside Digiseller/GGSEL chats. FunPay replies are sent through the logged-in web session; image attachments and the stock replenishment button use the same reply editor flow as Plati/Digiseller orders.

## Alerts and keepalive

After opening the web UI, click `Enable alerts` so browser sound/voice notifications are allowed.

Online keepalive calls `setonlinesetting` plus chat heartbeat APIs every 15 seconds, then verifies buyer-visible status with `getonlinestatus` and the public seller page. It also keeps GGSEL online when `GGSEL_API_KEY`/`GGSEL_SELLER_ID` are configured. Disable Digiseller with `DIGISELLER_KEEP_ONLINE=0` or GGSEL with `GGSEL_KEEP_ONLINE=0`.

If the API token cannot set chat online status, the app opens the seller chat keepalive URL as a top-level browser window on startup. Set `DIGISELLER_CHAT_OPEN_BROWSER=0` to disable, or use the top-bar button to reopen the chat window.

For terminal-only unread monitoring:

```bash
python3 digiseller_admin.py watch --interval 15
```

## Image and phrase previews

- Order chat attachments show image previews when possible.
- Admin messages render the latest 20 messages by default; use `/admin-messages?limit=50` for more.
- Common phrase file previews are served from `/phrase-files/`. If files are hosted publicly, set `DIGISELLER_COMMON_PHRASE_PUBLIC_BASE_URL=https://your-domain/path`.

## Reply editor

- `/chats` includes a reply editor under the selected buyer conversation.
- Text replies are sent through Digiseller `/debates/v2/`.
- Multiple images, attachments, and reference files are preuploaded through `/debates/v2/upload-preview` and sent with the reply.
- Buyer messages are translated to Chinese when possible; Chinese replies are translated to the buyer's recent language while protecting account strings, emails, URLs, and access codes.
