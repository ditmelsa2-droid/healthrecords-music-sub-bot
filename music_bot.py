import os
import sys
import asyncio
import logging
import urllib.parse
import urllib.request
import json
import re
import datetime
import shutil
import aiohttp
from aiohttp import web as aiohttp_web
import discord
from discord import app_commands
from discord.ext import commands, tasks
import yt_dlp
import static_ffmpeg

import config

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("DeaHades.MusicSubBot")

# ==================== CLOUDFLARE PROXY SHIELD ====================
if getattr(config, "USE_DISCORD_PROXY", False) and getattr(config, "DISCORD_PROXY_URL", None):
    import discord.http
    discord.http.Route.BASE = config.DISCORD_PROXY_URL
    logger.info(f"🛡️ KÍCH HOẠT CLOUDFLARE PROXY SHIELD: {config.DISCORD_PROXY_URL}")

# ==================== STATIC FFMPEG CONFIG ====================
try:
    static_ffmpeg.add_paths()
    DEFAULT_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    logger.info(f"🎬 FFmpeg sẵn sàng tại: {DEFAULT_FFMPEG}")
except Exception as e:
    logger.warning(f"Lỗi khởi tạo static-ffmpeg: {e}")
    DEFAULT_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# ==================== YTDL CONFIG ====================
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch1:',
    'extractor_args': {
        'youtube': {
            'player_client': ['web_embedded', 'android', 'mweb']
        }
    }
}

COOKIE_PATH = os.path.join("data", "cookies.txt")

def setup_youtube_cookies():
    os.makedirs("data", exist_ok=True)
    b64_cookie = os.environ.get("YOUTUBE_COOKIES_BASE64", "").strip()
    if b64_cookie:
        try:
            import base64
            raw = base64.b64decode(b64_cookie).decode("utf-8")
            with open(COOKIE_PATH, "w", encoding="utf-8") as f:
                f.write(raw)
            logger.info("🍪 Đã nạp YOUTUBE_COOKIES_BASE64 vào data/cookies.txt!")
        except Exception as e:
            logger.warning(f"Không thể giải mã YOUTUBE_COOKIES_BASE64: {e}")

setup_youtube_cookies()

def get_ytdl_instance(use_cookies: bool = True):
    opts = dict(YTDL_OPTIONS)
    if use_cookies and os.path.exists(COOKIE_PATH) and os.path.getsize(COOKIE_PATH) > 100:
        opts['cookiefile'] = COOKIE_PATH
    return yt_dlp.YoutubeDL(opts)

ytdl = get_ytdl_instance(use_cookies=True)
ytdl_nocookie = get_ytdl_instance(use_cookies=False)

# Monstercat Instinct Vol. 6 (Album Mix) 24/7
MONSTERCAT_SC_STREAM = "https://soundcloud.com/spookstervibes/monstercat-instinct-vol-6-album-mix"
MONSTERCAT_YT_STREAM = "https://www.youtube.com/watch?v=_RTy21niS0g"
IDLE_MUSIC_TITLE = "Monstercat Instinct Vol. 6 (Album Mix) 24/7"

# ==================== KEEP-ALIVE WEB SERVER ====================
async def handle_ping(request):
    return aiohttp_web.Response(
        text="🎵 HEALTH RECORDS Music Sub-Bot is ONLINE & STREAMING 24/7! 🗿🍷",
        content_type="text/plain",
        charset="utf-8"
    )

async def handle_status(request):
    guild = bot.get_guild(config.OFFICIAL_GUILD_ID)
    vc = guild.voice_client if guild else None
    return aiohttp_web.json_response({
        "status": "online",
        "vc_connected": vc.is_connected() if vc else False,
        "vc_playing": vc.is_playing() if vc else False,
        "vc_channel": vc.channel.name if (vc and vc.channel) else None,
        "idle_active": bot.idle_active.get(guild.id, False) if guild else False,
        "now_playing": bot.now_playing.get(guild.id, {}).get("title", "None") if guild else "None",
        "queue_len": len(bot.queues.get(guild.id, [])) if guild else 0
    })

async def start_web_server():
    app = aiohttp_web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/status', handle_status)
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = aiohttp_web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🚀 Web Server giữ Sub-Bot 24/7 đã lắng nghe trên cổng {port}")

