import os
import sys
import logging
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("discord_bot")

# 프로젝트 루트 경로를 sys.path에 추가 (어디서 실행하든 정상 동작하도록 보장)
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir) if os.path.basename(current_dir) == "bot" else current_dir
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# .env 파일 로드
load_dotenv(os.path.join(project_root, ".env"))
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")

class MyBot(commands.Bot):
    def __init__(self):
        # 기본 인텐트 설정
        intents = discord.Intents.default()
        intents.message_content = True  # 접두사 명령어 및 메시지 내용 수신용

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):
        """봇 시작 전 Cog 로드 및 슬래시 커맨드 동기화"""
        cogs_dir = os.path.join(project_root, "bot", "cogs")
        if os.path.exists(cogs_dir):
            for filename in os.listdir(cogs_dir):
                if filename.endswith(".py") and not filename.startswith("_"):
                    extension_name = f"bot.cogs.{filename[:-3]}"
                    try:
                        await self.load_extension(extension_name)
                        logger.info(f"Loaded extension: {extension_name}")
                    except Exception as e:
                        logger.error(f"Failed to load extension {extension_name}: {e}", exc_info=True)

        # 슬래시 커맨드(App Command) 동기화
        if TEST_GUILD_ID:
            try:
                guild = discord.Object(id=int(TEST_GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(f"Synced {len(synced)} command(s) to test guild: {TEST_GUILD_ID}")
            except Exception as e:
                logger.error(f"Failed to sync commands to test guild: {e}", exc_info=True)
        else:
            try:
                synced = await self.tree.sync()
                logger.info(f"Synced {len(synced)} command(s) globally.")
            except Exception as e:
                logger.error(f"Failed to sync commands globally: {e}", exc_info=True)

    async def on_ready(self):
        """봇 로그인 완료 시 호출"""
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s).")
        
        # 봇 상태 메시지 설정
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="/ping | 작동 중"
        )
        await self.change_presence(status=discord.Status.online, activity=activity)

async def main():
    if not TOKEN or TOKEN == "your_bot_token_here":
        logger.error("DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일에 올바른 봇 토큰을 입력해주세요.")
        return

    bot = MyBot()
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
