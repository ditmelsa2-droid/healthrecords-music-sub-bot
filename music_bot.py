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

# Monstercat Instinct Vol. 6 (Album Mix) - Stream 24/7 theo yêu cầu của Boss
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

    # Làm sạch URL nếu là link YouTube dính query rác
    if is_yt_link:
        parsed = urllib.parse.urlparse(query)
        qs = urllib.parse.parse_qs(parsed.query)
        if "watch" in parsed.path and "v" in qs:
            clean_yt_url = f"https://www.youtube.com/watch?v={qs['v'][0]}"
        elif "youtu.be" in parsed.netloc:
            vid = parsed.path.strip("/").split("?")[0]
            clean_yt_url = f"https://www.youtube.com/watch?v={vid}"

        # Lấy trước tiêu đề thực qua oEmbed
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
            logger.warning(f"Tầng 1 YouTube bị chặn ({e}). Tự động kích hoạt Cứu hộ HQ...")

    # TẦNG 2: Cứu hộ tự động qua SoundCloud HQ (Không chặn IP Datacenter)
    sc_query_term = oembed_title if oembed_title else re.sub(r'https?://[^\s]+', '', query).strip()
    if not sc_query_term:
        sc_query_term = query.split("/")[-1].replace("-", " ")

    # Nếu trực tiếp là link SoundCloud
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
    # 1. Thử stream SoundCloud HQ của Monstercat Instinct Vol. 6 (siêu mượt, không lỗi bản quyền trên Render)
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
    if header_str:
        before_opts = f'-headers "{header_str}" -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
    else:
        before_opts = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'

    audio = discord.FFmpegPCMAudio(
        track['url'],
        executable=DEFAULT_FFMPEG,
        before_options=before_opts,
        options='-vn'
    )
    return discord.PCMVolumeTransformer(audio, volume=volume)

# ==================== VOICE & PLAYBACK CORE ====================
async def get_or_join_voice_client(guild: discord.Guild, target_channel: discord.VoiceChannel = None) -> discord.VoiceClient | None:
    if not target_channel:
        target_channel = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
        if not target_channel:
            target_channel = discord.utils.find(lambda c: "music" in c.name.lower() or "nhạc" in c.name.lower(), guild.voice_channels)

    if not target_channel:
        return None

    # Nếu bot đã kết nối voice trong server:
    if guild.voice_client and guild.voice_client.is_connected():
        vc = guild.voice_client
        # Nếu đang ở phòng khác so với target_channel -> CHUYỂN PHÒNG!
        if vc.channel.id != target_channel.id:
            logger.info(f"🔄 Chuyển bot từ [{vc.channel.name}] sang [{target_channel.name}]")
            await vc.move_to(target_channel)
        return vc

    # Chưa kết nối -> Kết nối mới
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

# ==================== VOICE STATE LISTENER (AUTO-RETURN TO BASE) ====================
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    base_channel = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
    if not base_channel:
        return

    # Nếu bot đang ở phòng voice khác phòng gốc (ví dụ Room riêng của user):
    if vc.channel.id != base_channel.id:
        humans = [m for m in vc.channel.members if not m.bot]
        if len(humans) == 0:
            logger.info(f"Phòng [{vc.channel.name}] không còn thành viên nào. Bot trở về [{base_channel.name}] trong 3s...")
            await asyncio.sleep(3)
            # Kiểm tra lại xem có ai vào lại chưa
            if len([m for m in vc.channel.members if not m.bot]) == 0:
                bot.queues[guild.id] = []
                if vc.is_playing():
                    vc.stop()
                await vc.move_to(base_channel)
                await start_idle_music(guild)

# ==================== 24/7 VOICE MONITOR TASK ====================
@tasks.loop(seconds=35)
async def voice_health_check():
    guild = bot.get_guild(config.OFFICIAL_GUILD_ID)
    if not guild:
        return

    base_channel = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
    vc = guild.voice_client

    if not vc or not vc.is_connected():
        if base_channel:
            logger.info("🔄 Tự động tái kết nối phòng 🎵・Custom Music...")
            vc = await get_or_join_voice_client(guild, base_channel)
            if vc:
                await start_idle_music(guild)
    else:
        # Nếu bot đang ở phòng phụ nhưng phòng phụ không còn ai:
        if base_channel and vc.channel.id != base_channel.id:
            humans = [m for m in vc.channel.members if not m.bot]
            if len(humans) == 0:
                logger.info(f"Phòng vắng người, tự động đưa bot về lại [{base_channel.name}]...")
                bot.queues[guild.id] = []
                if vc.is_playing():
                    vc.stop()
                await vc.move_to(base_channel)
                await start_idle_music(guild)
                return

        # Nếu không phát nhạc gì và không bị pause -> Tiếp tục phát
        if not vc.is_playing() and not vc.is_paused():
            play_next(guild)

