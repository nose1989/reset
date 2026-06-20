# Digiseller Local Admin

本地 Digiseller Web 管理后台雏形。API Key 只放在本机 `.env`，不要写进代码，不要提交 Git。

## 安装

```bash
python3 -m pip install --user httpx certifi
```

## 配置 API Key

在 `digiseller_admin.py` 同目录创建 `.env`：

```bash
DIGISELLER_SELLER_ID=1437041
DIGISELLER_API_KEY=这里填 WebMoney Keeper 里的完整 API Key
DIGISELLER_ADMIN_HOST=127.0.0.1
DIGISELLER_ADMIN_PORT=8765
```

建议：如果 API Key 曾经发到聊天或公开地方，先在 Digiseller 后台重新生成新 key，再写入 `.env`。

## 启动

```bash
python3 digiseller_admin.py
```

浏览器打开：

```text
http://127.0.0.1:8765
```

## 当前功能

- Dashboard：API 登录测试
- Sales：最近订单，显示订单号、商品、金额、partner_id、referer
- Chats：买家订单聊天列表
- Unread：未读买家聊天和管理员消息
- Chat：查看订单聊天详情
- Download buyer images：下载指定订单买家发来的图片
- Product：查看商品价格、库存、可售状态

## 实时提醒

Web 提醒：启动后台并打开 `http://127.0.0.1:8765` 后，点击右下角 `Enable alerts`。

开启后页面会每 15 秒检查一次未读消息；有新的未读时会：

- 播放提示音
- 中文语音播报
- 浏览器通知弹窗
- 浏览器标题显示未读数

注意：浏览器出于安全限制，必须先手动点一次 `Enable alerts`，声音和语音才会被允许播放。

后台常驻提醒：即使不打开网页，也可以运行：

```bash
python3 digiseller_admin.py watch --interval 15
```

在 macOS 上会用系统声音和 `say` 语音播报。停止用 `Ctrl+C`。

## 图片预览

- 订单聊天附件：显示文件名、打开链接和图片缩略图。
- Admin messages：默认只显示最近 20 条，避免一次渲染太多图片导致页面卡顿。可用 `/admin-messages?limit=50` 查看更多。

## v6 reply editor

- `/chats` now includes a reply editor under the selected buyer conversation.
- The editor sends text replies through Digiseller `/debates/v2/`.
- Multiple images, attachments, and document/reference files are preuploaded through `/debates/v2/upload-preview` and sent with the reply.
- Use `.env` for `DIGISELLER_API_KEY`; do not publish packages containing `.env`.

## v7 auto translation

- Buyer messages on `/chats` are automatically translated to Chinese when possible.
- Each translated buyer message has a button to switch between Chinese and the original text.
- The reply editor detects the buyer's recent language and translates Chinese replies to that language before sending.
- Account strings, emails, URLs, and long access codes are protected during translation so credentials stay unchanged.

## v8 full history loading

- Buyer chat pages now load all available dialog history by paging backward with `old_id`, instead of only the newest 150/200 API messages.
- The selected chat header shows `Messages loaded: N` so you can compare the page with the API result.
