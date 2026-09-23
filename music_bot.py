import os
import sys
import asyncio
import logging
import urllib.parse
import re
import datetime
import shutil
import aiohttp
from aiohttp import web as aiohttp_web
import discord
from discord import app_commands
from discord.ext import commands, tasks
import yt_dlp

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

# ==================== FFMPEG & YTDL CONFIG ====================
try:
    import imageio_ffmpeg
    DEFAULT_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    DEFAULT_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

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
    'extractor_args': {'youtube': {'player_client': ['mweb', 'android', 'web']}}
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
    elif os.environ.get("YOUTUBE_COOKIES", "").strip():
        try:
            with open(COOKIE_PATH, "w", encoding="utf-8") as f:
                f.write(os.environ["YOUTUBE_COOKIES"])
            logger.info("🍪 Đã nạp YOUTUBE_COOKIES vào data/cookies.txt!")
        except Exception as e:
            logger.warning(f"Không thể ghi YOUTUBE_COOKIES: {e}")

setup_youtube_cookies()

def get_ytdl_instance(use_cookies: bool = True):
    opts = dict(YTDL_OPTIONS)
    if use_cookies and os.path.exists(COOKIE_PATH) and os.path.getsize(COOKIE_PATH) > 10:
        opts['cookiefile'] = COOKIE_PATH
    return yt_dlp.YoutubeDL(opts)

ytdl = get_ytdl_instance(use_cookies=True)
ytdl_nocookie = get_ytdl_instance(use_cookies=False)

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

IDLE_PLAYLIST_URL = "https://www.youtube.com/watch?v=PNh3gWJoF8Y"
IDLE_PLAYLIST_TITLE = "⋆˙⟡♡ 'Where am I?' ♡⟡˙⋆ a dreamcore playlist ⋆.˚ ☾⭒.˚"
URL_REGEX = r'(https?://[^\s]+)'

# ==================== KEEP-ALIVE WEB SERVER ====================
async def handle_ping(request):
    return aiohttp_web.Response(
        text="🎵 DeaHades Music Sub-Bot is ONLINE & STREAMING 24/7! 🗿🍷",
        content_type="text/plain",
        charset="utf-8"
    )

async def start_web_server():
    app = aiohttp_web.Application()
    app.router.add_get('/', handle_ping)
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
async def extract_audio_info(query: str) -> dict | None:
    loop = asyncio.get_event_loop()

    # Làm sạch URL nếu là link YouTube dính rác playlist
    if "youtube.com" in query or "youtu.be" in query:
        parsed = urllib.parse.urlparse(query)
        qs = urllib.parse.parse_qs(parsed.query)
        if "watch" in parsed.path and "v" in qs:
            query = f"https://www.youtube.com/watch?v={qs['v'][0]}"
        elif "youtu.be" in parsed.netloc:
            vid = parsed.path.strip("/").split("?")[0]
            query = f"https://www.youtube.com/watch?v={vid}"

    # TẦNG 1: Thử trích xuất với yt-dlp
    for current_ytdl in [ytdl, ytdl_nocookie]:
        try:
            data = await loop.run_in_executor(None, lambda: current_ytdl.extract_info(query, download=False))
            if data:
                if 'entries' in data and data['entries']:
                    data = data['entries'][0]
                
                audio_url = data.get('url')
                if not audio_url and 'formats' in data:
                    formats = [f for f in data['formats'] if f.get('acodec') != 'none']
                    if formats:
                        formats.sort(key=lambda f: f.get('abr') or 0, reverse=True)
                        audio_url = formats[0].get('url')

                if audio_url:
                    return {
                        'title': data.get('title', 'Unknown Title'),
                        'url': audio_url,
                        'webpage_url': data.get('webpage_url', query),
                        'duration': data.get('duration', 0),
                        'thumbnail': data.get('thumbnail'),
                        'uploader': data.get('uploader', 'Unknown Artist'),
                        'is_idle': False
                    }
        except Exception as e:
            logger.warning(f"Tầng 1 thất bại ({query}): {e}")

    # TẦNG 2: Cứu hộ tự động qua SoundCloud nếu YouTube bị chặn
    clean_query = re.sub(r'https?://[^\s]+', '', query).strip()
    if not clean_query:
        clean_query = query.split("/")[-1].replace("-", " ")

    sc_query = f"scsearch5:{clean_query}"
    logger.info(f"🔄 Kích hoạt Tầng 2 cứu hộ âm thanh SoundCloud: {sc_query}")
    try:
        data = await loop.run_in_executor(None, lambda: ytdl_nocookie.extract_info(sc_query, download=False))
        if data and 'entries' in data and data['entries']:
            entry = data['entries'][0]
            audio_url = entry.get('url')
            if audio_url:
                logger.info(f"✅ Tầng 2 cứu hộ thành công: {entry.get('title')}")
                return {
                    'title': f"{entry.get('title', 'Unknown')} [SoundCloud HQ]",
                    'url': audio_url,
                    'webpage_url': entry.get('webpage_url', ''),
                    'duration': entry.get('duration', 0),
                    'thumbnail': entry.get('thumbnail'),
                    'uploader': entry.get('uploader', 'SoundCloud'),
                    'is_idle': False
                }
    except Exception as e:
        logger.error(f"Tầng 2 thất bại: {e}")

    return None