@bot.event
async def on_ready():
    logger.info(f"👑 MUSIC SUB-BOT ĐÃ SẴN SÀNG: {bot.user.name}#{bot.user.discriminator} (ID: {bot.user.id})")
    if not voice_health_check.is_running():
        voice_health_check.start()

    guild = bot.get_guild(config.OFFICIAL_GUILD_ID)
    if guild:
        base_channel = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
        if base_channel:
            vc = await get_or_join_voice_client(guild, base_channel)
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
        await interaction.followup.send("❌ Bạn cần vào một phòng voice hoặc tạo phòng riêng để bot tham gia nhé!", ephemeral=True)
        return

    # Kết nối hoặc chuyển sang phòng voice của người gọi lệnh
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
                    f"🔊 **Phòng Voice:** {target_ch.mention}\n"
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
    app_commands.Choice(name="🐱 Monstercat EDM / Instinct", value="monstercat"),
    app_commands.Choice(name="☕ Lofi Chill Beats 24/7", value="lofi"),
    app_commands.Choice(name="⚡ Synthwave / Cyberpunk 24/7", value="synthwave"),
    app_commands.Choice(name="🍃 Piano & Peaceful Anime", value="anime"),
    app_commands.Choice(name="🌌 Dreamcore / Weirdcore Ambient", value="dreamcore")
])
async def slash_radio(interaction: discord.Interaction, genre: app_commands.Choice[str]):
    await interaction.response.defer()
    guild = interaction.guild

    user_voice = getattr(interaction.user, "voice", None)
    target_ch = user_voice.channel if user_voice else guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)

    streams = {
        "monstercat": MONSTERCAT_SC_STREAM,
        "lofi": "scsearch5:lofi hip hop radio beats to relax chill",
        "synthwave": "scsearch5:synthwave radio chillwave",
        "anime": "scsearch5:peaceful anime piano lofi",
        "dreamcore": "scsearch5:dreamcore weirdcore playlist"
    }

    q_str = streams.get(genre.value, streams["monstercat"])
    track = await extract_audio_info(q_str)
    if not track:
        await interaction.followup.send("❌ Không thể nạp kênh Radio này lúc này, vui lòng thử lại sau!", ephemeral=True)
        return

    vc = await get_or_join_voice_client(guild, target_ch)
    if not vc:
        await interaction.followup.send("❌ Không thể kết nối tới phòng phát thanh!", ephemeral=True)
        return

    if vc.is_playing():
        vc.stop()

    bot.queues[guild.id] = []
    bot.now_playing[guild.id] = track
    bot.idle_active[guild.id] = False

    vol = bot.volumes.get(guild.id, 1.0)
    try:
        source = create_pcm_source(track, volume=vol)
    except Exception as e:
        logger.error(f"Lỗi tạo radio audio source: {e}")
        await interaction.followup.send("❌ Lỗi khi khởi động stream radio!", ephemeral=True)
        return

    def after_radio(err):
        play_next(guild)

    vc.play(source, after=after_radio)

    embed = discord.Embed(
        title="📻 ĐÃ KÍCH HOẠT ĐÀI PHÁT THANH 24/7",
        description=f"✨ Thể loại: **{genre.name}**\n\n🎶 Đang phát: [{track['title']}]({track.get('webpage_url', '')})\n🔊 Phòng: {target_ch.mention}",
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

@bot.tree.command(name="stop", description="⏹️ Dừng phát nhạc và đưa bot về lại phòng 🎵・Custom Music")
async def slash_stop(interaction: discord.Interaction):
    guild = interaction.guild
    bot.queues[guild.id] = []
    vc = guild.voice_client if guild else None

    base_channel = guild.get_channel(config.CUSTOM_MUSIC_CHANNEL_ID)
    if vc and vc.is_playing():
        vc.stop()

    # Nếu đang ở phòng riêng, đưa bot về trạm gốc
    if vc and base_channel and vc.channel.id != base_channel.id:
        await vc.move_to(base_channel)

    await interaction.response.send_message("⏹️ Đã dừng phát nhạc, dọn sạch hàng chờ và khôi phục Monstercat 24/7 tại trạm gốc! 🎧")
    await start_idle_music(guild)

@bot.tree.command(name="nowplaying", description="🎧 Xem bài hát đang phát hiện tại")
async def slash_np(interaction: discord.Interaction):
    guild = interaction.guild
    track = bot.now_playing.get(guild.id)
    if not track:
        await interaction.response.send_message("❌ Hiện tại chưa có bài hát nào được phát.", ephemeral=True)
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
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="queue", description="📜 Xem danh sách hàng chờ bài hát")
async def slash_queue(interaction: discord.Interaction):
    guild = interaction.guild
    q = bot.queues.get(guild.id, [])
    if not q:
        await interaction.response.send_message("📜 Hàng chờ hiện đang trống. Bot đang phát nhạc Monstercat 24/7! 🐱", ephemeral=True)
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
