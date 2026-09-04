import os
import sys
import shutil
import asyncio
import logging
from typing import Dict, Optional, List

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp as youtube_dl

logger = logging.getLogger("discord_bot.music")

# FFmpeg 경로 자동 탐색 함수
def get_ffmpeg_executable() -> str:
    # 1. 환경변수 FFMPEG_PATH 확인
    env_path = os.getenv("FFMPEG_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    
    # 2. 프로젝트 디렉터리 내 ffmpeg.exe 확인
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    local_ffmpeg = os.path.join(project_root, "ffmpeg.exe")
    if os.path.isfile(local_ffmpeg):
        return local_ffmpeg

    # 3. 시스템 PATH 확인
    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return which_ffmpeg

    # 4. 기본값
    return "ffmpeg"


# yt-dlp 설정
YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

FFMPEG_BEFORE_OPTIONS = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
FFMPEG_OPTIONS = '-vn'

ytdl = youtube_dl.YoutubeDL(YTDL_FORMAT_OPTIONS)


class Song:
    """재생할 곡의 메타데이터 및 스트림 정보"""
    def __init__(self, data: dict):
        self.title: str = data.get('title', '알 수 없는 제목')
        self.webpage_url: str = data.get('webpage_url') or data.get('url', '')
        self.stream_url: str = data.get('url', '')
        self.duration: int = data.get('duration', 0)
        self.thumbnail: Optional[str] = data.get('thumbnail')
        self.uploader: str = data.get('uploader', '알 수 없음')
        self.data: dict = data

    @property
    def formatted_duration(self) -> str:
        if not self.duration:
            return "라이브/알 수 없음"
        mins, secs = divmod(self.duration, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return f"{hours:02d}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"


class YTDLSource(discord.PCMVolumeTransformer):
    """FFmpeg 오디오 소스 래퍼"""
    def __init__(self, source: discord.AudioSource, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', '알 수 없는 제목')
        self.url = data.get('url', '')

    @classmethod
    async def create_source(cls, search_or_url: str, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> 'YTDLSource':
        """URL 또는 검색어로부터 실시간 재생 가능한 AudioSource 생성"""
        loop = loop or asyncio.get_running_loop()
        ffmpeg_bin = get_ffmpeg_executable()

        # yt-dlp는 블로킹 I/O 작업이므로 executor에서 실행
        data = await loop.run_in_executor(
            None,
            lambda: ytdl.extract_info(search_or_url, download=False)
        )

        if 'entries' in data:
            if not data['entries']:
                raise ValueError("검색 결과를 찾을 수 없습니다.")
            data = data['entries'][0]

        stream_url = data['url']
        ffmpeg_audio = discord.FFmpegPCMAudio(
            stream_url,
            executable=ffmpeg_bin,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS
        )
        return cls(ffmpeg_audio, data=data)

    @classmethod
    async def extract_song_info(cls, search_or_url: str, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> Song:
        """재생하지 않고 곡 정보만 추출 (대기열 추가 시 사용)"""
        loop = loop or asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None,
            lambda: ytdl.extract_info(search_or_url, download=False)
        )
        if 'entries' in data:
            if not data['entries']:
                raise ValueError("검색 결과를 찾을 수 없습니다.")
            data = data['entries'][0]
        return Song(data)


class GuildMusicState:
    """서버(Guild)별 음악 재생 상태 및 대기열 관리"""
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: List[Song] = []
        self.current_song: Optional[Song] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.max_queue_size: int = 50


class MusicCog(commands.Cog, name="음악"):
    """유튜브 음악 재생 및 플레이리스트 관리 명령어"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: Dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState(guild_id)
        return self.states[guild_id]

    async def ensure_voice(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        """사용자가 음성 채널에 있는지 확인하고 봇을 해당 채널에 연결"""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("⚠️ 먼저 음성 채널에 입장해 주세요!", ephemeral=True)
            return None

        user_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client

        if voice_client is None:
            voice_client = await user_channel.connect()
        elif voice_client.channel != user_channel:
            await voice_client.move_to(user_channel)

        return voice_client

    def play_next_song(self, guild_id: int, error=None):
        """곡 재생 종료 후 다음 대기 곡을 재생하는 콜백"""
        if error:
            logger.error(f"Player error in guild {guild_id}: {error}")

        state = self.get_state(guild_id)
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            state.current_song = None
            return

        if not state.queue:
            state.current_song = None
            if state.text_channel:
                embed = discord.Embed(
                    title="🎵 재생 완료",
                    description="플레이리스트의 모든 곡 재생이 끝났습니다.",
                    color=discord.Color.light_grey()
                )
                self.bot.loop.create_task(state.text_channel.send(embed=embed))
            return

        # 다음 곡 추출 및 재생 비동기 작업 예약
        next_song = state.queue.pop(0)
        state.current_song = next_song
        self.bot.loop.create_task(self._start_playback(guild, state, next_song))

    async def _start_playback(self, guild: discord.Guild, state: GuildMusicState, song: Song):
        """실제 오디오 스트림을 생성하고 재생"""
        voice_client: discord.VoiceClient = guild.voice_client
        if not voice_client:
            return

        try:
            # 신선한 스트림 URL 추출하여 오디오 소스 생성
            source = await YTDLSource.create_source(song.webpage_url, loop=self.bot.loop)
            
            def after_playback(e):
                self.play_next_song(guild.id, e)

            voice_client.play(source, after=after_playback)

            if state.text_channel:
                embed = discord.Embed(
                    title="▶️ 지금 재생 중",
                    description=f"[{song.title}]({song.webpage_url})",
                    color=discord.Color.green()
                )
                embed.add_field(name="길이", value=song.formatted_duration, inline=True)
                embed.add_field(name="업로더", value=song.uploader, inline=True)
                embed.add_field(name="남은 대기열", value=f"{len(state.queue)}곡", inline=True)
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                await state.text_channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Playback error in guild {guild.id}: {e}", exc_info=True)
            if state.text_channel:
                err_msg = str(e)
                if "ffmpeg" in err_msg.lower() or isinstance(e, FileNotFoundError):
                    embed = discord.Embed(
                        title="❌ FFmpeg 실행 오류",
                        description=(
                            "FFmpeg를 찾을 수 없습니다.\n"
                            "1. [FFmpeg 공식 홈페이지](https://ffmpeg.org/download.html) 또는 gyan.dev에서 다운로드\n"
                            "2. `ffmpeg.exe`를 봇 폴더에 배치하거나 PATH에 추가해 주세요."
                        ),
                        color=discord.Color.red()
                    )
                else:
                    embed = discord.Embed(
                        title="❌ 재생 실패",
                        description=f"음악을 재생하는 도중 오류가 발생했습니다:\n`{e}`",
                        color=discord.Color.red()
                    )
                await state.text_channel.send(embed=embed)
            # 오류 발생 시 다음 곡으로 시도
            self.play_next_song(guild.id)

    # -------------------------------------------------------------
    # 1. /노래 : 단일 노래 재생
    # -------------------------------------------------------------
    @app_commands.command(name="노래", description="단일 노래를 즉시 재생합니다. (유튜브 제목 또는 링크)")
    @app_commands.describe(query="유튜브 동영상 링크 또는 검색할 노래 제목")
    async def play_single(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        voice_client = await self.ensure_voice(interaction)
        if not voice_client:
            return

        state = self.get_state(interaction.guild_id)
        state.text_channel = interaction.channel

        try:
            # 곡 정보 추출
            song = await YTDLSource.extract_song_info(query, loop=self.bot.loop)
        except Exception as e:
            await interaction.followup.send(f"❌ 검색 및 음악 정보 로드에 실패했습니다: {e}")
            return

        # 단일 곡 재생이므로 기존 재생 중단 및 대기열 리셋
        state.queue.clear()
        state.current_song = song

        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        try:
            source = await YTDLSource.create_source(song.webpage_url, loop=self.bot.loop)
            voice_client.play(source, after=lambda e: self.play_next_song(interaction.guild_id, e))

            embed = discord.Embed(
                title="🎵 단일 노래 재생 시작",
                description=f"[{song.title}]({song.webpage_url})",
                color=discord.Color.green()
            )
            embed.add_field(name="길이", value=song.formatted_duration, inline=True)
            embed.add_field(name="업로더", value=song.uploader, inline=True)
            embed.set_footer(text=f"신청자: {interaction.user.display_name}")
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error starting playback: {e}", exc_info=True)
            if "ffmpeg" in str(e).lower() or isinstance(e, FileNotFoundError):
                msg = "❌ FFmpeg가 설치되어 있지 않습니다. 프로젝트 폴더에 `ffmpeg.exe`를 넣거나 PATH에 등록해 주세요."
            else:
                msg = f"❌ 재생을 시작할 수 없습니다: {e}"
            await interaction.followup.send(msg)

    # -------------------------------------------------------------
    # 2. /플리 : 플레이리스트 확인
    # -------------------------------------------------------------
    @app_commands.command(name="플리", description="현재 재생 중인 곡과 플레이리스트(대기열)를 확인합니다.")
    async def playlist_show(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)

        embed = discord.Embed(
            title="📜 플레이리스트 (대기열)",
            color=discord.Color.blurple()
        )

        # 현재 재생 곡
        if state.current_song:
            embed.add_field(
                name="▶️ 현재 재생 중",
                value=f"[{state.current_song.title}]({state.current_song.webpage_url}) ({state.current_song.formatted_duration})",
                inline=False
            )
        else:
            embed.add_field(name="▶️ 현재 재생 중", value="재생 중인 곡이 없습니다.", inline=False)

        # 대기열 목록
        if state.queue:
            queue_lines = []
            for i, song in enumerate(state.queue[:10], start=1):
                queue_lines.append(f"**{i}.** [{song.title}]({song.webpage_url}) `[{song.formatted_duration}]`")
            
            queue_text = "\n".join(queue_lines)
            if len(state.queue) > 10:
                queue_text += f"\n*...외 {len(state.queue) - 10}곡 더 있음*"

            embed.add_field(
                name=f"📋 대기 목록 (총 {len(state.queue)}곡)",
                value=queue_text,
                inline=False
            )
        else:
            embed.add_field(name="📋 대기 목록", value="대기열이 비어 있습니다. `/플리추가`로 곡을 추가해보세요!", inline=False)

        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------
    # 3. /플리재생 : 플레이리스트 재생
    # -------------------------------------------------------------
    @app_commands.command(name="플리재생", description="대기열에 담긴 플레이리스트의 음악을 재생합니다.")
    async def playlist_play(self, interaction: discord.Interaction):
        await interaction.response.defer()

        state = self.get_state(interaction.guild_id)
        state.text_channel = interaction.channel

        voice_client = await self.ensure_voice(interaction)
        if not voice_client:
            return

        if voice_client.is_playing():
            await interaction.followup.send("⚠️ 이미 음악이 재생 중입니다. `/플리`로 목록을 확인하거나 `/스킵`할 수 있습니다.")
            return

        if not state.queue:
            await interaction.followup.send("⚠️ 대기열이 비어 있습니다. 먼저 `/플리추가 [노래]` 명령어로 곡을 담아주세요!")
            return

        # 대기열에서 첫 번째 곡 꺼내어 재생
        first_song = state.queue.pop(0)
        state.current_song = first_song

        try:
            source = await YTDLSource.create_source(first_song.webpage_url, loop=self.bot.loop)
            voice_client.play(source, after=lambda e: self.play_next_song(interaction.guild_id, e))

            embed = discord.Embed(
                title="▶️ 플레이리스트 재생 시작",
                description=f"[{first_song.title}]({first_song.webpage_url})",
                color=discord.Color.green()
            )
            embed.add_field(name="길이", value=first_song.formatted_duration, inline=True)
            embed.add_field(name="남은 대기열", value=f"{len(state.queue)}곡", inline=True)
            if first_song.thumbnail:
                embed.set_thumbnail(url=first_song.thumbnail)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error starting playlist playback: {e}", exc_info=True)
            if "ffmpeg" in str(e).lower() or isinstance(e, FileNotFoundError):
                msg = "❌ FFmpeg가 설치되어 있지 않습니다. 프로젝트 폴더에 `ffmpeg.exe`를 넣거나 PATH에 등록해 주세요."
            else:
                msg = f"❌ 재생을 시작할 수 없습니다: {e}"
            await interaction.followup.send(msg)

    # -------------------------------------------------------------
    # 4. /플리추가 : 플레이리스트에 노래 추가
    # -------------------------------------------------------------
    @app_commands.command(name="플리추가", description="플레이리스트(대기열)에 노래를 추가합니다.")
    @app_commands.describe(query="유튜브 동영상 링크 또는 검색할 노래 제목")
    async def playlist_add(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        state = self.get_state(interaction.guild_id)
        state.text_channel = interaction.channel

        if len(state.queue) >= state.max_queue_size:
            await interaction.followup.send(f"⚠️ 플레이리스트가 가득 찼습니다. (최대 {state.max_queue_size}곡)")
            return

        try:
            song = await YTDLSource.extract_song_info(query, loop=self.bot.loop)
        except Exception as e:
            await interaction.followup.send(f"❌ 노래 정보를 가져올 수 없습니다: {e}")
            return

        state.queue.append(song)

        embed = discord.Embed(
            title="➕ 플레이리스트에 곡 추가 완료",
            description=f"[{song.title}]({song.webpage_url})",
            color=discord.Color.blue()
        )
        embed.add_field(name="길이", value=song.formatted_duration, inline=True)
        embed.add_field(name="대기 번호", value=f"{len(state.queue)}번째", inline=True)
        embed.set_footer(text=f"신청자: {interaction.user.display_name} | /플리재생 으로 시작할 수 있습니다.")
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)

        await interaction.followup.send(embed=embed)

    # -------------------------------------------------------------
    # 5. 편의 커맨드: /스킵 및 /정지
    # -------------------------------------------------------------
    @app_commands.command(name="스킵", description="현재 재생 중인 노래를 건너뛰고 다음 곡을 재생합니다.")
    async def skip_song(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            await interaction.response.send_message("⚠️ 현재 재생 중인 음악이 없습니다.", ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        current_title = state.current_song.title if state.current_song else "현재 곡"
        
        # 음성 클라이언트 정지 -> after 콜백이 트리거되어 다음 곡 자동 재생
        voice_client.stop()
        await interaction.response.send_message(f"⏭️ `{current_title}` 곡을 건너뛰었습니다.")

    @app_commands.command(name="정지", description="재생을 중단하고 대기열을 비운 뒤 음성 채널에서 퇴장합니다.")
    async def stop_music(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        state = self.get_state(interaction.guild_id)

        state.queue.clear()
        state.current_song = None

        if voice_client:
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
            await voice_client.disconnect()
            await interaction.response.send_message("⏹️ 재생을 정지하고 음성 채널에서 퇴장했습니다.")
        else:
            await interaction.response.send_message("⚠️ 봇이 음성 채널에 연결되어 있지 않습니다.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