async def extract_dreamcore_stream() -> dict | None:
    loop = asyncio.get_event_loop()
    # Thử SoundCloud Dreamcore Playlist trước (rất ổn định, không chặn IP hosting)
    sc_queries = [
        "scsearch5:dreamcore playlist aesthetic",
        "scsearch5:running away dreamcore playlist",
        "scsearch5:dreamcore weirdcore traumacore"
    ]
    for q in sc_queries:
        try:
            data = await loop.run_in_executor(None, lambda: ytdl_nocookie.extract_info(q, download=False))
            if data and 'entries' in data and data['entries']:
                entry = data['entries'][0]
                if entry.get('url'):
                    return {
                        'title': f"{entry.get('title')} [Dreamcore 24/7]",
                        'url': entry.get('url'),
                        'webpage_url': entry.get('webpage_url', ''),
                        'duration': entry.get('duration', 0),
                        'thumbnail': entry.get('thumbnail'),
                        'uploader': entry.get('uploader', 'Dreamcore Radio'),
                        'is_idle': True
                    }
        except Exception:
            pass

    # Dự phòng YouTube
    res = await extract_audio_info(IDLE_PLAYLIST_URL)
    if res:
        res['is_idle'] = True
        return res
    return None

# ==================== VOICE & PLAYBACK CORE ====================
async def get_or_join_voice_client(guild: discord.Guild, target_channel: discord.VoiceChannel = None) -> discord.VoiceClient | None:
    if guild.voice_client:
        if guild.voice_client.is_connected():
            return guild.voice_client

    if not target_channel:
        target_channel = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
        if not target_channel:
            target_channel = discord.utils.find(lambda c: "music" in c.name.lower() or "nhạc" in c.name.lower(), guild.voice_channels)

    if not target_channel:
        return None

    try:
        vc = await target_channel.connect(reconnect=True, timeout=15.0)
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
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track['url'], executable=DEFAULT_FFMPEG, **FFMPEG_OPTIONS),
            volume=vol
        )

        def after_callback(err):
            if err:
                logger.error(f"Playback error: {err}")
            asyncio.run_coroutine_threadsafe(handle_after_playback(guild), bot.loop)

        vc.play(source, after=after_callback)
        logger.info(f"🎶 Đang phát: {track['title']} ({vol*100:.0f}% vol)")
    else:
        # Hàng chờ rỗng -> Bật nhạc nền Dreamcore IDLE 24/7
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

    logger.info(f"🌌 Kích hoạt nhạc nền Dreamcore IDLE tại {guild.name}...")
    track = await extract_dreamcore_stream()
    if not track:
        logger.warning("Không thể lấy stream Dreamcore IDLE.")
        return

    bot.now_playing[guild.id] = track
    bot.idle_active[guild.id] = True

    vol = bot.volumes.get(guild.id, 0.8) # Nhạc nền êm dịu 80%
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(track['url'], executable=DEFAULT_FFMPEG, **FFMPEG_OPTIONS),
        volume=vol
    )

    def after_idle(err):
        if err:
            logger.warning(f"Idle stream ended: {err}")
        asyncio.run_coroutine_threadsafe(handle_after_playback(guild), bot.loop)

    vc.play(source, after=after_idle)
    logger.info("✅ Dreamcore IDLE 24/7 đang ngân vang.")

