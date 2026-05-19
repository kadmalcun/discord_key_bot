"""
鍵管理用のDiscordボット

概要:
- 部室の鍵の貸し借りを管理するためのDiscordボット
- ボタンを使用して以下の操作を実行できる:
  - 鍵を借りる
  - 部屋を開ける/閉める
  - 鍵を返す
  - 鍵を他の人に受け渡す
  - 部室を閉めて持ち出す（場所を入力）
  - 直前の操作を取り消す
  - 金曜は土曜/日曜まで借りる
- 持ち主にリマインド（定時 + 無変化時間）
- 各操作は監査ログとして記録され、誰がいつ操作したかが分かる
- Embedを使用して見やすい表示を実現

主な機能:
1. 起動時に操作選択用のメッセージを表示
2. ボタンによるインタラクティブな操作（最新メッセージのみ操作可能）
3. 操作履歴の記録と取り消し
4. 鍵の受け渡し機能
5. リマインド（持ち主に DM 風メンション）
"""

# --- ライブラリのインポート ---
# 標準ライブラリ（Python に最初から入っているもの）
import asyncio          # 非同期処理（複数の処理を並行で動かす）
import json             # JSON ファイルの読み書き
import logging          # ログ出力
import os               # ファイルパスや環境変数の操作
import secrets          # 安全なランダムトークン生成（NFC 用）
import tempfile         # 一時ファイル作成（state を安全に書き込むため）
from datetime import date, datetime, timedelta  # 日時の計算
from logging.handlers import RotatingFileHandler  # ログのローテーション

# 外部ライブラリ（pip install で入れたもの）
import discord                          # Discord 本体
from aiohttp import web                 # NFC タッチを受ける HTTP サーバー
from discord import app_commands        # スラッシュコマンド用
from discord.ext import tasks           # 定期実行タスク（リマインド用）
from dotenv import load_dotenv          # .env ファイルから環境変数を読む
from tzlocal import get_localzone       # OS のタイムゾーン（日本時間）を取得

# .env ファイルを読み込む（TOKEN などをここに書いておく）
load_dotenv()


# --- ファイルパスと設定値の定義 ---
# logs フォルダ、nfc_tokens.json、room_state.json はこのスクリプトと同じ場所に置く
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_NFC_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nfc_tokens.json")
_ROOM_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "room_state.json")

# 環境変数（.env や OS の設定）から値を取得。なければデフォルトを使う
_NFC_PORT = int(os.getenv("NFC_PORT", "8080"))                                      # NFC HTTP サーバーのポート番号
_KEY_CHANNEL_ID = int(os.getenv("KEY_CHANNEL_ID", "1100228900501594179"))           # 操作対象の Discord チャンネル ID
_GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None                                 # サーバー ID（指定するとコマンドが即時反映）


def _parse_admin_ids(env_value: str | None) -> set[int]:
    """カンマ区切りの ID 文字列を set に変換する。例: '123,456' → {123, 456}"""
    if not env_value:
        return set()
    result: set[int] = set()
    for s in env_value.split(","):
        s = s.strip()
        if s.isdigit():
            result.add(int(s))
    return result


# 管理者のユーザー ID（/debug_* コマンドを使える人）
_ADMIN_USER_IDS = _parse_admin_ids(os.getenv("ADMIN_USER_IDS"))


def _is_admin(user_id: int) -> bool:
    """このユーザーが管理者かどうかを返す"""
    return user_id in _ADMIN_USER_IDS


# --- ログ関連の設定 ---
_LOG_FILE = os.path.join(_LOG_DIR, "key_bot.log")
_MAX_LOG_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))   # ログ 1 ファイルの最大サイズ（デフォ 2MB）
_LOG_BACKUPS = int(os.getenv("LOG_BACKUP_COUNT", "5"))                   # ログを何世代保持するか

# --- リマインドや履歴の設定値 ---
_DEFAULT_DAILY_HOUR = 20   # 毎日リマインドのデフォルト時刻（20 時）
_DEFAULT_IDLE_HOURS = 2    # 無変化リマインドのデフォルト時間（2 時間）
_HISTORY_MAX = 20          # 操作履歴を何件まで保持するか
_UNDO_TTL_SEC = 60         # 取り消しボタンの有効秒数（このあとボタンが消える）


def _setup_logging() -> logging.Logger:
    """ログ出力の準備。ファイルへの保存と、画面への出力を両方設定する"""
    os.makedirs(_LOG_DIR, exist_ok=True)  # logs フォルダがなければ作成
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(file_handler)
        root.addHandler(stream)

    # discord / aiohttp はデフォルトだとHTTPログが多く、ディスクを圧迫しやすい
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    return logging.getLogger("key_bot")


logger = _setup_logging()


# --- NFC タグのトークン管理 ---
# NFC タグにスマホをかざすと HTTP リクエストが飛ぶ仕組み。
# 「誰が押したか」を識別するための秘密トークンを JSON で保存する。

