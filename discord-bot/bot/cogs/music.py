import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.playlist import PlaylistManager
from bot.services.ytdlp import search_song


# ============================================================
# Music Cog
# ============================================================

class Music(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

        self.playlist_manager = PlaylistManager()

        # 서버별 재생 상태
        self.playing = {}

        # 서버별 플레이리스트 재생 중인지
        self.playlist_playing = {}


    # ========================================================
    # Voice
    # ========================================================

    async def ensure_voice(
        self,
        interaction: discord.Interaction
    ):
        """
        사용자가 있는 음성 채널에 봇을 연결합니다.
        """

        if not interaction.user.voice:
            await interaction.followup.send(
                "먼저 음성 채널에 들어가 주세요."
            )

            return None

        channel = interaction.user.voice.channel

        voice_client = interaction.guild.voice_client

        if voice_client is None:
            voice_client = await channel.connect()

        elif voice_client.channel != channel:
            await voice_client.move_to(channel)

        return voice_client


    # ========================================================
    # Audio
    # ========================================================

    async def create_audio_source(
        self,
        stream_url: str
    ):
        """
        yt-dlp가 반환한 스트림 URL을
        FFmpeg AudioSource로 변환합니다.
        """

        ffmpeg_options = {
            "before_options": (
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 5"
            ),
            "options": "-vn",
        }

        return discord.FFmpegPCMAudio(
            stream_url,
            **ffmpeg_options
        )


    # ========================================================
    # /노래
    # ========================================================

    @app_commands.command(
        name="노래",
        description="노래를 검색하거나 URL로 재생합니다."
    )
    @app_commands.describe(
        query="유튜브 URL 또는 검색할 노래 이름"
    )
    async def play(
        self,
        interaction: discord.Interaction,
        query: str
    ):

        await interaction.response.defer()

        if interaction.guild is None:
            await interaction.followup.send(
                "서버에서만 사용할 수 있습니다."
            )
            return

        voice_client = await self.ensure_voice(
            interaction
        )

        if voice_client is None:
            return

        try:
            song = await search_song(query)

            if not song["stream_url"]:
                raise RuntimeError(
                    "오디오 스트림을 찾을 수 없습니다."
                )

            # 기존 재생 중인 음악 정지
            if voice_client.is_playing():
                voice_client.stop()

            source = await self.create_audio_source(
                song["stream_url"]
            )

            source = discord.PCMVolumeTransformer(
                source,
                volume=0.5
            )

            voice_client.play(
                source,
                after=lambda error:
                    self._player_finished(
                        interaction.guild.id,
                        error
                    )
            )

            self.playing[
                interaction.guild.id
            ] = song

            embed = discord.Embed(
                title="🎵 음악 재생",
                description=song["title"],
                color=discord.Color.blurple()
            )

            if song["thumbnail"]:
                embed.set_thumbnail(
                    url=song["thumbnail"]
                )

            await interaction.followup.send(
                embed=embed
            )

        except Exception as error:

            await interaction.followup.send(
                f"❌ 음악 재생 중 오류가 발생했습니다.\n"
                f"`{error}`"
            )


    # ========================================================
    # /플리
    # ========================================================

    @app_commands.command(
        name="플리",
        description="현재 서버의 플레이리스트를 확인합니다."
    )
    async def playlist(
        self,
        interaction: discord.Interaction
    ):

        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.",
                ephemeral=True
            )
            return

        songs = self.playlist_manager.get_playlist(
            interaction.guild.id
        )

        if not songs:
            await interaction.response.send_message(
                "🎵 플레이리스트가 비어있습니다.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎵 플레이리스트",
            description=(
                f"총 **{len(songs)}곡**"
            ),
            color=discord.Color.blurple()
        )

        text = ""

        for index, song in enumerate(
            songs,
            1
        ):

            text += (
                f"**{index}.** "
                f"[{song['title']}]"
                f"({song['url']})\n"
            )

        # Discord Embed description 제한 대응
        if len(text) > 4000:
            text = text[:3990] + "\n..."

        embed.description += "\n\n" + text

        await interaction.response.send_message(
            embed=embed
        )


    # ========================================================
    # /플리추가
    # ========================================================

    @app_commands.command(
        name="플리추가",
        description="플레이리스트에 노래를 추가합니다."
    )
    @app_commands.describe(
        query="유튜브 URL 또는 검색할 노래 이름"
    )
    async def playlist_add(
        self,
        interaction: discord.Interaction,
        query: str
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        if interaction.guild is None:
            await interaction.followup.send(
                "서버에서만 사용할 수 있습니다."
            )
            return

        try:

            song = await search_song(query)

            self.playlist_manager.add_song(
                interaction.guild.id,
                song["title"],
                song["url"]
            )

            songs = self.playlist_manager.get_playlist(
                interaction.guild.id
            )

            await interaction.followup.send(
                f"✅ **{song['title']}**\n"
                f"플레이리스트에 추가했습니다.\n\n"
                f"현재 총 **{len(songs)}곡**입니다."
            )

        except Exception as error:

            await interaction.followup.send(
                f"❌ 노래를 추가할 수 없습니다.\n"
                f"`{error}`"
            )


    # ========================================================
    # /플리재생
    # ========================================================

    @app_commands.command(
        name="플리재생",
        description="플레이리스트의 노래를 순서대로 재생합니다."
    )
    async def playlist_play(
        self,
        interaction: discord.Interaction
    ):

        await interaction.response.defer()

        if interaction.guild is None:
            await interaction.followup.send(
                "서버에서만 사용할 수 있습니다."
            )
            return

        guild_id = interaction.guild.id

        songs = self.playlist_manager.get_playlist(
            guild_id
        )

        if not songs:
            await interaction.followup.send(
                "🎵 플레이리스트가 비어있습니다."
            )
            return

        if self.playlist_playing.get(
            guild_id,
            False
        ):
            await interaction.followup.send(
                "이미 플레이리스트를 재생 중입니다."
            )
            return

        voice_client = await self.ensure_voice(
            interaction
        )

        if voice_client is None:
            return

        self.playlist_playing[
            guild_id
        ] = True

        await interaction.followup.send(
            f"▶️ 플레이리스트 재생을 시작합니다.\n"
            f"총 **{len(songs)}곡**"
        )

        await self.play_playlist(
            interaction,
            songs
        )


    # ========================================================
    # Playlist Player
    # ========================================================

    async def play_playlist(
        self,
        interaction,
        songs
    ):

        guild_id = interaction.guild.id

        voice_client = (
            interaction.guild.voice_client
        )

        try:

            for index, song in enumerate(
                songs,
                1
            ):

                if not self.playlist_playing.get(
                    guild_id,
                    False
                ):
                    break

                # 스트림 URL 재생성
                data = await search_song(
                    song["url"]
                )

                stream_url = data["stream_url"]

                source = await self.create_audio_source(
                    stream_url
                )

                source = discord.PCMVolumeTransformer(
                    source,
                    volume=0.5
                )

                # 이전 재생이 끝날 때까지 대기
                finished = asyncio.Event()

                def after(error):

                    if error:
                        print(
                            f"Player error: {error}"
                        )

                    self.bot.loop.call_soon_threadsafe(
                        finished.set
                    )

                voice_client.play(
                    source,
                    after=after
                )

                self.playing[
                    guild_id
                ] = song

                await interaction.channel.send(
                    f"▶️ **{index}/{len(songs)}**\n"
                    f"{song['title']}"
                )

                await finished.wait()

        except Exception as error:

            await interaction.channel.send(
                f"❌ 플레이리스트 재생 중 오류:\n"
                f"`{error}`"
            )

        finally:

            self.playlist_playing[
                guild_id
            ] = False

            self.playing.pop(
                guild_id,
                None
            )


    # ========================================================
    # Player Callback
    # ========================================================

    def _player_finished(
        self,
        guild_id,
        error
    ):

        if error:
            print(
                f"Player error: {error}"
            )


    # ========================================================
    # Cog
    # ========================================================


async def setup(bot):

    await bot.add_cog(
        Music(bot)
    )