"""
鍵管理用のDiscordボット

概要:
- 部室の鍵の貸し借りを管理するためのDiscordボット
- ボタンを使用して以下の操作を実行できる:
  - 鍵を借りる
  - 部屋を開ける/閉める
  - 鍵を返す
  - 鍵を他の人に受け渡す
- 各操作は監査ログとして記録され、誰がいつ操作したかが分かる
- Embedを使用して見やすい表示を実現

主な機能:
1. 起動時に操作選択用のメッセージを表示
2. ボタンによるインタラクティブな操作
3. 操作履歴の記録と表示
4. 鍵の受け渡し機能

使用技術:
- discord.py - Discordボット開発用ライブラリ
- dotenv - 環境変数からトークンを読み込み
- datetime - タイムスタンプ管理
"""

# discord.pyをインポート
import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

from datetime import datetime
from tzlocal import get_localzone

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "key_bot.log")
_MAX_LOG_BYTES = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
_LOG_BACKUPS = int(os.getenv("LOG_BACKUP_COUNT", "5"))


def _setup_logging() -> logging.Logger:
    os.makedirs(_LOG_DIR, exist_ok=True)
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

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
_tree_synced = False
_startup_message_sent = False

def create_Embed(title):
    embed = discord.Embed(
        title= title,
        timestamp= datetime.now(),
        color=0x0000ff
        )
    return embed


def _embed_set_actor(embed: discord.Embed, user: discord.abc.User) -> None:
    """アイコンなしユーザでも落ちないように display_avatar を使う。"""
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)


#起動時に実行される関数
@client.event
async def on_ready():
    global _tree_synced, _startup_message_sent
    logger.info("on_ready user=%s discord.py=%s", client.user, discord.__version__)
    wait_sec = 60 - datetime.now(get_localzone()).second
    if wait_sec > 0:
        logger.info("aligning to minute boundary: sleep %.1fs (async)", wait_sec)
        await asyncio.sleep(wait_sec)

    channel = client.get_channel(1100228900501594179)
    if channel is None:
        logger.error("channel 1100228900501594179 not found (missing intent or wrong id)")
        return

    embed = discord.Embed(
        title="on ready",
        color=0x0000FF,
        description="操作を選択してください",
    )
    button_rent = discord.ui.Button(
        label="借りる",
        style=discord.ButtonStyle.success,
        custom_id="key_rent",
    )
    view = discord.ui.View()
    view.add_item(button_rent)

    if not _startup_message_sent:
        await channel.send(embed=embed, view=view)
        _startup_message_sent = True

    if not _tree_synced:
        await tree.sync()
        _tree_synced = True
        logger.info("application commands synced")
    

#イントラクションを読み込むための関数
@client.event
async def on_interaction(inter: discord.Interaction):
    try:
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