# ==================== MUSIC BOT CLASS ====================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

class MusicSubBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )
        self.queues = {}          # guild_id -> list of track dicts
        self.now_playing = {}     # guild_id -> track dict
        self.idle_active = {}     # guild_id -> bool
        self.volumes = {}         # guild_id -> float (0.0 to 2.0)
        self.lock = asyncio.Lock()

    async def setup_hook(self):
        await start_web_server()
        try:
            guild_obj = discord.Object(id=config.OFFICIAL_GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            await self.tree.sync()
            logger.info("⚡ Đã đồng bộ toàn bộ Slash Commands Âm Nhạc.")
        except Exception as e:
            logger.error(f"Lỗi sync commands: {e}")

bot = MusicSubBot()

# ==================== AUDIO EXTRACTION ENGINE ====================
def fetch_youtube_oembed_title(url: str) -> tuple[str, str]:
    """Lấy tiêu đề YouTube an toàn 100% qua oEmbed API không bao giờ bị chặn IP."""
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json",
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return data.get('title', ''), data.get('author_name', '')
    except Exception:
        return '', ''

async def extract_audio_info(query: str) -> dict | None:
    loop = asyncio.get_event_loop()
    is_yt_link = "youtube.com" in query or "youtu.be" in query

    clean_yt_url = query
    oembed_title = ""
    oembed_author = ""

    # Làm sạch URL nếu là link YouTube
    if is_yt_link:
        parsed = urllib.parse.urlparse(query)
        qs = urllib.parse.parse_qs(parsed.query)
        if "watch" in parsed.path and "v" in qs:
            clean_yt_url = f"https://www.youtube.com/watch?v={qs['v'][0]}"
        elif "youtu.be" in parsed.netloc:
            vid = parsed.path.strip("/").split("?")[0]
            clean_yt_url = f"https://www.youtube.com/watch?v={vid}"

        oembed_title, oembed_author = await loop.run_in_executor(None, lambda: fetch_youtube_oembed_title(clean_yt_url))

    # TẦNG 1: Trích xuất YouTube qua yt-dlp
    if is_yt_link:
        try:
            data = await loop.run_in_executor(None, lambda: ytdl_nocookie.extract_info(clean_yt_url, download=False))
            if data:
                audio_url = data.get('url')
                headers = data.get('http_headers', {})
                if not audio_url and 'formats' in data:
                    formats = [f for f in data['formats'] if f.get('acodec') != 'none']
                    if formats:
                        formats.sort(key=lambda f: f.get('abr') or 0, reverse=True)
                        audio_url = formats[0].get('url')
                        if formats[0].get('http_headers'):
                            headers = formats[0].get('http_headers')

                if audio_url:
                    return {
                        'title': data.get('title', oembed_title or 'Unknown Title'),
                        'url': audio_url,
                        'headers': headers,
                        'webpage_url': data.get('webpage_url', clean_yt_url),
                        'duration': data.get('duration', 0),
                        'thumbnail': data.get('thumbnail'),
                        'uploader': data.get('uploader', oembed_author or 'YouTube'),
                        'is_idle': False
                    }
        except Exception as e:
            logger.warning(f"Tầng 1 YouTube bị chặn ({e}). Tự động kích hoạt Cứu hộ SoundCloud HQ...")

    # TẦNG 2: Cứu hộ tự động qua SoundCloud HQ (Không chặn IP Datacenter)
    sc_query_term = oembed_title if oembed_title else re.sub(r'https?://[^\s]+', '', query).strip()
    if not sc_query_term:
        sc_query_term = query.split("/")[-1].replace("-", " ")

    if "soundcloud.com" in query:
        sc_target = query
    else:
        sc_target = f"scsearch5:{sc_query_term}"

    logger.info(f"🔄 Kích hoạt Tầng 2 SoundCloud HQ: {sc_target}")
    try:
        data = await loop.run_in_executor(None, lambda: ytdl_nocookie.extract_info(sc_target, download=False))
        if data:
            entry = data['entries'][0] if 'entries' in data and data['entries'] else data
            audio_url = entry.get('url')
            if audio_url:
                title = entry.get('title', oembed_title or sc_query_term)
                logger.info(f"✅ Tầng 2 cứu hộ thành công: {title}")
                return {
                    'title': f"{title} [HQ Stream]",
                    'url': audio_url,
                    'headers': entry.get('http_headers', {}),
                    'webpage_url': entry.get('webpage_url', clean_yt_url),
                    'duration': entry.get('duration', 0),
                    'thumbnail': entry.get('thumbnail'),
                    'uploader': entry.get('uploader', 'SoundCloud'),
                    'is_idle': False
                }
    except Exception as e:
        logger.error(f"Tầng 2 thất bại: {e}")

    return None

async def extract_idle_stream() -> dict | None:
    loop = asyncio.get_event_loop()
    # 1. Thử stream SoundCloud HQ của Monstercat Instinct Vol. 6
    try:
        data = await loop.run_in_executor(None, lambda: ytdl_nocookie.extract_info(MONSTERCAT_SC_STREAM, download=False))
        if data and data.get('url'):
            return {
                'title': IDLE_MUSIC_TITLE,
                'url': data.get('url'),
                'headers': data.get('http_headers', {}),
                'webpage_url': MONSTERCAT_YT_STREAM,
                'duration': data.get('duration', 7712),
                'thumbnail': data.get('thumbnail'),
                'uploader': 'Monstercat',
                'is_idle': True
            }
    except Exception as e:
        logger.warning(f"SoundCloud Monstercat idle thất bại: {e}")

    # 2. Thử dự phòng YouTube URL
    res = await extract_audio_info(MONSTERCAT_YT_STREAM)
    if res:
        res['title'] = IDLE_MUSIC_TITLE
        res['is_idle'] = True
        return res

    return None

def create_pcm_source(track: dict, volume: float = 1.0) -> discord.PCMVolumeTransformer:
    headers = track.get('headers', {})
    header_str = "".join([f"{k}: {v}\r\n" for k, v in headers.items()]) if headers else ""
    is_m3u8 = ".m3u8" in track['url'] or "playlist.m3u8" in track['url']

    if header_str and not is_m3u8:
        before_opts = f'-headers "{header_str}" -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
    elif not is_m3u8:
        before_opts = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
    else:
        before_opts = None  # HLS playlist m3u8 tự xử lý segment reconnection

    audio = discord.FFmpegPCMAudio(
        track['url'],
        executable=DEFAULT_FFMPEG,
        before_options=before_opts,
        options='-vn'
    )
    return discord.PCMVolumeTransformer(audio, volume=volume)

# ==================== VOICE & PLAYBACK CORE (LOCKED TO CUSTOM MUSIC) ====================
async def get_or_join_voice_client(guild: discord.Guild) -> discord.VoiceClient | None:
    """Đảm bảo bot luôn luôn kết nối vào phòng DUY NHẤT: 🎵・Custom Music."""
    target_channel = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
    if not target_channel:
        target_channel = discord.utils.find(lambda c: "custom music" in c.name.lower() or "nhạc" in c.name.lower(), guild.voice_channels)

    if not target_channel:
        return None

    # Nếu bot đã kết nối voice trong server:
    if guild.voice_client and guild.voice_client.is_connected():
        vc = guild.voice_client
        if vc.channel.id != target_channel.id:
            logger.info(f"🔄 Đưa bot về cố định tại phòng [{target_channel.name}]")
            await vc.move_to(target_channel)
        return vc

    # Kết nối mới
    try:
        vc = await target_channel.connect(reconnect=True, timeout=20.0)
        return vc
    except Exception as e:
        logger.error(f"Lỗi kết nối phòng voice: {e}")
        return None

def play_next(guild: discord.Guild):
    if not guild or not guild.voice_client:
        return

    vc = guild.voice_client
    if not vc.is_connected():
        return

    q = bot.queues.get(guild.id, [])
    if q:
        track = q.pop(0)
        bot.now_playing[guild.id] = track
        bot.idle_active[guild.id] = False

        vol = bot.volumes.get(guild.id, 1.0)
        try:
            source = create_pcm_source(track, volume=vol)
        except Exception as e:
            logger.error(f"Lỗi tạo audio source: {e}")
            bot.loop.call_later(2, lambda: play_next(guild))
            return

        def after_callback(err):
            if err:
                logger.error(f"Playback error: {err}")
            asyncio.run_coroutine_threadsafe(handle_after_playback(guild), bot.loop)

        vc.play(source, after=after_callback)
        logger.info(f"🎶 Đang phát: {track['title']} ({vol*100:.0f}% vol)")
    else:
        # Hàng chờ rỗng -> Bật nhạc nền Monstercat IDLE 24/7
        asyncio.run_coroutine_threadsafe(start_idle_music(guild), bot.loop)

async def handle_after_playback(guild: discord.Guild):
    await asyncio.sleep(1)
    play_next(guild)

async def start_idle_music(guild: discord.Guild):
    if not guild or not guild.voice_client:
        return

    vc = guild.voice_client
    if not vc.is_connected() or vc.is_playing():
        return

    logger.info(f"🎧 Kích hoạt Monstercat Instinct IDLE 24/7 tại {guild.name}...")
    track = await extract_idle_stream()
    if not track:
        logger.warning("Không thể lấy stream Monstercat IDLE. Thử lại sau 15s...")
        await asyncio.sleep(15)
        if not vc.is_playing():
            play_next(guild)
        return

    bot.now_playing[guild.id] = track
    bot.idle_active[guild.id] = True

    vol = bot.volumes.get(guild.id, 0.8) # Nhạc nền 80%
    try:
        source = create_pcm_source(track, volume=vol)
    except Exception as e:
        logger.error(f"Lỗi tạo idle audio source: {e}")
        return

    def after_idle(err):
        if err:
            logger.warning(f"Idle stream ended: {err}")
        asyncio.run_coroutine_threadsafe(handle_after_playback(guild), bot.loop)

    vc.play(source, after=after_idle)
    logger.info("✅ Monstercat Instinct IDLE 24/7 đang ngân vang.")

# ==================== DỰNG BÀI VÀ PHÁT NHẠC CHUNG (DÙNG CHO CẢ SLASH VÀ CHAT MESSAGE) ====================
async def process_and_play(send_target, query: str, requester: discord.User | discord.Member):
    guild = send_target.guild if hasattr(send_target, "guild") else None
    if not guild:
        return

    vc = await get_or_join_voice_client(guild)
    if not vc:
        if hasattr(send_target, "send"):
            await send_target.send("❌ Bot không thể kết nối tới phòng `🎵・Custom Music`!", ephemeral=True)
        return

    # Thông báo trạng thái đang tải
    status_msg = None
    if hasattr(send_target, "send"):
        try:
            status_msg = await send_target.send(f"🔎 Đang nạp bài hát: `{query[:60]}...`")
        except Exception:
            pass

    track = await extract_audio_info(query)
    if not track:
        err_text = f"❌ Không thể tìm thấy hoặc trích xuất âm thanh từ: `{query}`"
        if status_msg:
            await status_msg.edit(content=err_text)
        elif hasattr(send_target, "send"):
            await send_target.send(err_text, ephemeral=True)
        return

    q = bot.queues.setdefault(guild.id, [])
    track['requester'] = requester.display_name

    embed = discord.Embed(
        title="🎵 THÊM BÀI HÁT THÀNH CÔNG",
        description=f"[{track['title']}]({track.get('webpage_url', '')})\n\n"
                    f"⏱️ **Thời lượng:** `{datetime.timedelta(seconds=track['duration']) if track['duration'] else 'Livestream'}`\n"
                    f"🔊 **Kênh:** `🎵・Custom Music`\n"
                    f"👤 **Yêu cầu bởi:** {requester.mention}",
        color=config.COLOR_SUCCESS
    )
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])

    # Nếu đang phát nhạc nền IDLE hoặc không phát gì -> phát ngay bài mới!
    if bot.idle_active.get(guild.id, False) or not vc.is_playing():
        if vc.is_playing():
            vc.stop()
        q.insert(0, track)
        play_next(guild)
        embed.title = "🎶 ĐANG PHÁT BÀI HÁT"
        if status_msg:
            await status_msg.edit(content=None, embed=embed)
        else:
            await send_target.send(embed=embed)
    else:
        q.append(track)
        embed.set_footer(text=f"Vị trí trong hàng chờ: #{len(q)}")
        if status_msg:
            await status_msg.edit(content=None, embed=embed)
        else:
            await send_target.send(embed=embed)