# ==================== 24/7 VOICE MONITOR TASK ====================
@tasks.loop(seconds=45)
async def voice_health_check():
    guild = bot.get_guild(config.OFFICIAL_GUILD_ID)
    if not guild:
        return

    vc = guild.voice_client
    if not vc or not vc.is_connected():
        target_ch = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
        if target_ch:
            logger.info("🔄 Tự động tái kết nối phòng 🎵・Custom Music...")
            vc = await get_or_join_voice_client(guild, target_ch)
            if vc:
                await start_idle_music(guild)
    else:
        if not vc.is_playing() and not vc.is_paused():
            play_next(guild)

@bot.event
async def on_ready():
    logger.info(f"👑 MUSIC SUB-BOT ĐÃ SẴN SÀNG: {bot.user.name}#{bot.user.discriminator} (ID: {bot.user.id})")
    if not voice_health_check.is_running():
        voice_health_check.start()

    guild = bot.get_guild(config.OFFICIAL_GUILD_ID)
    if guild:
        target_ch = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
        if target_ch:
            vc = await get_or_join_voice_client(guild, target_ch)
            if vc and not vc.is_playing():
                await start_idle_music(guild)

# ==================== SLASH COMMANDS ====================
@bot.tree.command(name="play", description="🎵 Phát nhạc từ YouTube, SoundCloud hoặc tìm kiếm bài hát")
@app_commands.describe(query="Link bài hát (YouTube/SoundCloud) hoặc tên bài hát cần tìm")
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    guild = interaction.guild

    user_voice = getattr(interaction.user, "voice", None)
    target_ch = user_voice.channel if user_voice else guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)

    if not target_ch:
        await interaction.followup.send("❌ Bạn cần vào một phòng voice hoặc tạo phòng để bot phát nhạc nhé!", ephemeral=True)
        return

    vc = await get_or_join_voice_client(guild, target_ch)
    if not vc:
        await interaction.followup.send("❌ Bot không thể tham gia phòng voice của bạn!", ephemeral=True)
        return

    track = await extract_audio_info(query)
    if not track:
        await interaction.followup.send(f"❌ Không thể tìm thấy hoặc trích xuất âm thanh từ: `{query}`", ephemeral=True)
        return

    q = bot.queues.setdefault(guild.id, [])
    track['requester'] = interaction.user.display_name

    embed = discord.Embed(
        title="🎵 THÊM BÀI HÁT THÀNH CÔNG",
        description=f"[{track['title']}]({track.get('webpage_url', '')})\n\n"
                    f"⏱️ **Thời lượng:** `{datetime.timedelta(seconds=track['duration']) if track['duration'] else 'Livestream'}`\n"
                    f"👤 **Yêu cầu bởi:** {interaction.user.mention}",
        color=config.COLOR_SUCCESS
    )
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])

    # Nếu bot đang phát nhạc nền IDLE hoặc không phát gì -> phát ngay bài mới!
    if bot.idle_active.get(guild.id, False) or not vc.is_playing():
        if vc.is_playing():
            vc.stop()
        q.insert(0, track)
        play_next(guild)
        embed.title = "🎶 ĐANG PHÁT BÀI HÁT"
        await interaction.followup.send(embed=embed)
    else:
        q.append(track)
        embed.set_footer(text=f"Vị trí trong hàng chờ: #{len(q)}")
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="radio", description="📻 Bật đài phát thanh 24/7 theo thể loại yêu thích")
@app_commands.choices(genre=[
    app_commands.Choice(name="🌌 Dreamcore / Weirdcore 24/7", value="dreamcore"),
    app_commands.Choice(name="☕ Lofi Chill Beats 24/7", value="lofi"),
    app_commands.Choice(name="⚡ Synthwave / Cyberpunk 24/7", value="synthwave"),
    app_commands.Choice(name="🎧 Gaming EDM High Energy", value="edm"),
    app_commands.Choice(name="🍃 Piano & Peaceful Anime", value="anime")
])
async def slash_radio(interaction: discord.Interaction, genre: app_commands.Choice[str]):
    await interaction.response.defer()
    guild = interaction.guild

    streams = {
        "dreamcore": "scsearch5:dreamcore playlist aesthetic",
        "lofi": "scsearch5:lofi hip hop radio beats to relax chill",
        "synthwave": "scsearch5:synthwave radio chillwave",
        "edm": "scsearch5:gaming edm nocopyrightsounds",
        "anime": "scsearch5:peaceful anime piano lofi"
    }

    q_str = streams.get(genre.value, streams["dreamcore"])
    track = await extract_audio_info(q_str)
    if not track:
        await interaction.followup.send("❌ Không thể nạp kênh Radio này lúc này, vui lòng thử lại sau!", ephemeral=True)
        return

    vc = await get_or_join_voice_client(guild)
    if not vc:
        await interaction.followup.send("❌ Không thể kết nối tới phòng phát thanh!", ephemeral=True)
        return

    if vc.is_playing():
        vc.stop()

    bot.queues[guild.id] = []
    bot.now_playing[guild.id] = track
    bot.idle_active[guild.id] = False

    vol = bot.volumes.get(guild.id, 1.0)
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(track['url'], executable=DEFAULT_FFMPEG, **FFMPEG_OPTIONS),
        volume=vol
    )

    def after_radio(err):
        play_next(guild)

    vc.play(source, after=after_radio)

    embed = discord.Embed(
        title="📻 ĐÃ KÍCH HOẠT ĐÀI PHÁT THANH 24/7",
        description=f"✨ Thể loại: **{genre.name}**\n\n🎶 Đang phát: [{track['title']}]({track.get('webpage_url', '')})",
        color=config.COLOR_GOLD
    )
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="skip", description="⏭️ Bỏ qua bài hát hiện tại")
async def slash_skip(interaction: discord.Interaction):
    guild = interaction.guild
    vc = guild.voice_client if guild else None
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Đã bỏ qua bài hát hiện tại! Đang chuyển tiếp... 🗿🍷")
    else:
        await interaction.response.send_message("❌ Hiện không có bài hát nào đang phát để skip.", ephemeral=True)

