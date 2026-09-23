import os
import discord
from dotenv import load_dotenv

load_dotenv()

# ==================== CREDENTIALS & TOKENS ====================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_SUB_BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
BOT_SUPER_ADMIN_ID = int(os.getenv("BOT_SUPER_ADMIN_ID", "993927627532423189"))
OFFICIAL_GUILD_ID = int(os.getenv("OFFICIAL_GUILD_ID", "1525826537033695352"))
CUSTOM_MUSIC_CHANNEL_ID = int(os.getenv("CUSTOM_MUSIC_CHANNEL_ID", "1551498078111006780"))

# ==================== CLOUDFLARE DISCORD PROXY (Bypass 429 Rate Limits) ====================
DISCORD_PROXY_URL = os.getenv("DISCORD_PROXY_URL", "https://discord-proxy.mugiandietquy.workers.dev/api/v10")
USE_DISCORD_PROXY = os.getenv("USE_DISCORD_PROXY", "true").lower() in ("true", "1", "yes")

# ==================== COLOR PALETTE ====================
COLOR_GOLD    = discord.Color.from_rgb(255, 215, 0)     # Radiant Gold
COLOR_SUCCESS = discord.Color.from_rgb(0, 230, 118)     # Vivid Spring Emerald
COLOR_PRIMARY = discord.Color.from_rgb(47, 49, 54)      # Dark clean aesthetic
COLOR_DANGER  = discord.Color.from_rgb(255, 23, 68)     # Ruby Crimson Red
COLOR_CYAN    = discord.Color.from_rgb(0, 229, 255)     # Electric Cyan
COLOR_PURPLE  = discord.Color.from_rgb(179, 136, 255)   # Royal Amethyst