# ==================== 24/7 VOICE MONITOR TASK ====================
@tasks.loop(seconds=30)
async def voice_health_check():
    guild = bot.get_guild(config.OFFICIAL_GUILD_ID)
    if not guild:
        return

    vc = guild.voice_client
    if not vc or not vc.is_connected():
        logger.info("🔄 Tự động kết nối cố định vào phòng 🎵・Custom Music...")
        vc = await get_or_join_voice_client(guild)
        if vc and not vc.is_playing():
            await start_idle_music(guild)
    else:
        # Nếu đang ở khác phòng Custom Music, đưa về Custom Music
        if vc.channel.id != config.CUSTOM_MUSIC_CHANNEL_ID:
            base_ch = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
            if base_ch:
                await vc.move_to(base_ch)

        # Nếu không phát bài nào và không pause -> Tiếp tục phát
        if not vc.is_playing() and not vc.is_paused():
            play_next(guild)

@bot.event
async def on_ready():
    logger.info(f"👑 MUSIC SUB-BOT ĐÃ SẴN SÀNG: {bot.user.name}#{bot.user.discriminator} (ID: {bot.user.id})")
    if not voice_health_check.is_running():
        voice_health_check.start()

# ==================== CHAT MESSAGE LISTENER (AUTO-PLAY ON PASTE) ====================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content:
        return

    # Nhận diện nếu chat trong kênh âm nhạc hoặc mention bot
    is_music_channel = (
        message.channel.id == config.CUSTOM_MUSIC_CHANNEL_ID or
        "custom-music" in getattr(message.channel, "name", "").lower() or
        "music" in getattr(message.channel, "name", "").lower()
    )
    is_mentioned = bot.user.mentioned_in(message)

    # 1. Các lệnh text nhanh: skip, stop, queue, np
    cmd = content.lower()
    if cmd in ("!skip", "skip", "next"):
        await do_skip(message.channel)
        return
    elif cmd in ("!stop", "stop"):
        await do_stop(message.channel)
        return
    elif cmd in ("!queue", "queue", "hangcho"):
        await do_queue(message.channel)
        return
    elif cmd in ("!np", "np", "nowplaying"):
        await do_np(message.channel)
        return

    # 2. Nhận diện link bài hát hoặc lệnh play
    url_match = re.search(r'(https?://[^\s]+)', content)
    is_play_cmd = cmd.startswith("!play ") or cmd.startswith("play ")

    query = None
    if url_match:
        query = url_match.group(1)
    elif is_play_cmd:
        query = content.split(" ", 1)[1].strip()
    elif is_music_channel and len(content) > 2 and not content.startswith("/"):
        # Trong kênh chat âm nhạc, người dùng gửi bất kỳ tên bài hát nào cũng tự động tìm và phát!
        query = content

    if query:
        try:
            await message.add_reaction("🎵")
        except Exception:
            pass
        await process_and_play(message.channel, query, message.author)

