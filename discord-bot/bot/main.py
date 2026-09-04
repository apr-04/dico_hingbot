import os

import discord
from discord.ext import commands
from dotenv import load_dotenv


# ============================================================
# Environment
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")


# ============================================================
# Bot
# ============================================================

class MusicBot(commands.Bot):

    def __init__(self):

        intents = discord.Intents.default()

        # Prefix Command 및 Message Content 사용
        intents.message_content = True

        super().__init__(
            command_prefix="/",
            intents=intents
        )


    async def setup_hook(self):
        await self.load_extension(
            "bot.cogs.music"
        )

        await self.load_extension(
            "bot.cogs.custom_match"
        )

        synced = await self.tree.sync()
        print(
            f"글로벌 Slash Command 동기화 완료 ({len(synced)}개): {[c.name for c in synced]}"
        )


# ============================================================
# Bot
# ============================================================

bot = MusicBot()


# ============================================================
# Event
# ============================================================

@bot.event
async def on_ready():
    print(
        f"로그인 완료: {bot.user} (ID: {bot.user.id})"
    )

    # 봇이 참여 중인 모든 서버에 즉시 동기화 (글로벌 롤아웃 지연 방지)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            guild_synced = await bot.tree.sync(guild=guild)
            print(
                f"[{guild.name}] 서버 슬래시 커맨드 즉시 동기화 완료 ({len(guild_synced)}개)"
            )
        except Exception as e:
            print(
                f"[{guild.name}] 길드 동기화 오류: {e}"
            )


# ============================================================
# Run
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "DISCORD_TOKEN이 설정되지 않았습니다."
    )


bot.run(TOKEN)