#ボタンが押された時の関数
async def on_button_click(inter: discord.Interaction):
    custom_id = inter.data.get("custom_id")
    if not custom_id:
        return
    view = discord.ui.View()
    
    #鍵を借りた時
    if custom_id == 'key_rent':
        embed = create_Embed("借りました")
        _embed_set_actor(embed, inter.user)
        
        #部屋を開けるボタン
        key_open = discord.ui.Button(
            label='開ける',
            style=discord.ButtonStyle.success,
            custom_id='room_open'
        )
        view.add_item(key_open)
        
        #鍵を返すボタン
        key_return = discord.ui.Button(
            label='返す',
            style=discord.ButtonStyle.danger,
            custom_id='key_return'
        )
        view.add_item(key_return)
        
        #鍵を受け取るボタン
        key_pass = discord.ui.Button(
            label='受け取る',
            style=discord.ButtonStyle.primary,
            custom_id='key_pass_rent'
        )
        view.add_item(key_pass)
        
        await inter.response.send_message(embed=embed, view=view)
    
    #部屋を開けた時
    elif custom_id == 'room_open':
        embed = create_Embed('開けました')
        _embed_set_actor(embed, inter.user)
        
        #部屋を閉めるボタン
        key_close = discord.ui.Button(
            label='閉める',
            style=discord.ButtonStyle.success,
            custom_id='room_close'
        )
        view.add_item(key_close)
        
        #鍵を渡すボタン
        key_pass = discord.ui.Button(
            label='受け取る',
            style=discord.ButtonStyle.primary,
            custom_id='key_pass_open'
        )
        view.add_item(key_pass)
        
        await inter.response.send_message(embed=embed, view=view)
    
    #部屋を閉めた時
    elif custom_id == 'room_close':
        embed = create_Embed('閉めました')
        _embed_set_actor(embed, inter.user)
        
        #部屋を開けるボタン
        key_open = discord.ui.Button(
            label='開ける',
            style=discord.ButtonStyle.success,
            custom_id='room_open'
        )
        view.add_item(key_open)
        
        #鍵を返すボタン
        key_return = discord.ui.Button(
            label='返す',
            style=discord.ButtonStyle.danger,
            custom_id='key_return'
        )
        view.add_item(key_return)
        
        #鍵を渡すボタン
        key_pass = discord.ui.Button(
            label='受け取る',
            style=discord.ButtonStyle.primary,
            custom_id='key_pass_close'
        )
        view.add_item(key_pass)
        
        
        await inter.response.send_message(embed=embed, view=view)
    
    #鍵を返す時
    elif custom_id == 'key_return':
        embed = create_Embed('返しました')
        _embed_set_actor(embed, inter.user)
        
        #鍵を借りるボタン
        key_rent = discord.ui.Button(
            label='借りる',
            style=discord.ButtonStyle.success,
            custom_id='key_rent'
        )
        view.add_item(key_rent)
        
        await inter.response.send_message(embed=embed, view=view)
        
    #鍵を渡した時、前の操作が鍵を借りた場合
    elif custom_id=='key_pass_rent':
        embed = create_Embed('受け取りました')
        _embed_set_actor(embed, inter.user)
        
        #部屋を開けるボタン
        key_open = discord.ui.Button(
            label='開ける',
            style=discord.ButtonStyle.success,
            custom_id='room_open'
        )
        view.add_item(key_open)
        
        #鍵を返すボタン
        key_return = discord.ui.Button(
            label='返す',
            style=discord.ButtonStyle.danger,
            custom_id='key_return'
        )
        view.add_item(key_return)
        
        #鍵を渡すボタン
        key_pass = discord.ui.Button(
            label='受け取る',
            style=discord.ButtonStyle.primary,
            custom_id='key_pass_rent'
        )
        view.add_item(key_pass)
        
        await inter.response.send_message(embed=embed, view=view)
    
    #鍵を渡す時、前の操作が部屋を開けた場合
    elif custom_id == 'key_pass_open':
        embed = create_Embed('受け取りました')
        _embed_set_actor(embed, inter.user)
        
        #部屋を閉めるボタン
        key_close = discord.ui.Button(
            label='閉める',
            style=discord.ButtonStyle.success,
            custom_id='room_close'
        )
        view.add_item(key_close)
        
        #鍵を渡すボタン
        key_pass = discord.ui.Button(
            label='受け取る',
            style=discord.ButtonStyle.primary,
            custom_id='key_pass_open'
        )
        view.add_item(key_pass)
        
        await inter.response.send_message(embed=embed, view=view)
    
    #鍵を渡す時、前の操作が部屋を占めた場合
    elif custom_id == 'key_pass_close':
        embed = create_Embed('受け取りました')
        _embed_set_actor(embed, inter.user)
        
        #部屋を開けるボタン
        key_open = discord.ui.Button(
            label='開ける',
            style=discord.ButtonStyle.success,
            custom_id='room_open'
        )
        view.add_item(key_open)
        
        #鍵を返すボタン
        key_return = discord.ui.Button(
            label='返す',
            style=discord.ButtonStyle.danger,
            custom_id='key_return'
        )
        view.add_item(key_return)
        
        #鍵を渡すボタン
        key_pass = discord.ui.Button(
            label='受け取る',
            style=discord.ButtonStyle.primary,
            custom_id='key_pass_close'
        )
        view.add_item(key_pass)
        
        await inter.response.send_message(embed=embed, view=view)

    else:
        logger.info("unknown custom_id=%s user=%s", custom_id, inter.user)
        if not inter.response.is_done():
            await inter.response.send_message(
                "このボタンは使えません（古いメッセージのボタンか、未対応の操作です）。",
                ephemeral=True,
            )


#ボット起動のためのコード
_token = os.getenv("TOKEN")
if not _token:
    raise SystemExit("環境変数 TOKEN が設定されていません。")

client.run(_token, log_level=logging.WARNING)
