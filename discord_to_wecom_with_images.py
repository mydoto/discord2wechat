#!/usr/bin/env python3
"""
Discord -> 企业微信（WeCom） 单向同步脚本（支持把图片附件以原图发送到企业微信 webhook）

环境变量:
- DISCORD_TOKEN (必填)
- WECHAT_WEBHOOK_URL (必填)
- WECHAT_WEBHOOK_SECRET (可选)
- ALLOWED_CHANNEL_IDS (可选, 逗号分隔)
- TRUNCATE_LENGTH (可选, 默认6000)
- MAX_IMAGE_BYTES (可选, 最大发送为 image 的字节阈值，默认 5MB)
- SEND_DELAY_SECONDS (可选, 多附件间短暂 sleep，默认 0.5s)
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
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))  # 5 MB 默认
SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "0.5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12"))  # seconds

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
    if not secret:
        return webhook_url
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    enc_sign = quote_plus(sign)
    return f"{webhook_url}&timestamp={timestamp}&sign={enc_sign}"

async def post_json(session: aiohttp.ClientSession, url: str, payload: dict):
    try:
        async with session.post(url, json=payload, timeout=REQUEST_TIMEOUT) as resp:
            text = await resp.text()
            if resp.status != 200:
                logging.error("WeCom HTTP error %s: %s", resp.status, text)
                return False, text
            try:
                j = await resp.json()
                if j.get("errcode", 0) != 0:
                    logging.error("WeCom API error: %s", j)
                    return False, j
            except Exception:
                logging.debug("WeCom returned non-JSON: %s", text)
            return True, text
    except asyncio.TimeoutError:
        logging.error("WeCom request timeout")
        return False, "timeout"
    except Exception as e:
        logging.exception("WeCom request exception: %s", e)
        return False, str(e)

async def send_text_to_wecom(session: aiohttp.ClientSession, text: str):
    url = build_wecom_signed_url(WECHAT_WEBHOOK_URL, WECHAT_WEBHOOK_SECRET)
    payload = {"msgtype": "text", "text": {"content": text}}
    return await post_json(session, url, payload)

async def send_image_to_wecom(session: aiohttp.ClientSession, image_bytes: bytes):
    """
    把图片 bytes 转为 base64 + md5 并发送为 image msgtype。
    md5 使用 hex 小写格式（企业微信要求）。
    """
    if len(image_bytes) > MAX_IMAGE_BYTES:
        logging.warning("图片尺寸 %s 超过 MAX_IMAGE_BYTES (%s), 不作为 image 发送", len(image_bytes), MAX_IMAGE_BYTES)
        return False, "too_large"
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    md5_hex = hashlib.md5(image_bytes).hexdigest()
    url = build_wecom_signed_url(WECHAT_WEBHOOK_URL, WECHAT_WEBHOOK_SECRET)
    payload = {"msgtype": "image", "image": {"base64": b64, "md5": md5_hex}}
    return await post_json(session, url, payload)

# ---------- Discord Bot ----------
intents = Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

client = discord.Client(intents=intents)

def is_image_attachment(attachment: discord.Attachment) -> bool:
    # 优先检查 content_type，如果没有则看扩展名
    if attachment.content_type:
        return attachment.content_type.startswith("image")
    lower = attachment.filename.lower() if attachment.filename else ""
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        if lower.endswith(ext):
            return True
    return False

def format_message_content(message: discord.Message) -> str:
    guild_name = message.guild.name if message.guild else "DM"
    channel_name = getattr(message.channel, "name", str(message.channel))
    author = f"{message.author.display_name}#{message.author.discriminator}"
    parts = []
    parts.append(f"[Discord] {guild_name} / {channel_name}")
    parts.append(f"{author}:")
    content = message.content or ""
    if content:
        parts.append(content)

    # 附件 URL（非 image 或 超大 image 会保留 URL）
    other_urls = []
    for a in message.attachments:
        if not is_image_attachment(a):
            other_urls.append(a.url)
    if other_urls:
        parts.append("附件链接:")
        parts.extend(other_urls)

    # Embeds 简要
    if message.embeds:
        parts.append("嵌入内容:")
        for e in message.embeds:
            if e.title:
                parts.append(f"- 标题: {e.title}")
            if e.description:
                parts.append(f"- 描述: {e.description}")
            if e.url:
                parts.append(f"- url: {e.url}")
    text = "\n".join(parts).strip()
    if len(text) > TRUNCATE_LENGTH:
        text = text[: TRUNCATE_LENGTH - 3] + "..."
    return text

@client.event
async def on_ready():
    logging.info("Logged in as %s (id=%s)", client.user, client.user.id)

@client.event
async def on_message(message: discord.Message):
    # 忽略机器人的消息
    if message.author.bot:
        return

    # 频道白名单
    if allowed_channel_ids and getattr(message.channel, "id", None) not in allowed_channel_ids:
        return

    text = format_message_content(message)
    logging.info("Forwarding message from %s in %s -> wecom", message.author, getattr(message.channel, "name", message.channel))

    async with aiohttp.ClientSession() as session:
        # 先发送文本（包含作者、正文、非图片附件链接、embed 信息）
        ok, _ = await send_text_to_wecom(session, text)
        if not ok:
            logging.warning("发送文本到企业微信失败，继续尝试发送附件（如果有）")

        # 处理附件：把图片下载并以原图形式发送；非图片已作为链接包含在文本
        for attachment in message.attachments:
            if is_image_attachment(attachment):
                try:
                    logging.info("Downloading attachment %s", attachment.url)
                    async with session.get(attachment.url, timeout=REQUEST_TIMEOUT) as resp:
                        if resp.status != 200:
                            logging.warning("下载附件失败 %s: HTTP %s", attachment.url, resp.status)
                            continue
                        data = await resp.read()
                except asyncio.TimeoutError:
                    logging.warning("下载附件超时: %s", attachment.url)
                    continue
                except Exception as e:
                    logging.exception("下载附件异常: %s", e)
                    continue

                # 如果图片过大，退回到发送 URL（或你可选择缩放后再发送）
                if len(data) > MAX_IMAGE_BYTES:
                    logging.warning("图片过大 (%s bytes), 附上 URL 而非原图", len(data))
                    # 发送一条包含文件名 + URL 的文本
                    fallback_text = f"附件（过大，未发送原图）: {attachment.filename or 'file'}\n{attachment.url}"
                    await send_text_to_wecom(session, fallback_text)
                else:
                    ok, resp = await send_image_to_wecom(session, data)
                    if not ok:
                        logging.warning("以 image 发送失败，改为发送 URL: %s", attachment.url)
                        await send_text_to_wecom(session, f"附件: {attachment.filename or 'file'}\n{attachment.url}")
                # 小延迟，避免短时间内大量请求触发速率限制
                await asyncio.sleep(SEND_DELAY_SECONDS)

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