def _load_tokens() -> dict:
    """nfc_tokens.json を読む。ファイルがなければ空の dict を返す"""
    if not os.path.exists(_NFC_TOKEN_FILE):
        return {}
    with open(_NFC_TOKEN_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_tokens(tokens: dict) -> None:
    """nfc_tokens.json に書き込む"""
    with open(_NFC_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)


def _upsert_token(user_id: int) -> str:
    """そのユーザーの新しいトークンを発行し、古いものは破棄する"""
    tokens = _load_tokens()
    # このユーザーの古いトークンを除外
    tokens = {t: uid for t, uid in tokens.items() if uid != str(user_id)}
    # ランダムな安全トークンを生成（推測不可能）
    token = secrets.token_urlsafe(24)
    tokens[token] = str(user_id)
    _save_tokens(tokens)
    return token


def _find_user_id(token: str) -> str | None:
    """トークンからユーザー ID を逆引きする"""
    return _load_tokens().get(token)


# --- 鍵の状態 (room_state.json) を管理する関数たち ---
# 「いま誰が鍵を持っているか」「部屋が開いているか」などを JSON ファイルに保存する。
# 再起動しても状態が消えないようにするためにファイルに残す。

# 同時に書き込みが起きないようにする鍵（NFC HTTP と Discord ボタンの競合防止）
_STATE_LOCK = asyncio.Lock()


def _default_reminder() -> dict:
    """リマインド設定の初期値（持ち主が変わるたびにこの値に戻る）"""
    return {
        "daily_hour": _DEFAULT_DAILY_HOUR,            # 毎日の通知時刻（0 で停止）
        "idle_hours": _DEFAULT_IDLE_HOURS,            # 無変化通知の時間（0 で停止）
        "daily_last_sent_date": None,                 # 最後に daily 通知した日付（重複防止用）
        "idle_last_sent_at": None,                    # 最後に idle 通知した時刻（重複防止用）
    }


def _default_state() -> dict:
    """状態ファイルが空のときの初期値"""
    return {
        "schema_version": 2,
        "state": "closed",
        "holder_id": None,
        "holder_name": None,
        "last_change_at": None,
        "last_message_id": None,
        "last_message_channel_id": _KEY_CHANNEL_ID,
        "out_location": None,
        "long_rent_until": None,
        "debug_friday": False,
        "reminder": _default_reminder(),
        "history": [],
    }


def _now_iso() -> str:
    """現在の日時を ISO 文字列で返す（例: '2026-05-19T15:42:29+09:00'）"""
    return datetime.now(get_localzone()).isoformat()


def _today_iso_date() -> str:
    """今日の日付を ISO 文字列で返す（例: '2026-05-19'）"""
    return datetime.now(get_localzone()).date().isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    """ISO 文字列を datetime に変換。失敗したら None を返す"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _fmt_jst(s: str | None) -> str:
    """ISO 文字列を日本時間 (yyyy-mm-dd HH:MM) に整形。空なら '—'"""
    dt = _parse_iso(s)
    if dt is None:
        return "—"
    return dt.astimezone(get_localzone()).strftime("%Y-%m-%d %H:%M")


def _migrate_state(d: dict) -> dict:
    """旧形式 {"state": "open"|"closed"} から新形式に補完"""
    if d.get("schema_version") == 2:
        # 欠損キーだけ補完
        base = _default_state()
        for k, v in base.items():
            d.setdefault(k, v)
        # reminder の内部も補完
        rem_base = _default_reminder()
        for k, v in rem_base.items():
            d["reminder"].setdefault(k, v)
        return d

    base = _default_state()
    base["state"] = d.get("state", "closed")
    return base


def _load_state() -> dict:
    """room_state.json を読んで dict として返す。ファイルが無ければ初期値"""
    if not os.path.exists(_ROOM_STATE_FILE):
        return _default_state()
    try:
        with open(_ROOM_STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        # 読めなかったらログを残して初期値で続行
        logger.exception("state file unreadable; using default")
        return _default_state()
    return _migrate_state(raw)


def _save_state_sync(d: dict) -> None:
    """state を JSON ファイルに保存する（安全な書き込み）

    一時ファイルに書いてから rename することで、書き込み途中で Bot が落ちても
    ファイルが壊れないようにしている。
    """
    dir_ = os.path.dirname(_ROOM_STATE_FILE)
    fd, tmp = tempfile.mkstemp(prefix=".room_state.", suffix=".tmp", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _ROOM_STATE_FILE)  # 一気に置き換える（アトミック操作）
    except Exception:
        # 失敗したら一時ファイルを掃除
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


async def _save_state(d: dict) -> None:
    """ロックを取って state を保存（複数の処理が同時に書き込まないように）"""
    async with _STATE_LOCK:
        _save_state_sync(d)


# 旧 API（NFC ハンドラ内の他コードなどから呼ばれた場合の保険として残す）
def _get_room_state() -> str:
    return _load_state().get("state", "closed")


def _set_room_state(state: str) -> None:
    d = _load_state()
    d["state"] = state
    _save_state_sync(d)


def _snapshot(state: dict) -> dict:
    """現在の state を簡単に dict として複製する（取り消し機能用）

    操作する前にこれを保存しておけば、取り消したいときに元に戻せる。
    """
    return {
        "state": state.get("state"),
        "holder_id": state.get("holder_id"),
        "holder_name": state.get("holder_name"),
        "last_change_at": state.get("last_change_at"),
        "last_message_id": state.get("last_message_id"),
        "last_message_channel_id": state.get("last_message_channel_id"),
        "out_location": state.get("out_location"),
        "long_rent_until": state.get("long_rent_until"),
    }


def _apply_snapshot(state: dict, snap: dict) -> None:
    """snapshot の中身で state を上書きする（取り消し時に使う）"""
    for k, v in snap.items():
        state[k] = v


def _push_history(
    state: dict,
    action: str,
    user: discord.abc.User | None,
    message_id: int | None,
    channel_id: int | None,
    prev_snapshot: dict,
) -> None:
    """操作履歴を 1 件追加する（古いものは自動で削除されて常に 20 件以下に保つ）"""
    entry = {
        "action": action,
        "user_id": user.id if user else None,
        "user_name": user.display_name if user else None,
        "at": _now_iso(),
        "message_id": message_id,
        "message_channel_id": channel_id,
        "prev_snapshot": prev_snapshot,
    }
    state["history"].append(entry)
    if len(state["history"]) > _HISTORY_MAX:
        state["history"] = state["history"][-_HISTORY_MAX:]


def _set_holder(state: dict, user: discord.abc.User | None) -> bool:
    """鍵の持ち主を更新する。前の持ち主と違うときだけリマインド設定をデフォルトに戻す。

    Returns:
        持ち主が変わったら True、変わらなかったら False
    """
    new_id = user.id if user else None
    if state.get("holder_id") == new_id:
        return False  # 同じ人の継続操作 → リマインド設定は維持
    state["holder_id"] = new_id
    state["holder_name"] = user.display_name if user else None
    state["reminder"] = _default_reminder()  # 持ち主が変わったら設定を初期化
    return True


def _is_friday() -> bool:
    """今日が金曜日かどうか。デバッグモードが ON なら強制的に True"""
    if _load_state().get("debug_friday"):
        return True
    # weekday() は月=0, 火=1, ..., 金=4, 土=5, 日=6
    return datetime.now(get_localzone()).weekday() == 4


def _is_long_rent_active(state: dict) -> bool:
    """長期貸出期間内（最終日を含む）。idle リマインドの抑制に使う。"""
    ld = state.get("long_rent_until")
    if not ld:
        return False
    try:
        return date.fromisoformat(ld) >= datetime.now(get_localzone()).date()
    except ValueError:
        return False


def _is_before_long_rent_last_day(state: dict) -> bool:
    """長期貸出の最終日より前（最終日になったら False）。daily リマインドの抑制に使う。"""
    ld = state.get("long_rent_until")
    if not ld:
        return False
    try:
        return date.fromisoformat(ld) > datetime.now(get_localzone()).date()
    except ValueError:
        return False


def _next_weekday_date(target_weekday: int) -> str:
    """今日から見て次の target_weekday (mon=0..sun=6)。今日が target_weekday なら今日。"""
    today = datetime.now(get_localzone()).date()
    delta = (target_weekday - today.weekday()) % 7
    return (today + timedelta(days=delta)).isoformat()


# --- 持ち出し場所入力 Modal ---
# Modal は Discord のポップアップ入力フォーム。「持ち出す」ボタンを押すと表示される。

class KeyOutModal(discord.ui.Modal, title="持ち出し先を入力"):
    location = discord.ui.TextInput(
        label="どこに持ち出しますか？",
        placeholder="例: 研究棟A503、地下ホール",
        max_length=80,
        required=True,
    )

    async def on_submit(self, inter: discord.Interaction):
        await _handle_room_out_submitted(inter, str(self.location).strip())

    async def on_error(self, inter: discord.Interaction, error: Exception) -> None:
        logger.exception("KeyOutModal error: %s", error)
        if not inter.response.is_done():
            try:
                await inter.response.send_message("入力エラーが発生しました。", ephemeral=True)
            except discord.HTTPException:
                pass


# --- NFC HTTP サーバー ---
# iPhone Shortcuts などから HTTP POST が来ると、鍵の開閉をトグルする。

async def _handle_nfc(request: web.Request) -> web.Response:
    """NFC タッチで HTTP POST されたときの処理（鍵の開閉をトグル）"""
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid JSON")

    token = body.get("token", "")
    user_id_str = _find_user_id(token.strip())
    if not user_id_str:
        logger.warning("NFC: unknown token")
        return web.Response(status=401, text="unknown token")

    channel = client.get_channel(_KEY_CHANNEL_ID)
    if channel is None:
        return web.Response(status=503, text="channel not found")

    user = client.get_user(int(user_id_str))

    async with _STATE_LOCK:
        state = _load_state()
        prev = _snapshot(state)

        current = state.get("state", "closed")
        if current == "closed":
            label = "開けました"
            state["state"] = "open"
        else:
            label = "閉めました"
            state["state"] = "closed"
            # 戻ってきたので持ち出し場所はクリア
            state["out_location"] = None

        if user is not None:
            _set_holder(state, user)
        state["last_change_at"] = _now_iso()

        embed = create_Embed(f"[NFC] {label}")
        _decorate_embed_with_status(embed, state)
        if user:
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        else:
            embed.set_author(name=f"ユーザーID: {user_id_str}")

        try:
            sent = await channel.send(embed=embed)
            new_msg_id = sent.id
        except discord.HTTPException as e:
            logger.warning("NFC: failed to send message: %s", e)
            new_msg_id = None

        # 前メッセージのボタンを剥がす（ベストエフォート）
        await _strip_view(channel, prev.get("last_message_id"))

        state["last_message_id"] = new_msg_id
        state["last_message_channel_id"] = channel.id
        _push_history(state, "nfc_toggle", user, new_msg_id, channel.id, prev)
        _save_state_sync(state)

    logger.info("NFC: %s user_id=%s", label, user_id_str)
    return web.Response(status=200, text="ok")


async def _start_nfc_server() -> None:
    """NFC タッチを受け取る簡易 HTTP サーバーを起動する"""
    app = web.Application()
    app.router.add_post("/nfc", _handle_nfc)  # POST /nfc を受け付ける
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", _NFC_PORT)
    await site.start()
    logger.info("NFC HTTP server listening on port %d", _NFC_PORT)


# --- Discord クライアントの作成 ---
intents = discord.Intents.default()           # メッセージ送信などの基本権限を取得
client = discord.Client(intents=intents)      # Bot 本体
tree = app_commands.CommandTree(client)       # スラッシュコマンドを登録するツリー
_tree_synced = False                          # コマンド sync が済んだかフラグ


def create_Embed(title):
    """共通の Embed（埋め込みメッセージ）テンプレート。タイトル＋現在時刻＋青色"""
    embed = discord.Embed(
        title=title,
        timestamp=datetime.now(),
        color=0x0000FF,  # 青色（16 進数）
    )
    return embed


def _embed_set_actor(embed: discord.Embed, user: discord.abc.User) -> None:
    """Embed の上部に「誰がやったか」を表示する（アイコン + 名前）"""
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)


def _decorate_embed_with_status(embed: discord.Embed, state: dict) -> None:
    """Embed に「鍵の現在地」「長期貸出中」などの状態フィールドを足す。"""
    if state.get("state") == "out" and state.get("out_location"):
        embed.add_field(name="鍵の現在地", value=state["out_location"], inline=False)
    if state.get("long_rent_until"):
        embed.add_field(name="長期貸出", value=f"{state['long_rent_until']} まで", inline=False)


# --- ACTIONS テーブル（ボタン操作の動作一覧）---
# ボタンの custom_id ごとに「何をするか」をまとめた辞書。
# このおかげで if/elif の長い分岐を書かずに済む。
#
# 形式: custom_id -> (Embed タイトル, 新しい state | None=変更しない, 持ち主の扱い, 次の view 名)
# 持ち主の扱い:
#   "set_self"            = 押した人を持ち主にする
#   "clear"               = 持ち主をクリア（返却時など）
#   "set_self_long_sat"   = 持ち主にしつつ「土曜まで貸出」をセット
#   "set_self_long_sun"   = 持ち主にしつつ「日曜まで貸出」をセット
ACTIONS: dict[str, tuple[str, str | None, str, str]] = {
    "key_rent":           ("借りました",           None,     "set_self", "rent_done"),
    "room_open":          ("開けました",           "open",   "set_self", "open_done"),
    "room_close":         ("閉めました",           "closed", "set_self", "close_done"),
    "key_return":         ("返しました",           "closed", "clear",    "initial"),
    "key_pass_rent":      ("受け取りました",       None,     "set_self", "rent_done"),
    "key_pass_open":      ("受け取りました",       None,     "set_self", "open_done"),
    "key_pass_close":     ("受け取りました",       None,     "set_self", "close_done"),
    "key_rent_until_sat": ("土曜まで借りました",   None,     "set_self_long_sat", "rent_done"),
    "key_rent_until_sun": ("日曜まで借りました",   None,     "set_self_long_sun", "rent_done"),
}

# action → 日本語ラベル（Undo メッセージで使用）
ACTION_JP: dict[str, str] = {
    "key_rent":           "鍵を借りる",
    "room_open":          "部屋を開ける",
    "room_close":         "部屋を閉める",
    "key_return":         "鍵を返す",
    "key_pass_rent":      "鍵を受け取る",
    "key_pass_open":      "鍵を受け取る",
    "key_pass_close":     "鍵を受け取る",
    "key_rent_until_sat": "土曜まで借りる",
    "key_rent_until_sun": "日曜まで借りる",
    "room_out":           "鍵を持ち出す",
    "nfc_toggle":         "NFC 操作",
}


def _build_view(view_key: str, *, include_undo: bool = True) -> discord.ui.View:
    """状況に応じたボタンの組み合わせを作る

    Args:
        view_key: どの局面か（"initial"=初期画面、"rent_done"=借りた後、など）
        include_undo: 取り消すボタンも付けるか（起動メッセージは付けない）
    """
    view = discord.ui.View()

    if view_key == "initial":
        # 初期画面・返した後: 「借りる」だけ。金曜は土日貸出ボタンも追加
        view.add_item(discord.ui.Button(
            label="借りる", style=discord.ButtonStyle.success, custom_id="key_rent",
        ))
        if _is_friday():
            view.add_item(discord.ui.Button(
                label="土曜まで借りる", style=discord.ButtonStyle.success,
                custom_id="key_rent_until_sat",
            ))
            view.add_item(discord.ui.Button(
                label="日曜まで借りる", style=discord.ButtonStyle.success,
                custom_id="key_rent_until_sun",
            ))

    elif view_key == "rent_done":
        # 借りた直後 / 受け取った直後（前=借りた）
        view.add_item(discord.ui.Button(
            label="開ける", style=discord.ButtonStyle.success, custom_id="room_open",
        ))
        view.add_item(discord.ui.Button(
            label="返す", style=discord.ButtonStyle.danger, custom_id="key_return",
        ))
        view.add_item(discord.ui.Button(
            label="受け取る", style=discord.ButtonStyle.primary, custom_id="key_pass_rent",
        ))
        view.add_item(discord.ui.Button(
            label="持ち出す", style=discord.ButtonStyle.secondary, custom_id="room_out_open_modal",
        ))

    elif view_key == "open_done":
        # 開けた後: 部屋にいる状態なので「持ち出す」は出さない
        view.add_item(discord.ui.Button(
            label="閉める", style=discord.ButtonStyle.success, custom_id="room_close",
        ))
        view.add_item(discord.ui.Button(
            label="受け取る", style=discord.ButtonStyle.primary, custom_id="key_pass_open",
        ))

    elif view_key == "close_done":
        # 閉めた後: 開け直しも持ち出しもできる
        view.add_item(discord.ui.Button(
            label="開ける", style=discord.ButtonStyle.success, custom_id="room_open",
        ))
        view.add_item(discord.ui.Button(
            label="返す", style=discord.ButtonStyle.danger, custom_id="key_return",
        ))
        view.add_item(discord.ui.Button(
            label="受け取る", style=discord.ButtonStyle.primary, custom_id="key_pass_close",
        ))
        view.add_item(discord.ui.Button(
            label="持ち出す", style=discord.ButtonStyle.secondary, custom_id="room_out_open_modal",
        ))

    elif view_key == "out_done":
        # 持ち出し中（鍵が部室の外）
        view.add_item(discord.ui.Button(
            label="開ける", style=discord.ButtonStyle.success, custom_id="room_open",
        ))
        view.add_item(discord.ui.Button(
            label="返す", style=discord.ButtonStyle.danger, custom_id="key_return",
        ))
        view.add_item(discord.ui.Button(
            label="受け取る", style=discord.ButtonStyle.primary, custom_id="key_pass_rent",
        ))

    if include_undo:
        view.add_item(discord.ui.Button(
            label="取り消す", style=discord.ButtonStyle.secondary, custom_id="undo",
        ))

    return view


def _view_key_for_state(state: dict) -> str:
    """現在の state から、最新メッセージに付けるべき view_key を決める（Undo 後の復活用）"""
    s = state.get("state")
    holder = state.get("holder_id")
    if holder is None:
        return "initial"
    if s == "open":
        return "open_done"
    if s == "out":
        return "out_done"
    if s == "closed":
        # 持ち主あり & 閉まっている = 借りた直後 or 閉めた直後
        # 履歴の直近 action で判別（無ければ rent_done を返す）
        hist = state.get("history") or []
        if hist:
            last_action = hist[-1].get("action")
            if last_action == "room_close":
                return "close_done"
            if last_action in ("key_pass_close",):
                return "close_done"
        return "rent_done"
    return "initial"


# --- メッセージ送信ヘルパ ---

async def _schedule_undo_removal(
    channel: discord.abc.Messageable,
    msg_id: int,
    view_key: str,
    delay: float = float(_UNDO_TTL_SEC),
) -> None:
    """delay 秒後に、まだそのメッセージが最新なら undo ボタンだけ消す。"""
    try:
        await asyncio.sleep(delay)
        state = _load_state()
        if state.get("last_message_id") != msg_id:
            return  # 既に別の操作で view が剥がされている
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(view=_build_view(view_key, include_undo=False))
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning("undo removal: edit failed: %s", e)
    except Exception:
        logger.exception("_schedule_undo_removal failed")


async def _strip_view(channel: discord.abc.Messageable, message_id: int | None) -> None:
    """指定したメッセージのボタンを全部消す（古いメッセージのボタンを無効化する用）"""
    if not message_id:
        return
    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(view=None)  # view=None でボタンが消える
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.warning("strip_view: forbidden for message %s", message_id)
    except discord.HTTPException as e:
        logger.warning("strip_view: HTTP error %s for message %s", e, message_id)


async def _send_action_message(
    inter: discord.Interaction,
    action: str,
    label: str,
    new_state: str | None,
    holder_strategy: str,
    view_key: str,
) -> None:
    """ボタン押下後の共通処理（Embed 送信、state 更新、履歴記録、古いボタン削除）"""
    channel = inter.channel
    user = inter.user

    # interaction 応答（メッセージ ID 確実取得のため defer + channel.send）
    try:
        await inter.response.defer()
    except discord.HTTPException:
        pass

    async with _STATE_LOCK:
        state = _load_state()
        prev = _snapshot(state)

        if new_state is not None:
            state["state"] = new_state
            # 閉めて鍵が戻ってきたら持ち出し場所はクリア
            if new_state == "closed":
                state["out_location"] = None
            if new_state == "open":
                state["out_location"] = None

        if holder_strategy == "clear":
            _set_holder(state, None)
            state["long_rent_until"] = None
        elif holder_strategy == "set_self":
            _set_holder(state, user)
        elif holder_strategy == "set_self_long_sat":
            if not _is_friday():
                await _send_ephemeral(inter, "土日貸出ボタンは金曜のみ使用できます。")
                return
            _set_holder(state, user)
            state["long_rent_until"] = _next_weekday_date(5)  # 土曜
        elif holder_strategy == "set_self_long_sun":
            if not _is_friday():
                await _send_ephemeral(inter, "土日貸出ボタンは金曜のみ使用できます。")
                return
            _set_holder(state, user)
            state["long_rent_until"] = _next_weekday_date(6)  # 日曜

        state["last_change_at"] = _now_iso()

        embed = create_Embed(label)
        _embed_set_actor(embed, user)
        _decorate_embed_with_status(embed, state)
        view = _build_view(view_key, include_undo=True)

        try:
            sent = await channel.send(embed=embed, view=view)
            new_msg_id = sent.id
        except discord.HTTPException as e:
            logger.warning("send_action_message: send failed: %s", e)
            return

        await _strip_view(channel, prev.get("last_message_id"))

        state["last_message_id"] = new_msg_id
        state["last_message_channel_id"] = channel.id
        _push_history(state, action, user, new_msg_id, channel.id, prev)
        _save_state_sync(state)

    asyncio.create_task(_schedule_undo_removal(channel, new_msg_id, view_key))
    logger.info("action=%s user=%s msg=%s", action, user, new_msg_id)


async def _handle_room_out_submitted(inter: discord.Interaction, location: str) -> None:
    """Modal で「持ち出し先」が入力されたあとの処理"""
    if not location:
        await _send_ephemeral(inter, "場所を入力してください。")
        return

    channel = inter.channel
    user = inter.user

    try:
        await inter.response.defer()
    except discord.HTTPException:
        pass

    async with _STATE_LOCK:
        state = _load_state()
        prev = _snapshot(state)

        state["state"] = "out"
        state["out_location"] = location
        _set_holder(state, user)
        state["last_change_at"] = _now_iso()

        embed = create_Embed(f"{location} に持ち出しました")
        _embed_set_actor(embed, user)
        _decorate_embed_with_status(embed, state)
        view = _build_view("out_done", include_undo=True)

        try:
            sent = await channel.send(embed=embed, view=view)
            new_msg_id = sent.id
        except discord.HTTPException as e:
            logger.warning("room_out: send failed: %s", e)
            return

        await _strip_view(channel, prev.get("last_message_id"))

        state["last_message_id"] = new_msg_id
        state["last_message_channel_id"] = channel.id
        _push_history(state, "room_out", user, new_msg_id, channel.id, prev)
        _save_state_sync(state)

    asyncio.create_task(_schedule_undo_removal(channel, new_msg_id, "out_done"))
    logger.info("room_out user=%s location=%s", user, location)


async def _send_ephemeral(inter: discord.Interaction, content: str) -> None:
    """本人にだけ見えるメッセージを送る（エラー通知などに使う）"""
    try:
        if inter.response.is_done():
            await inter.followup.send(content, ephemeral=True)
        else:
            await inter.response.send_message(content, ephemeral=True)
    except discord.HTTPException:
        pass


# --- 取り消し（Undo）の処理 ---

async def _handle_undo(inter: discord.Interaction) -> None:
    """「取り消す」ボタンが押されたときの処理（直前の操作を巻き戻す）"""
    user = inter.user
    channel = inter.channel

    async with _STATE_LOCK:
        state = _load_state()
        history = state.get("history") or []
        if not history:
            await _send_ephemeral(inter, "取り消せる操作がありません。")
            return

        last = history[-1]
        if last.get("user_id") != user.id:
            await _send_ephemeral(inter, "本人のみ取り消せます。")
            return

        at = _parse_iso(last.get("at"))
        if at and (datetime.now(get_localzone()) - at).total_seconds() > _UNDO_TTL_SEC:
            await _send_ephemeral(inter, "取り消し可能な時間（1分）を過ぎています。")
            return

        try:
            await inter.response.defer()
        except discord.HTTPException:
            pass

        # 巻き戻し
        history.pop()
        snap = last.get("prev_snapshot") or {}
        _apply_snapshot(state, snap)
        state["history"] = history

        # 取消対象メッセージを「取消済み」に編集
        cancel_msg_id = last.get("message_id")
        cancel_ch_id = last.get("message_channel_id") or (channel.id if channel else None)
        if cancel_msg_id and cancel_ch_id:
            try:
                ch = client.get_channel(cancel_ch_id) or channel
                msg = await ch.fetch_message(cancel_msg_id)
                action_jp = ACTION_JP.get(last.get("action") or "", "操作")
                cancel_embed = create_Embed(f"「{action_jp}」を取り消しました")
                _embed_set_actor(cancel_embed, user)
                await msg.edit(embed=cancel_embed, view=None)
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                logger.warning("undo: failed to edit canceled message: %s", e)

        # 1 つ前のメッセージ（巻き戻し後の最新）にボタン復活
        prev_msg_id = state.get("last_message_id")
        prev_ch_id = state.get("last_message_channel_id")
        view_key = _view_key_for_state(state)
        if prev_msg_id and prev_ch_id and view_key != "initial":
            try:
                ch = client.get_channel(prev_ch_id) or channel
                prev_msg = await ch.fetch_message(prev_msg_id)
                await prev_msg.edit(view=_build_view(view_key, include_undo=False))
            except discord.NotFound:
                # 消失していたら新規送信でフォールバック
                try:
                    fb_embed = create_Embed("（復元）操作を選択してください")
                    sent = await channel.send(
                        embed=fb_embed,
                        view=_build_view(view_key, include_undo=False),
                    )
                    state["last_message_id"] = sent.id
                    state["last_message_channel_id"] = channel.id
                except discord.HTTPException as e:
                    logger.warning("undo: fallback send failed: %s", e)
            except discord.HTTPException as e:
                logger.warning("undo: failed to restore prev view: %s", e)
        elif view_key == "initial":
            # 巻き戻し後に持ち主なし→初期画面を改めて出す
            try:
                fb_embed = create_Embed("操作を選択してください")
                sent = await channel.send(
                    embed=fb_embed,
                    view=_build_view("initial", include_undo=False),
                )
                state["last_message_id"] = sent.id
                state["last_message_channel_id"] = channel.id
            except discord.HTTPException as e:
                logger.warning("undo: initial send failed: %s", e)

        _save_state_sync(state)

    logger.info("undo user=%s action=%s", user, last.get("action"))


# --- リマインド（自動通知） ---
# 1 分ごとにこの関数が動き、条件を満たしていたら持ち主にメンション通知する。

@tasks.loop(minutes=1)
async def _reminder_tick():
    """1 分ごとに呼ばれる。daily / idle リマインドの送信判定を行う"""
    try:
        state = _load_state()
        holder_id = state.get("holder_id")

        # 期限切れの long_rent_until は持ち主有無に関わらずクリアしておく
        clear_long_rent = bool(state.get("long_rent_until")) and not _is_long_rent_active(state)
        if clear_long_rent:
            async with _STATE_LOCK:
                s2 = _load_state()
                s2["long_rent_until"] = None
                _save_state_sync(s2)

        if not holder_id:
            return

        channel_id = state.get("last_message_channel_id") or _KEY_CHANNEL_ID
        channel = client.get_channel(channel_id)
        if channel is None:
            return

        now = datetime.now(get_localzone())
        rem = state.get("reminder") or _default_reminder()

        new_daily_sent: str | None = None
        new_idle_sent: str | None = None

        # daily: 長期貸出の最終日より前は抑制（最終日と過ぎた日は出す）
        daily_hour = rem.get("daily_hour") or 0
        if daily_hour and now.hour == daily_hour and not _is_before_long_rent_last_day(state):
            today = now.date().isoformat()
            if rem.get("daily_last_sent_date") != today:
                try:
                    await channel.send(
                        f"<@{holder_id}> 鍵を返し忘れていませんか？",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                    new_daily_sent = today
                except discord.HTTPException as e:
                    logger.warning("daily reminder send failed: %s", e)

        # idle: 長期貸出期間（最終日含む）は抑制
        idle_hours = rem.get("idle_hours") or 0
        if idle_hours and not _is_long_rent_active(state):
            last_change = _parse_iso(state.get("last_change_at"))
            last_sent = _parse_iso(rem.get("idle_last_sent_at"))
            candidates = [t for t in (last_change, last_sent) if t]
            base = max(candidates) if candidates else None
            if base and (now - base).total_seconds() >= idle_hours * 3600:
                try:
                    await channel.send(
                        f"<@{holder_id}> {idle_hours}時間 状態に変化がありません。",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                    new_idle_sent = _now_iso()
                except discord.HTTPException as e:
                    logger.warning("idle reminder send failed: %s", e)

        # 送信した分だけピンポイントに last_sent を更新（slash command の変更を踏み潰さない）
        if new_daily_sent or new_idle_sent:
            async with _STATE_LOCK:
                s2 = _load_state()
                s2.setdefault("reminder", _default_reminder())
                if new_daily_sent:
                    s2["reminder"]["daily_last_sent_date"] = new_daily_sent
                if new_idle_sent:
                    s2["reminder"]["idle_last_sent_at"] = new_idle_sent
                _save_state_sync(s2)
    except Exception:
        logger.exception("_reminder_tick failed")


@_reminder_tick.before_loop
async def _before_reminder_tick():
    """ループを始める前に、Bot のログインが完了するまで待つ"""
    await client.wait_until_ready()


# --- Discord のイベントハンドラ ---

@client.event
async def on_ready():
    """Bot が Discord に接続できたときに 1 回呼ばれる（起動処理）"""
    global _tree_synced
    logger.info("on_ready user=%s discord.py=%s", client.user, discord.__version__)

    # スラッシュコマンドの sync はチャンネル有無に関わらず最優先で実行
    if not _tree_synced:
        try:
            if _GUILD_ID:
                guild = discord.Object(id=_GUILD_ID)
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
                logger.info("commands synced to guild %s (instant)", _GUILD_ID)
            else:
                await tree.sync()
                logger.info("application commands synced (global, ~1h delay)")
            _tree_synced = True
        except discord.HTTPException as e:
            logger.warning("tree.sync failed: %s", e)

    # NFC サーバーも同様にチャンネル取得とは独立
    await _start_nfc_server()
    if not _reminder_tick.is_running():
        _reminder_tick.start()

    # 分の境界に揃える（既存挙動）
    wait_sec = 60 - datetime.now(get_localzone()).second
    if wait_sec > 0:
        logger.info("aligning to minute boundary: sleep %.1fs (async)", wait_sec)
        await asyncio.sleep(wait_sec)

    channel = client.get_channel(_KEY_CHANNEL_ID)
    if channel is None:
        logger.error("channel %s not found (KEY_CHANNEL_ID env で変更可)", _KEY_CHANNEL_ID)
        return

    # 起動メッセージの再利用 or 新規作成
    state = _load_state()
    view_key = _view_key_for_state(state)
    embed = discord.Embed(
        title="on ready",
        color=0x0000FF,
        description="操作を選択してください",
    )
    _decorate_embed_with_status(embed, state)
    view = _build_view(view_key, include_undo=False)

    reused = False
    if state.get("last_message_id"):
        try:
            prev = await channel.fetch_message(state["last_message_id"])
            await prev.edit(embed=embed, view=view)
            reused = True
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning("on_ready: failed to edit last message: %s", e)

    if not reused:
        try:
            sent = await channel.send(embed=embed, view=view)
            async with _STATE_LOCK:
                s = _load_state()
                s["last_message_id"] = sent.id
                s["last_message_channel_id"] = channel.id
                _save_state_sync(s)
        except discord.HTTPException as e:
            logger.warning("on_ready: send failed: %s", e)



# --- スラッシュコマンドの定義 ---
# /xxxx の形でユーザーが叩けるコマンド。@tree.command で登録する。

@tree.command(name="nfc_register", description="NFCタグ用のトークンを発行（再実行で上書き）")
async def nfc_register(inter: discord.Interaction):
    """/nfc_register: NFC 用の秘密トークンを発行（再実行で古いものは破棄）"""
    token = _upsert_token(inter.user.id)
    await inter.response.send_message(
        f"トークンを発行しました。Shortcutsの設定に貼り付けてください。\n```\n{token}\n```\n"
        "※ 再度このコマンドを実行すると古いトークンは無効になります。",
        ephemeral=True,
    )
    logger.info("nfc_register user=%s", inter.user)


async def _announce_reminder_change(
    inter: discord.Interaction, title: str, desc: str,
) -> None:
    """リマインド設定変更時のチャンネル公開通知。"""
    channel = inter.channel
    if channel is None:
        return
    embed = create_Embed(title)
    _embed_set_actor(embed, inter.user)
    embed.description = desc
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        logger.warning("announce reminder change failed: %s", e)


@tree.command(
    name="reminder_daily",
    description="持ち主のみ。毎日リマインドの時刻を変更",
)
@app_commands.describe(hour="毎日リマインドする時刻 (0-23、0 で停止)")
async def reminder_daily(inter: discord.Interaction, hour: int):
    state = _load_state()
    if state.get("holder_id") != inter.user.id:
        await inter.response.send_message(
            "現在の鍵の持ち主のみ変更できます。", ephemeral=True,
        )
        return
    if not (0 <= hour <= 23):
        await inter.response.send_message("hour は 0-23 で指定してください。", ephemeral=True)
        return
    async with _STATE_LOCK:
        s = _load_state()
        s["reminder"]["daily_hour"] = hour
        s["reminder"]["daily_last_sent_date"] = None
        _save_state_sync(s)
    desc = "毎日リマインドを停止しました" if hour == 0 else f"毎日リマインドを {hour} 時に変更しました"
    await _announce_reminder_change(inter, "リマインド設定変更", desc)
    await inter.response.send_message("変更を反映しました。", ephemeral=True)
    logger.info("reminder_daily user=%s hour=%d", inter.user, hour)


@tree.command(
    name="reminder_idle",
    description="持ち主のみ。無変化リマインドの時間を変更",
)
@app_commands.describe(hours="無変化リマインドする時間 (0-168、0 で停止)")
async def reminder_idle(inter: discord.Interaction, hours: int):
    state = _load_state()
    if state.get("holder_id") != inter.user.id:
        await inter.response.send_message(
            "現在の鍵の持ち主のみ変更できます。", ephemeral=True,
        )
        return
    if not (0 <= hours <= 168):
        await inter.response.send_message("hours は 0-168 で指定してください。", ephemeral=True)
        return
    async with _STATE_LOCK:
        s = _load_state()
        s["reminder"]["idle_hours"] = hours
        s["reminder"]["idle_last_sent_at"] = None
        _save_state_sync(s)
    desc = "無変化リマインドを停止しました" if hours == 0 else f"無変化リマインドを {hours} 時間に変更しました"
    await _announce_reminder_change(inter, "リマインド設定変更", desc)
    await inter.response.send_message("変更を反映しました。", ephemeral=True)
    logger.info("reminder_idle user=%s hours=%d", inter.user, hours)


@tree.command(
    name="reminder_status",
    description="リマインド設定と現在の状況を確認（自分にだけ表示）",
)
async def reminder_status(inter: discord.Interaction):
    """/reminder_status: 現在の鍵の状態とリマインド設定をまとめて表示（本人にだけ見える）"""
    state = _load_state()
    rem = state.get("reminder") or _default_reminder()
    now = datetime.now(get_localzone())

    embed = create_Embed("リマインド設定 / 状態")

    state_jp = {"closed": "閉まっている", "open": "開いている", "out": "持ち出し中"}
    embed.add_field(
        name="鍵の状態",
        value=state_jp.get(state.get("state"), "不明"),
        inline=True,
    )

    holder_id = state.get("holder_id")
    holder_text = f"<@{holder_id}>" if holder_id else "なし（返却済み）"
    embed.add_field(name="持ち主", value=holder_text, inline=True)

    if state.get("out_location"):
        embed.add_field(name="鍵の現在地", value=state["out_location"], inline=False)

    if state.get("last_change_at"):
        embed.add_field(
            name="最終操作時刻",
            value=_fmt_jst(state["last_change_at"]),
            inline=False,
        )

    # リマインド設定
    daily_hour = rem.get("daily_hour") or 0
    daily_text = "停止中" if daily_hour == 0 else f"毎日 {daily_hour} 時"
    embed.add_field(name="毎日リマインド (daily)", value=daily_text, inline=True)

    idle_hours = rem.get("idle_hours") or 0
    idle_text = "停止中" if idle_hours == 0 else f"{idle_hours} 時間 無変化で通知"
    embed.add_field(name="無変化リマインド (idle)", value=idle_text, inline=True)

    # 長期貸出の状況とリマインドへの影響
    ld = state.get("long_rent_until")
    if ld:
        lines = [f"最終日: **{ld}**"]
        if _is_before_long_rent_last_day(state):
            lines.append("→ daily: 最終日まで停止（最終日 20 時に通知予定）")
            lines.append("→ idle: 期間中ずっと停止")
        elif _is_long_rent_active(state):
            lines.append("→ daily: 本日（最終日）通知あり")
            lines.append("→ idle: 本日まで停止")
        else:
            lines.append("→ 期間終了（次のリマインドタイマーでクリアされます）")
        embed.add_field(name="長期貸出", value="\n".join(lines), inline=False)

    # 次回送信予定
    if holder_id:
        if daily_hour and not _is_before_long_rent_last_day(state):
            today_iso = now.date().isoformat()
            if rem.get("daily_last_sent_date") != today_iso and now.hour < daily_hour:
                next_daily = f"今日 {daily_hour} 時"
            else:
                tomorrow = now.date() + timedelta(days=1)
                next_daily = f"{tomorrow.isoformat()} {daily_hour} 時"
            embed.add_field(name="次回 daily 予定", value=next_daily, inline=True)

        if idle_hours and not _is_long_rent_active(state):
            last_change_dt = _parse_iso(state.get("last_change_at"))
            last_sent_dt = _parse_iso(rem.get("idle_last_sent_at"))
            candidates = [t for t in (last_change_dt, last_sent_dt) if t]
            if candidates:
                base = max(candidates)
                next_idle = (base + timedelta(hours=idle_hours)).astimezone(get_localzone())
                embed.add_field(
                    name="次回 idle 予定",
                    value=next_idle.strftime("%Y-%m-%d %H:%M"),
                    inline=True,
                )

    # 最後に送信した通知
    parts: list[str] = []
    if rem.get("daily_last_sent_date"):
        parts.append(f"daily: {rem['daily_last_sent_date']}")
    if rem.get("idle_last_sent_at"):
        parts.append(f"idle: {_fmt_jst(rem['idle_last_sent_at'])}")
    if parts:
        embed.add_field(name="最後に送信した通知", value="\n".join(parts), inline=False)

    # debug_friday は管理者にだけ表示
    if state.get("debug_friday") and _is_admin(inter.user.id):
        embed.add_field(name="🛠 debug_friday", value="ON（金曜扱い）", inline=False)

    await inter.response.send_message(embed=embed, ephemeral=True)


@tree.command(
    name="debug_friday",
    description="(管理者) 金曜モードのON/OFFを切り替え（土日貸出ボタン表示テスト）",
)
@app_commands.describe(on="True=金曜扱い、False=通常曜日に戻す")
async def debug_friday(inter: discord.Interaction, on: bool):
    """/debug_friday: 金曜モードのフラグを切り替える（テスト用、管理者のみ）"""
    if not _is_admin(inter.user.id):
        await inter.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    async with _STATE_LOCK:
        s = _load_state()
        s["debug_friday"] = bool(on)
        _save_state_sync(s)
    await inter.response.send_message(
        f"[debug] 金曜モードを {'ON' if on else 'OFF'} にしました。",
        ephemeral=True,
    )
    logger.info("debug_friday user=%s on=%s", inter.user, on)


@tree.command(
    name="debug_reminder",
    description="(管理者) リマインド通知を即時送信（state には反映しない）",
)
@app_commands.describe(type="daily=毎日通知、idle=無変化通知")
@app_commands.choices(type=[
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="idle", value="idle"),
])
async def debug_reminder(inter: discord.Interaction, type: app_commands.Choice[str]):
    """/debug_reminder: リマインドを試しに今すぐ送る（管理者のみ）"""
    if not _is_admin(inter.user.id):
        await inter.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    state = _load_state()
    holder_id = state.get("holder_id")
    if not holder_id:
        await inter.response.send_message(
            "現在の持ち主がいないので通知を送信しません。",
            ephemeral=True,
        )
        return
    channel_id = state.get("last_message_channel_id") or _KEY_CHANNEL_ID
    channel = client.get_channel(channel_id)
    if channel is None:
        await inter.response.send_message(
            "通知先チャンネルが見つかりません。",
            ephemeral=True,
        )
        return
    if type.value == "daily":
        await channel.send(
            f"[テスト] <@{holder_id}> 鍵を返し忘れていませんか？",
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    else:
        idle_hours = (state.get("reminder") or {}).get("idle_hours", _DEFAULT_IDLE_HOURS)
        await channel.send(
            f"[テスト] <@{holder_id}> {idle_hours}時間 状態に変化がありません。",
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    await inter.response.send_message("[debug] テスト通知を送信しました。", ephemeral=True)
    logger.info("debug_reminder user=%s type=%s", inter.user, type.value)


@client.event
async def on_interaction(inter: discord.Interaction):
    """ボタンクリックなどのインタラクションが届いたときに呼ばれる"""
    try:
        # component_type == 2 は「ボタンクリック」のみを意味する
        # Modal の submit などはここを素通りして、Modal.on_submit が呼ばれる
        if inter.data.get("component_type") != 2:
            return
        await on_button_click(inter)
    except discord.HTTPException as e:
        logger.warning("Discord HTTP error on interaction: %s", e)
    except Exception:
        logger.exception("on_interaction failed")
        if not inter.response.is_done():
            try:
                await inter.response.send_message(
                    "処理中にエラーが発生しました。しばらくしてから再度お試しください。",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass


async def on_button_click(inter: discord.Interaction):
    """ボタンの custom_id を見て、適切な処理に振り分ける"""
    custom_id = inter.data.get("custom_id")
    if not custom_id:
        return

    # 「取り消す」と「持ち出す」は特殊処理
    if custom_id == "undo":
        await _handle_undo(inter)
        return

    if custom_id == "room_out_open_modal":
        # Modal を表示して場所を入力してもらう
        try:
            await inter.response.send_modal(KeyOutModal())
        except discord.HTTPException as e:
            logger.warning("send_modal failed: %s", e)
        return

    # 通常のボタン操作は ACTIONS テーブルから設定を取って実行
    action_def = ACTIONS.get(custom_id)
    if action_def is None:
        # 古いメッセージの押されないはずのボタンや、未対応の custom_id
        logger.info("unknown custom_id=%s user=%s", custom_id, inter.user)
        if not inter.response.is_done():
            try:
                await inter.response.send_message(
                    "このボタンは使えません（古いメッセージのボタンか、未対応の操作です）。",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        return

    label, new_state, holder_strategy, view_key = action_def
    await _send_action_message(inter, custom_id, label, new_state, holder_strategy, view_key)


# --- Bot の起動 ---
# .env から TOKEN を読み、なければ起動を中止する
_token = os.getenv("TOKEN")
if not _token:
    raise SystemExit("環境変数 TOKEN が設定されていません。")

# Bot をログインして無限ループでイベントを待ち受ける
client.run(_token, log_level=logging.WARNING)
