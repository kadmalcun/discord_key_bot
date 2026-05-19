# Discord 鍵管理 Bot

部室の鍵の貸し借りを Discord チャンネル上で管理するための Bot です。
ボタン操作で「誰が・いつ・どこで」鍵を借りたかが自動的に記録され、返し忘れには自動でリマインドが飛びます。

> **エンドユーザー向けの使い方は [USAGE.md](./USAGE.md) を参照してください。**
> この README はリポジトリのセットアップ・運用担当向けです。

---

## 主な機能

- 🔑 **ボタンベースの鍵管理** — 借りる / 開ける / 閉める / 返す / 受け取る
- 📦 **持ち出し場所の共有** — 部室外に鍵を持ち出すとき、Modal で場所を入力
- ↩️ **操作の取り消し** — 直前の操作を本人限定で 1 分以内に取り消せる
- 🔕 **最新操作のみアクティブ** — 古いメッセージのボタンは自動で無効化
- 📅 **金曜の長期貸出** — 「土曜まで借りる」「日曜まで借りる」が金曜のみ表示
- ⏰ **自動リマインド** — 毎日 20 時 + 2 時間無変化で持ち主にメンション
- 📱 **NFC 連携** — iPhone Shortcuts などからの HTTP リクエストで鍵をトグル
- 📊 **状態確認コマンド** — `/reminder_status` で現状とリマインド予定を一覧表示

---

## 動作要件

- **Python 3.13** 以上（型ヒント `str | None` 構文を使用）
- Discord Bot トークン
- Bot が参加している Discord サーバー

主要な依存ライブラリは `requirements.txt` を参照:
- `discord.py` 2.7.x
- `python-dotenv`
- `aiohttp`
- `tzlocal`

---

## セットアップ手順

### 1. リポジトリのクローン

```bash
git clone https://github.com/<your-account>/discord_key_bot.git
cd discord_key_bot
```

### 2. Python 仮想環境の作成と依存インストール

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Discord Bot の作成とトークン取得

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** をクリック
2. **Bot** タブで Bot を作成し、**Reset Token** で表示されたトークンを控える
3. **Bot** タブで以下を有効化:
   - `Server Members Intent`（任意）
4. **OAuth2 → URL Generator** で:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Use Slash Commands`, `Manage Messages`
5. 生成された URL からサーバーに Bot を招待

### 4. `.env` ファイルの作成

リポジトリ直下に `.env` を作成し、以下を記入:

```ini
# 必須
TOKEN=ステップ3で取得したBotトークン
KEY_CHANNEL_ID=操作対象のチャンネルID

# 任意（推奨）
GUILD_ID=対象サーバーのID            # 指定するとスラッシュコマンドが即時反映
ADMIN_USER_IDS=123456789,987654321   # 管理者の Discord ユーザーID（カンマ区切り）

# 任意（デフォルトのまま運用可能）
NFC_PORT=8080                         # NFC HTTP サーバーのポート
LOG_MAX_BYTES=2097152                 # ログ 1 ファイルのサイズ上限 (2MB)
LOG_BACKUP_COUNT=5                    # ログのローテーション世代数
```

> ID の取り方: Discord クライアントの「設定 → 詳細設定 → 開発者モード」を ON にすると、
> サーバー名やチャンネル、ユーザーアイコンの右クリックメニューに「ID をコピー」が表示されます。

### 5. 起動

```bash
.venv/bin/python key_bot.py
```

ログに `commands synced to guild ... (instant)` と表示されれば、Discord で `/` を打つとコマンドが補完候補に出てきます。

---

## デプロイ（本番運用）

### バックグラウンド実行（Linux / macOS）

```bash
nohup .venv/bin/python key_bot.py > /dev/null 2>&1 &
```

ログは `logs/key_bot.log` に自動で書き出され、2MB × 5 世代でローテーションされます。

### systemd で常駐させる場合（例）

`/etc/systemd/system/discord-key-bot.service`:

```ini
[Unit]
Description=Discord Key Management Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/discord_key_bot
ExecStart=/path/to/discord_key_bot/.venv/bin/python key_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-key-bot
sudo systemctl status discord-key-bot
```

---

## スラッシュコマンド一覧

### 全員が使える

| コマンド | 説明 |
|---------|------|
| `/reminder_status` | 現在の鍵の状態とリマインド設定を表示（本人のみ） |
| `/nfc_register` | NFC タグ用の秘密トークンを発行（再発行で上書き） |

### 現在の鍵の持ち主だけが使える

| コマンド | 説明 |
|---------|------|
| `/reminder_daily hour:<0-23>` | 毎日リマインドの時刻を変更（0 で停止） |
| `/reminder_idle hours:<0-168>` | 無変化リマインドの時間を変更（0 で停止） |

> ⚠️ 持ち主が変わると、リマインド設定は自動でデフォルト（20 時 / 2 時間）に戻ります。

### 管理者だけが使える（`ADMIN_USER_IDS` で指定）

| コマンド | 説明 |
|---------|------|
| `/debug_friday on:<bool>` | 金曜モードを ON/OFF（土日貸出ボタンの表示テスト） |
| `/debug_reminder type:<daily/idle>` | リマインドを即時送信（state は変更しない） |

---

## リマインド仕様

| 種類 | デフォルト | 動作 |
|------|-----------|------|
| **毎日リマインド (daily)** | 20:00 | 持ち主がいる時に「鍵を返し忘れていませんか？」を送信 |
| **無変化リマインド (idle)** | 2 時間 | 最終操作から指定時間経過で送信 |

### 長期貸出中のリマインド挙動

「土曜まで借りる」「日曜まで借りる」を押した場合:

| 日 | daily | idle |
|----|-------|------|
| 押下日 | 抑制 | 抑制 |
| 最終日 | **送信** | 抑制 |
| 翌日以降 | 通常通り | 通常通り |

毎日リマインドだけ最終日に通知することで「返却日に忘れない」状態を作ります。

---

## NFC 連携

`POST /nfc` エンドポイントに以下の JSON を送ると鍵の開閉がトグルされます:

```json
{ "token": "/nfc_register で発行されたトークン" }
```

リクエスト例:

```bash
curl -X POST http://<server>:8080/nfc \
  -H "Content-Type: application/json" \
  -d '{"token": "xxx"}'
