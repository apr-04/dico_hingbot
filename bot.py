"""봇 진입점. .env 로드, cog 등록, 슬래시 명령어 동기화."""

import asyncio
import logging
import os
import sys

# Windows 콘솔은 cp949라 리다이렉트/pythonw 실행 시 한글·이모지에서 UnicodeEncodeError가 난다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

INITIAL_COGS = ["cogs.music", "cogs.pinball"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True  # 자동 퇴장 판정에 필요
        # message_content는 사용하지 않는다. 슬래시 명령어만 쓰므로 특권 인텐트가 불필요.
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        self.tree.on_error = self.on_app_command_error

        for cog in INITIAL_COGS:
            try:
                await self.load_extension(cog)
                log.info("cog 로드 성공: %s", cog)
            except Exception:
                log.exception("cog 로드 실패: %s", cog)

        # GUILD_ID 지정 시 해당 서버에 즉시 등록. 전역 등록은 반영까지 최대 1시간.
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("슬래시 명령어 %d개를 서버(%s)에 등록", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("슬래시 명령어 %d개를 전역 등록 (반영까지 최대 1시간)", len(synced))

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """미처리 예외는 디스코드에 "응답하지 않았습니다"만 남으므로 여기서 로깅한다."""
        name = interaction.command.name if interaction.command else "unknown"
        log.error("슬래시 명령어 /%s 실행 중 오류", name, exc_info=error)
        try:
            message = f"명령어 처리 중 오류가 났습니다.\n`{type(error).__name__}: {error}`"
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_ready(self) -> None:
        log.info("로그인 완료: %s (ID: %s)", self.user, self.user.id)
        log.info("접속한 서버 수: %d", len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="/재생")
        )


async def main() -> None:
    if not TOKEN or TOKEN.startswith("여기에"):
        raise SystemExit(
            "\nDISCORD_TOKEN이 설정되지 않았습니다. .env.example 을 .env 로 복사한 뒤 "
            "개발자 포털에서 발급한 토큰을 넣어주세요.\n"
        )

    bot = MusicBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("종료")
