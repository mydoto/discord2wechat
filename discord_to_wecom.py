#!/usr/bin/env python3
"""
Discord -> 企业微信（WeCom） 单向同步脚本

说明:
- 使用 discord.py (async) 监听消息并通过企业微信机器人 webhook 转发文本/附件链接。
- 支持带 secret 的企业微信 webhook 签名。
- 忽略机器人消息，支持按频道白名单过滤，自动裁剪超长消息。

环境变量 (示例请见 .env.example):
- DISCORD_TOKEN: Discord Bot Token
- WECHAT_WEBHOOK_URL: 企业微信机器人 webhook（例如: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx）
- WECHAT_WEBHOOK_SECRET: (可选) 如果你的机器人启用了签名机制，填写 secret
- ALLOWED_CHANNEL_IDS: (可选) 逗号分隔的 channel.id 列表，只有这些频道的消息才会被转发。留空则转发所有频道
- TRUNCATE_LENGTH: (可选) 最长消息长度，默认 6000
"""

import os
import time
import hmac
import hashlib
import base64
import asyncio
import logging
from urllib.parse import quote_plus

import aiohttp
import discord
from discord import Intents

# ---------- 配置 ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WECHAT_WEBHOOK_URL = os.getenv("WECHAT_WEBHOOK_URL")
WECHAT_WEBHOOK_SECRET = os.getenv("WECHAT_WEBHOOK_SECRET", "")
ALLOWED_CHANNEL_IDS = os.getenv("ALLOWED_CHANNEL_IDS", "")  # "123,456"
TRUNCATE_LENGTH = int(os.getenv("TRUNCATE_LENGTH", "6000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "8"))  # seconds

if not DISCORD_TOKEN or not WECHAT_WEBHOOK_URL:
    raise SystemExit("请设置 DISCORD_TOKEN 和 WECHAT_WEBHOOK_URL 环境变量（参见 .env.example）")

allowed_channel_ids = set()
if ALLOWED_CHANNEL_IDS.strip():
    for part in ALLOWED_CHANNEL_IDS.split(","):
        part = part.strip()
        if part:
            try:
                allowed_channel_ids.add(int(part))
            except ValueError:
                logging.warning("忽略无效 channel id: %s", part)

# ---------- 日志 ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- WeCom 签名工具 ----------
def build_wecom_signed_url(webhook_url: str, secret: str) -> str:
    """
    如果没有 secret，直接返回 webhook_url。
    如果有 secret，根据企业微信说明计算签名并返回带 timestamp 和 sign 的完整 URL。
    """
    if not secret:
        return webhook_url
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    # 对 sign 做 URL encode
    enc_sign = quote_plus(sign)
    # webhook_url 一般已经带 ?key=xxx，用 & 拼接额外参数
    return f"{webhook_url}&timestamp={timestamp}&sign={enc_sign}"

async def send_to_wecom(session: aiohttp.ClientSession, text: str):
    """
    发送文本消息到企业微信 webhook。
    """
    url = build_wecom_signed_url(WECHAT_WEBHOOK_URL, WECHAT_WEBHOOK_SECRET)
    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        async with session.post(url, json=payload, timeout=REQUEST_TIMEOUT) as resp:
            data = await resp.text()
            if resp.status != 200:
                logging.error("发送到企业微信失败: HTTP %s - %s", resp.status, data)
            else:
                # 企业微信会返回 JSON，200 但 code != 0 则表示业务失败
                try:
                    j = await resp.json()
                    if j.get("errcode", 0) != 0:
                        logging.error("企业微信返回错误: %s", j)
                except Exception:
                    # 不是 JSON，记录原文
                    logging.debug("企业微信返回非 JSON: %s", data)
    except asyncio.TimeoutError:
        logging.error("发送到企业微信超时")
    except Exception as e:
        logging.exception("发送到企业微信过程中发生异常: %s", e)

# ---------- Discord Bot ----------
intents = Intents.default()
# 需要启用 message content intent 才能读取消息内容
intents.message_content = True
intents.guilds = True
intents.messages = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    logging.info("Logged in as %s (id=%s)", client.user, client.user.id)

def format_message_content(message: discord.Message) -> str:
    """
    将 Discord 消息格式化为要发送到企业微信的文本。
    包含：服务器名、频道名、作者、正文、附件链接、embed 简要。
    """
    guild_name = message.guild.name if message.guild else "DM"
    channel_name = getattr(message.channel, "name", str(message.channel))
    author = f"{message.author.display_name}#{message.author.discriminator}"
    parts = []
    parts.append(f"[Discord] {guild_name} / {channel_name}")
    parts.append(f"{author}:")
    content = message.content or ""
    if content:
        parts.append(content)

    # 处理附件（文件/图片）: 将附件的 URL 附加在消息末尾
    if message.attachments:
        parts.append("附件:")
        for a in message.attachments:
            # 只附上 URL，企业微信会在消息中展示为链接
            parts.append(a.url)

    # 处理 embed（尽量保留 title/description/fields）
    if message.embeds:
        parts.append("嵌入内容:")
        for e in message.embeds:
            if e.title:
                parts.append(f"- 标题: {e.title}")
            if e.description:
                parts.append(f"- 描述: {e.description}")
            # 简要列出字段
            if e.fields:
                for f in e.fields:
                    parts.append(f"- {f.name}: {f.value}")

            # 如果 embed 有图片或缩略图，附加 URL
            if e.image and getattr(e.image, "url", None):
                parts.append(f"- image: {e.image.url}")
            if e.thumbnail and getattr(e.thumbnail, "url", None):
                parts.append(f"- thumbnail: {e.thumbnail.url}")

    text = "\n".join(parts).strip()
    # 裁剪超长消息（企业微信有长度限制，保守使用 TRUNCATE_LENGTH）
    if len(text) > TRUNCATE_LENGTH:
        text = text[: TRUNCATE_LENGTH - 3] + "..."
    return text

@client.event
async def on_message(message: discord.Message):
    # 忽略自己的消息与其他机器人消息
    if message.author.bot:
        return

    # 可选：按频道白名单过滤
    if allowed_channel_ids and getattr(message.channel, "id", None) not in allowed_channel_ids:
        return

    text = format_message_content(message)
    logging.info("Forwarding message from %s in %s -> wecom", message.author, getattr(message.channel, "name", message.channel))

    # 在异步环境中使用 aiohttp 客户端会话
    async with aiohttp.ClientSession() as session:
        await send_to_wecom(session, text)

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