# ==================== TEXT & SLASH ACTION HELPERS ====================
async def do_skip(send_target):
    guild = send_target.guild
    vc = guild.voice_client if guild else None
    if vc and vc.is_playing():
        vc.stop()
        await send_target.send("⏭️ Đã bỏ qua bài hát hiện tại! Đang chuyển tiếp... 🗿🍷")
    else:
        await send_target.send("❌ Hiện không có bài hát nào đang phát để skip.", delete_after=5)

async def do_stop(send_target):
    guild = send_target.guild
    bot.queues[guild.id] = []
    vc = guild.voice_client if guild else None
    if vc and vc.is_playing():
        vc.stop()
    await send_target.send("⏹️ Đã dừng phát nhạc, dọn sạch hàng chờ và khôi phục Monstercat 24/7! 🎧")
    await start_idle_music(guild)

async def do_queue(send_target):
    guild = send_target.guild
    q = bot.queues.get(guild.id, [])
    if not q:
        await send_target.send("📜 Hàng chờ hiện đang trống. Bot đang phát nhạc Monstercat 24/7! 🐱")
        return

    desc = ""
    for idx, t in enumerate(q[:10], 1):
        dur = str(datetime.timedelta(seconds=t['duration'])) if t.get('duration') else "Live"
        desc += f"`#{idx}` **{t['title'][:45]}** — `{dur}` *(bởi {t.get('requester', 'Ẩn danh')})*\n"

    embed = discord.Embed(
        title=f"📜 DANH SÁCH BÀI HÁT ĐANG CHỜ ({len(q)} bài)",
        description=desc,
        color=config.COLOR_CYAN
    )
    await send_target.send(embed=embed)