```

iPhone の Shortcuts アプリで「URL の内容を取得」を使い、NFC タグに割り当てると **タグタッチで Discord に状態通知**を飛ばせます。

---

## ファイル構成

```
discord_key_bot/
├── key_bot.py            # メインのボットコード
├── requirements.txt      # Python 依存パッケージ
├── README.md             # この README（運用者向け）
├── USAGE.md              # 利用者向け使い方ガイド
├── .env                  # 環境変数（コミット禁止）
├── .gitignore
├── logs/
│   └── key_bot.log       # ローテーションログ（自動生成）
├── room_state.json       # 鍵の状態と履歴（自動生成、コミット禁止）
└── nfc_tokens.json       # NFC 用秘密トークン（自動生成、コミット禁止）
```

`.gitignore` で `.env`, `logs/`, `room_state.json`, `nfc_tokens.json`, `.venv/` を除外しています。

---

## データ永続化

### `room_state.json`

```jsonc
{
  "schema_version": 2,
  "state": "open" | "closed" | "out",
  "holder_id": 123,
  "holder_name": "...",
  "last_change_at": "ISO8601",
  "last_message_id": 9999,
  "out_location": "場所" | null,
  "long_rent_until": "YYYY-MM-DD" | null,
  "debug_friday": false,
  "reminder": {
    "daily_hour": 20,
    "idle_hours": 2,
    "daily_last_sent_date": "YYYY-MM-DD",
    "idle_last_sent_at": "ISO8601"
  },
  "history": [/* 末尾 20 件のみ保持 */]
}
```

- **アトミック書き込み**（tmp + `os.replace`）で破損リスクを軽減
- **asyncio.Lock** による排他制御で、NFC HTTP と Discord ボタンの同時操作にも安全
- 旧形式 `{"state": "open"|"closed"}` からの **自動マイグレーション**対応

### ファイルサイズ

| ファイル | サイズの上限 | 理由 |
|---------|------------|------|
| `logs/key_bot.log*` | 約 12MB | 2MB × 6 世代でローテーション |
| `room_state.json` | 約 10KB 程度 | history は末尾 20 件のみ |
| `nfc_tokens.json` | 登録ユーザー数次第（数 KB 程度） | 1 ユーザー = 1 トークン（上書き） |

---

## トラブルシューティング

### スラッシュコマンドが Discord に表示されない

- `.env` の `GUILD_ID` を指定しているか確認（未指定だとグローバル sync で最大 1 時間かかる）
- ログに `commands synced to guild ... (instant)` が出ているか確認
- Discord クライアントを Cmd/Ctrl + R でリロード
- Bot 招待時に `applications.commands` スコープを含めたか確認

### `channel ... not found` エラー

`.env` の `KEY_CHANNEL_ID` が正しいか確認してください。
Bot がそのチャンネルへの読み書き権限を持っている必要があります。

### Bot が落ちる

- メモリ不足の可能性 → サーバーの空きメモリを確認
- ログを `tail -50 logs/key_bot.log` で確認、エラーがあれば原因を調査
- systemd で運用していれば自動再起動されます

### 設定変更しても反映されない

`.env` の変更は **起動時にしか読み込まれません**。
Bot を再起動してください。

---

## 開発

### 構文チェック

```bash
.venv/bin/python -m py_compile key_bot.py
```

### ログを見る

```bash
tail -f logs/key_bot.log
```

### 依存を更新したとき

```bash
.venv/bin/pip freeze > requirements.txt
```

---

## ライセンス

MIT License（必要に応じて変更してください）

---

## 貢献

Issue / Pull Request 歓迎です。
バグ報告や機能要望は GitHub Issue でお知らせください。