@bot.tree.command(name="stop", description="⏹️ Dừng phát nhạc và trở lại không gian Dreamcore IDLE 24/7")
async def slash_stop(interaction: discord.Interaction):
    guild = interaction.guild
    bot.queues[guild.id] = []
    vc = guild.voice_client if guild else None
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message("⏹️ Đã dừng phát nhạc, dọn sạch hàng chờ và khôi phục nhạc nền 24/7! 🌌")

@bot.tree.command(name="nowplaying", description="🎧 Xem bài hát đang phát hiện tại")
async def slash_np(interaction: discord.Interaction):
    guild = interaction.guild
    track = bot.now_playing.get(guild.id)
    if not track:
        await interaction.response.send_message("❌ Hiện tại chưa có bài hát nào được phát.", ephemeral=True)
        return

    is_idle = bot.idle_active.get(guild.id, False)
    status_text = "🌌 **Nhạc Nền 24/7 (IDLE Ambient)**" if is_idle else "🎶 **Đang Phát (Thành Viên Yêu Cầu)**"

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
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="queue", description="📜 Xem danh sách hàng chờ bài hát")
async def slash_queue(interaction: discord.Interaction):
    guild = interaction.guild
    q = bot.queues.get(guild.id, [])
    if not q:
        await interaction.response.send_message("📜 Hàng chờ hiện đang trống. Bot đang phát nhạc nền thư giãn 24/7! 🌌", ephemeral=True)
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
    await interaction.response.send_message(embed=embed)

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