async def do_np(send_target):
    guild = send_target.guild
    track = bot.now_playing.get(guild.id)
    if not track:
        await send_target.send("❌ Hiện tại chưa có bài hát nào được phát.")
        return

    is_idle = bot.idle_active.get(guild.id, False)
    status_text = "🐱 **Nhạc Nền 24/7 (Monstercat Instinct)**" if is_idle else "🎶 **Đang Phát (Thành Viên Yêu Cầu)**"

    dur = str(datetime.timedelta(seconds=track['duration'])) if track.get('duration') else "Livestream"
    embed = discord.Embed(
        title="🎧 BÀI HÁT ĐANG PHÁT",
        description=f"{status_text}\n\n"
                    f"📌 **Tên:** [{track['title']}]({track.get('webpage_url', '')})\n"
                    f"⏱️ **Thời lượng:** `{dur}`\n"
                    f"🔊 **Âm lượng:** `{int(bot.volumes.get(guild.id, 1.0) * 100)}%`",
        color=config.COLOR_GOLD
    )
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    await send_target.send(embed=embed)

# ==================== SLASH COMMANDS ====================
@bot.tree.command(name="play", description="🎵 Phát nhạc từ YouTube, SoundCloud hoặc tìm kiếm bài hát")
@app_commands.describe(query="Link bài hát (YouTube/SoundCloud) hoặc tên bài hát cần tìm")
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    await process_and_play(interaction.followup, query, interaction.user)

