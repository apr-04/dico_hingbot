import platform
import discord
from discord import app_commands
from discord.ext import commands

class GeneralCog(commands.Cog, name="일반"):
    """기본 유틸리티 및 상태 확인 명령어 모음"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="봇의 현재 응답 지연 시간(Latency)을 확인합니다.")
    async def ping(self, interaction: discord.Interaction):
        """봇 지연시간 확인 명령어"""
        latency_ms = round(self.bot.latency * 1000)
        
        embed = discord.Embed(
            title="🏓 퐁(Pong)!",
            description=f"현재 봇 레이턴시: **{latency_ms}ms**",
            color=discord.Color.green() if latency_ms < 200 else discord.Color.orange()
        )
        embed.set_footer(text=f"요청자: {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="info", description="봇의 시스템 정보 및 상태를 확인합니다.")
    async def info(self, interaction: discord.Interaction):
        """봇 시스템 정보 명령어"""
        embed = discord.Embed(
            title="ℹ️ 봇 시스템 정보",
            color=discord.Color.blue()
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        
        embed.add_field(name="🤖 봇 이름", value=self.bot.user.name if self.bot.user else "알 수 없음", inline=True)
        embed.add_field(name="🌐 서버 수", value=f"{len(self.bot.guilds)}개", inline=True)
        embed.add_field(name="🐍 Python 버전", value=platform.python_version(), inline=True)
        embed.add_field(name="📦 discord.py 버전", value=discord.__version__, inline=True)
        embed.add_field(name="💻 운영체제", value=f"{platform.system()} {platform.release()}", inline=True)
        embed.set_footer(text=f"요청자: {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="echo", description="입력한 메시지를 봇이 그대로 따라 말합니다.")
    @app_commands.describe(message="따라 말할 메시지")
    async def echo(self, interaction: discord.Interaction, message: str):
        """에코 명령어"""
        await interaction.response.send_message(f"📢 **{interaction.user.display_name}**: {message}")

async def setup(bot: commands.Bot):
    await bot.add_cog(GeneralCog(bot))