@bot.tree.command(name="skip", description="⏭️ Bỏ qua bài hát hiện tại")
async def slash_skip(interaction: discord.Interaction):
    await do_skip(interaction.channel)
    if not interaction.response.is_done():
        await interaction.response.send_message("⏭️ Skip!", ephemeral=True)

@bot.tree.command(name="stop", description="⏹️ Dừng phát nhạc và khôi phục Monstercat 24/7")
async def slash_stop(interaction: discord.Interaction):
    await do_stop(interaction.channel)
    if not interaction.response.is_done():
        await interaction.response.send_message("⏹️ Stop!", ephemeral=True)

@bot.tree.command(name="nowplaying", description="🎧 Xem bài hát đang phát hiện tại")
async def slash_np(interaction: discord.Interaction):
    await do_np(interaction.channel)
    if not interaction.response.is_done():
        await interaction.response.send_message("🎧 Now Playing", ephemeral=True)

@bot.tree.command(name="queue", description="📜 Xem danh sách hàng chờ bài hát")
async def slash_queue(interaction: discord.Interaction):
    await do_queue(interaction.channel)
    if not interaction.response.is_done():
        await interaction.response.send_message("📜 Queue", ephemeral=True)

@bot.tree.command(name="volume", description="🔊 Điều chỉnh âm lượng nhạc (1 - 100)")
@app_commands.describe(level="Mức âm lượng từ 1 đến 100")
async def slash_volume(interaction: discord.Interaction, level: int):
    level = max(1, min(100, level))
    vol = level / 100.0
    bot.volumes[interaction.guild.id] = vol

    vc = interaction.guild.voice_client
    if vc and vc.source:
        vc.source.volume = vol

    await interaction.response.send_message(f"🔊 Đã chỉnh âm lượng về **{level}%**! 🗿🍷")

# ==================== MAIN ====================
def main():
    token = config.DISCORD_BOT_TOKEN
    if not token:
        logger.error("❌ Chưa cấu hình DISCORD_SUB_BOT_TOKEN hoặc DISCORD_BOT_TOKEN!")
        return

    try:
        bot.run(token)
    except discord.errors.HTTPException as e:
        if getattr(e, "status", None) == 429:
            import time
            logger.error("🚨 Gặp 429 Rate Limit trên IP hosting. Đang đợi giải phóng...")
            time.sleep(120)
        raise e

if __name__ == "__main__":
    main()
