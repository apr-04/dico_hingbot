"""
음악 재생.

대기열에는 유튜브 페이지 URL만 저장하고 재생 직전에 스트림 URL을 조회한다.
스트림 URL이 수 시간 후 만료되므로 미리 받아두면 뒷 순번 곡이 실패한다.

모든 명령어는 사용자가 접속한 음성 채널의 내장 채팅창에서만 동작한다.
해당 채팅창의 ID는 음성 채널 ID와 동일하다.
"""

import asyncio
import logging
import os
import random
import sys
import time
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.hangul import particle  # noqa: E402

log = logging.getLogger("music")

# ── 설정값 ───────────────────────────────────────────────────

IDLE_TIMEOUT = 300    # 대기열이 빈 채로 이 시간이 지나면 퇴장
MAX_QUEUE_SHOW = 10
HISTORY_SIZE = 50
SEARCH_RESULTS = 5    # 디스코드 드롭다운 최대 25
SEARCH_TIMEOUT = 60
EMBED_COLOR = 0xF06292
AUTOPLAY_FETCH = 20   # 믹스에서 훑어볼 곡 수
AUTOPLAY_ADD = 3      # 그중 대기열에 넣을 수

# 반복 모드: off | one | all | shuffle
LOOP_LABELS = {"off": "끔", "one": "한곡 반복", "all": "반복", "shuffle": "무작위 반복"}
LOOP_MESSAGES = {
    "one": "현재 노래를 반복할게용가리 🐲",
    "all": "현재 플레이 리스트를 반복할게용가리 🐲",
    "shuffle": "현재 플레이 리스트를 무작위로 반복할게용가리 🐲",
    "off": "반복 모드를 해제할게용가리 🐲",
}

# -vn: 영상 무시. -reconnect: 스트리밍 중 네트워크 단절 시 자동 재접속
FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -loglevel warning"
)
FFMPEG_OPTIONS = "-vn"


def _build_ytdl_opts(flat: bool) -> dict:
    """yt-dlp 옵션. flat=True는 목록 조회용(스트림 URL 미포함), False는 재생 직전 정밀 조회."""
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "skip_download": True,
        "default_search": "ytsearch",  # URL이 아닌 일반 텍스트가 오면 유튜브 검색으로 처리
        "source_address": "0.0.0.0",
        "retries": 3,
    }
    if flat:
        opts["extract_flat"] = "in_playlist"

    cookie = os.getenv("YTDL_COOKIE_FILE")
    if cookie and os.path.exists(cookie):
        opts["cookiefile"] = cookie
    return opts


def _youtube_video_id(url: str) -> str | None:
    """유튜브 주소에서 영상 ID만 뽑아냅니다. (youtu.be 짧은 주소도 처리)"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.hostname and "youtu.be" in parsed.hostname:
        return parsed.path.lstrip("/") or None
    return (parse_qs(parsed.query).get("v") or [None])[0]


def _fmt_time(sec: float) -> str:
    """초를 3:45 / 1:02:03 같은 형태로 바꿉니다."""
    m, s = divmod(int(max(sec, 0)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _progress_bar(elapsed: float, total: int | None, width: int = 18) -> str:
    """진행바. 길이를 모르면 LIVE 표시."""
    if not total:
        return f"🔴 LIVE · {_fmt_time(elapsed)} 경과"
    ratio = min(max(elapsed / total, 0.0), 1.0)
    pos = int(ratio * (width - 1))
    bar = "".join("🔘" if i == pos else "▬" for i in range(width))
    return f"{bar}\n`{_fmt_time(elapsed)} / {_fmt_time(total)}`"


@dataclass
class Track:
    """대기열에 들어가는 곡 한 개의 정보."""

    title: str
    url: str                      # 유튜브 페이지 주소 (스트림 주소가 아님에 주의)
    duration: int | None = None   # 초 단위
    thumbnail: str | None = None
    uploader: str | None = None
    requester_id: int | None = None
    auto: bool = False            # 자동재생이 알아서 넣은 곡인지

    @property
    def duration_str(self) -> str:
        return _fmt_time(self.duration) if self.duration else "LIVE"

    @property
    def image_url(self) -> str | None:
        """썸네일 URL. yt-dlp가 준 값이 없으면 영상 ID로 조립한다."""
        if self.thumbnail:
            return self.thumbnail
        video_id = _youtube_video_id(self.url)
        return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else None


class GuildPlayer:
    """길드 하나의 재생 상태. 생성 시 재생 루프 태스크를 띄운다."""

    def __init__(self, cog: "Music", interaction: discord.Interaction, voice: discord.VoiceClient):
        self.cog = cog
        self.bot = cog.bot
        self.guild_id = interaction.guild_id
        self.voice = voice
        self.text_channel = interaction.channel  # 안내 메시지를 보낼 채널

        self.queue: deque[Track] = deque()
        # 재생 완료된 곡. 맨 뒤가 직전 곡이며 maxlen 초과분은 자동으로 밀려난다.
        self.history: deque[Track] = deque(maxlen=HISTORY_SIZE)
        self.current: Track | None = None
        self.volume: float = 0.5     # 0.0 ~ 1.0 (0.5 = 50%)
        self.loop_mode: str = "off"  # off | one | all | shuffle
        self.autoplay: bool = False  # 대기열이 비면 관련곡을 알아서 이어 틀지

        self._song_finished = asyncio.Event()  # 곡 재생이 끝났다는 신호
        self._track_added = asyncio.Event()    # 대기열에 곡이 들어왔다는 신호
        self._skipped = False                  # 사용자가 일부러 중단시켰는지 여부
        self._skip_history_once = False        # 이번 곡은 재생기록에 남기지 않음 (/이전 전용)
        self._suppress_announce = False        # 다음 곡 카드를 띄우지 않음 (명령어가 이미 띄웠을 때)

        # 디스코드가 재생 위치를 제공하지 않아 직접 측정한다.
        self._started_at: float | None = None  # 재생 시작 시각
        self._paused_at: float | None = None   # 일시정지가 시작된 시각
        self._paused_total: float = 0.0        # 지금까지 멈춰 있던 시간의 합

        self._task = self.bot.loop.create_task(self._player_loop())

    # ── 진행 시간 ────────────────────────────────────────────

    @property
    def elapsed(self) -> float:
        """현재 곡을 몇 초째 재생 중인지. 일시정지한 시간은 빼고 셉니다."""
        if self._started_at is None:
            return 0.0
        now = time.monotonic()
        value = now - self._started_at - self._paused_total
        if self._paused_at is not None:  # 지금도 멈춰 있는 중이면 그만큼 더 뺍니다
            value -= now - self._paused_at
        return max(0.0, value)

    def mark_paused(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def mark_resumed(self) -> None:
        if self._paused_at is not None:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None

    # ── 대기열 조작 ──────────────────────────────────────────

    def add(self, track: Track, front: bool = False) -> None:
        """front=True면 대기열 맨 앞에 넣는다."""
        if front:
            self.queue.appendleft(track)
        else:
            self.queue.append(track)
        self._track_added.set()

    def remove_at(self, number: int) -> Track:
        """1부터 시작하는 번호로 대기열에서 곡을 빼냅니다."""
        items = list(self.queue)
        track = items.pop(number - 1)
        self.queue = deque(items)
        return track

    def move_to_front(self, number: int) -> Track:
        """대기열의 특정 곡을 맨 앞으로 끌어옵니다."""
        track = self.remove_at(number)
        self.queue.appendleft(track)
        return track

    def skip(self) -> None:
        """현재 곡 중단. _skipped를 켜야 한곡 반복 중에도 다음 곡으로 넘어간다."""
        self._skipped = True
        if self.voice.is_playing() or self.voice.is_paused():
            self.voice.stop()  
    def play_previous(self) -> Track | None:
        """직전 곡으로 되돌아간다. 기록에서 꺼내 맨 앞에 놓고 현재 곡을 그 뒤에 다시 넣는다."""
        if not self.history:
            return None

        previous = self.history.pop()
        if self.current:
            self.queue.appendleft(self.current)
        self.queue.appendleft(previous)

        self._skip_history_once = True
        self._skipped = True
        # /이전 명령어가 카드를 직접 띄우므로, 재생 루프는 이번 한 번 조용히 넘어갑니다.
        self._suppress_announce = True
        if self.voice.is_playing() or self.voice.is_paused():
            self.voice.stop()
        else:
            self._track_added.set()  # 멈춰 있던 상태면 재생 루프를 깨워줍니다
        return previous

    async def _wait_for_track(self) -> Track:
        """대기열에 곡이 생길 때까지 기다렸다가 맨 앞 곡을 꺼냅니다."""
        while not self.queue:
            self._track_added.clear()
            await self._track_added.wait()
        return self.queue.popleft()

    # ── 재생 루프 (이 봇의 심장부) ───────────────────────────

    async def _player_loop(self) -> None:
        while True:
            self._song_finished.clear()

            # 다음 곡 결정. 사용자가 직접 중단한 경우(_skipped)엔 한곡 반복을 무시한다.
            if self.loop_mode == "one" and self.current and not self._skipped:
                track = self.current
            else:
                if self.loop_mode in ("all", "shuffle") and self.current:
                    self.queue.append(self.current)  # 반복 모드: 방금 튼 곡을 맨 뒤로

                # 무작위 반복: 임의의 곡을 맨 앞으로 옮겨두면 다음 pop이 무작위 선택이 된다.
                if self.loop_mode == "shuffle" and len(self.queue) > 1:
                    items = list(self.queue)
                    items.insert(0, items.pop(random.randrange(len(items))))
                    self.queue = deque(items)

                # 대기열 소진 시 관련곡을 채운다.
                if self.autoplay and not self.queue and self.current:
                    await self._fill_autoplay()

                try:
                    track = await asyncio.wait_for(self._wait_for_track(), timeout=IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._notify(
                        f"{IDLE_TIMEOUT // 60}분간 재생이 없어 음성 채널에서 나갑니다."
                    )
                    return await self.destroy()

            self._skipped = False  # 한 번 쓰고 바로 꺼줍니다

            # 재생 기록 적재. 한곡 반복으로 같은 곡이 재선택된 경우는 제외한다.
            if self.current and self.current is not track and not self._skip_history_once:
                self.history.append(self.current)
            self._skip_history_once = False

            self.current = track

            try:
                stream_url, thumb = await self.cog.resolve_stream(track.url)
                if thumb:
                    track.thumbnail = thumb  # 빠른 조회 때는 못 받았던 썸네일을 채웁니다
            except Exception as e:
                log.warning("스트림 주소 조회 실패: %s (%s)", track.title, e)
                await self._notify(f"`{track.title}`{particle(track.title)} 재생할 수 없어 건너뜁니다.")
                continue

            # 볼륨 조절을 위해 PCMVolumeTransformer로 감싼다.
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    stream_url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                    executable=os.getenv("FFMPEG_PATH", "ffmpeg"),
                ),
                volume=self.volume,
            )

            # after 콜백은 다른 스레드에서 실행되므로 await 대신 신호만 넘긴다.
            def _after(error: Exception | None) -> None:
                if error:
                    log.error("재생 중 오류: %s", error)
                self.bot.loop.call_soon_threadsafe(self._song_finished.set)

            # 진행 시간 측정 초기화
            self._started_at = time.monotonic()
            self._paused_at = None
            self._paused_total = 0.0

            self.voice.play(source, after=_after)

            # 명령어가 이미 카드를 띄운 경우 중복 방지
            if self._suppress_announce:
                self._suppress_announce = False
            else:
                await self._notify(
                    embed=self.now_playing_embed(title="노래를 추가할게용가리 🐲")
                )

            # 곡 종료까지 대기
            await self._song_finished.wait()

    # ── 자동재생 ─────────────────────────────────────────────

    async def _fill_autoplay(self) -> None:
        """관련곡으로 대기열을 채운다. 이미 재생한 곡은 제외한다."""
        seed = self.current
        try:
            candidates = await self.cog.fetch_related(seed.url)
        except Exception as e:
            log.warning("자동재생 관련곡 조회 실패: %s", e)
            return

        # 이미 재생한 곡 제외
        played = {t.url for t in self.history}
        played.add(seed.url)

        picked = [t for t in candidates if t.url not in played][:AUTOPLAY_ADD]
        if not picked:
            log.info("자동재생: 새로 틀 만한 관련곡을 찾지 못했습니다")
            return

        for track in picked:
            track.auto = True
            self.add(track)
        await self._notify(
            f"🎲 자동재생: **{picked[0].title}** 외 {len(picked) - 1}곡을 이어서 재생합니다."
            if len(picked) > 1
            else f"🎲 자동재생: **{picked[0].title}**{particle(picked[0].title)} 이어서 재생합니다."
        )

    # ── 보조 기능 ────────────────────────────────────────────

    def now_playing_embed(
        self,
        with_progress: bool = False,
        title: str = "지금 재생 중",
        track: "Track | None" = None,
    ) -> discord.Embed:
        """곡 정보 카드. track을 넘기면 아직 재생 전인 곡으로도 만들 수 있다."""
        # track 인자는 아직 재생 전인 곡을 미리 보여줄 때 쓴다. (/이전, /다음)
        track = track or self.current
        # 디스코드가 앞뒤 공백을 제거하므로 제로 폭 공백으로 빈 줄을 유지한다.
        embed = discord.Embed(
            title=title,
            description=f"​\n**[{track.title} ({track.duration_str})]({track.url})**",
            color=EMBED_COLOR,
        )
        if with_progress:
            embed.add_field(
                name="진행", value=_progress_bar(self.elapsed, track.duration), inline=False
            )

        # 곡 시작 카드는 제목과 썸네일만 남긴다.
        if with_progress:
            if track.uploader:
                embed.add_field(name="채널", value=track.uploader, inline=True)
            if track.auto:
                embed.add_field(name="신청", value="🎲 자동재생", inline=True)
            elif track.requester_id:
                embed.add_field(name="신청", value=f"<@{track.requester_id}>", inline=True)

        # 임베드 요소 순서는 제목-설명-필드-이미지로 고정이라 이미지를 중간에 넣을 수 없다.
        image = track.image_url
        if image:
            if with_progress:
                embed.set_thumbnail(url=image)
            else:
                embed.set_image(url=image)

        # 상태 줄은 /지금곡 에서만 노출한다.
        if with_progress:
            mode_label = LOOP_LABELS[self.loop_mode]
            state = "일시정지" if self.voice.is_paused() else "재생 중"
            footer = (
                f"{state} · 대기열 {len(self.queue)}곡 · 반복 {mode_label} "
                f"· 볼륨 {int(self.volume * 100)}%"
            )
            if self.autoplay:
                footer += " · 자동재생 켜짐"
            embed.set_footer(text=footer)
        return embed

    async def _notify(self, content: str | None = None, embed: discord.Embed | None = None) -> None:
        """안내 메시지를 보냅니다. 채널이 삭제됐거나 권한이 없어도 봇이 죽지 않도록 감쌉니다."""
        if not self.text_channel:
            return
        try:
            await self.text_channel.send(content=content, embed=embed)
        except discord.HTTPException:
            pass

    async def destroy(self) -> None:
        """재생을 완전히 정리하고 음성채널에서 나갑니다."""
        self.queue.clear()
        self.current = None
        if self._task and not self._task.done():
            self._task.cancel()
        if self.voice and self.voice.is_connected():
            await self.voice.disconnect(force=True)
        self.cog.players.pop(self.guild_id, None)


class SearchView(discord.ui.View):
    """검색 결과 드롭다운. timeout 경과 시 비활성화된다."""

    def __init__(self, player: GuildPlayer, tracks: list[Track], requester_id: int):
        super().__init__(timeout=SEARCH_TIMEOUT)
        self.player = player
        self.tracks = tracks
        self.requester_id = requester_id
        self.message: discord.Message | None = None

        options = []
        for i, track in enumerate(tracks):
            desc = track.duration_str
            if track.uploader:
                desc += f" · {track.uploader}"
            options.append(
                discord.SelectOption(
                    label=track.title[:100],   # 디스코드 제한: 라벨 100자
                    description=desc[:100],
                    value=str(i),
                )
            )

        select = discord.ui.Select(placeholder="재생할 곡을 고르세요", options=options)

        async def on_select(interaction: discord.Interaction) -> None:
            # 검색을 요청한 사용자만 선택할 수 있게 제한한다.
            if interaction.user.id != self.requester_id:
                return await interaction.response.send_message(
                    "검색한 사람만 고를 수 있습니다.", ephemeral=True
                )

            track = self.tracks[int(select.values[0])]
            was_idle = not self.player.voice.is_playing() and not self.player.queue
            self.player.add(track)
            self.stop()  # 드롭다운을 더 이상 쓰지 못하게 닫습니다

            msg = (
                "노래를 준비할게용가리 🐲"
                if was_idle
                else f"대기열에 담았어용가리 🐲\n**{track.title}** ({track.duration_str})"
            )
            await interaction.response.edit_message(content=msg, embed=None, view=None)

        select.callback = on_select
        self.add_item(select)

    async def on_timeout(self) -> None:
        """시간이 지나면 드롭다운을 치우고 안내 문구로 바꿉니다."""
        if self.message:
            try:
                await self.message.edit(
                    content="검색 시간이 지났습니다. 다시 검색해주세요.", embed=None, view=None
                )
            except discord.HTTPException:
                pass


class Music(commands.Cog):
    """/재생, /스킵 같은 슬래시 명령어들이 정의된 곳."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}  # {서버ID: 재생기}

    # ── yt-dlp 호출 (느린 작업이라 별도 스레드에서 실행) ─────

    async def _ytdl_extract(self, query: str, flat: bool) -> dict | None:
        """yt-dlp는 블로킹이므로 별도 스레드에서 실행한다."""

        def _run() -> dict | None:
            with yt_dlp.YoutubeDL(_build_ytdl_opts(flat)) as ydl:
                return ydl.extract_info(query, download=False)

        return await asyncio.to_thread(_run)

    async def resolve_stream(self, page_url: str) -> tuple[str, str | None]:
        """페이지 URL -> (스트림 URL, 썸네일). 목록 조회는 썸네일을 주지 않아 여기서 함께 받는다."""
        info = await self._ytdl_extract(page_url, flat=False)
        if not info:
            raise RuntimeError("영상 정보를 가져오지 못했습니다")
        if "entries" in info:  # 혹시 재생목록이 왔다면 첫 곡만 사용
            info = next((e for e in info["entries"] if e), None)
            if not info:
                raise RuntimeError("재생 가능한 항목이 없습니다")
        url = info.get("url")
        if not url:
            raise RuntimeError("오디오 스트림을 찾지 못했습니다")
        return url, info.get("thumbnail")

    async def fetch_related(self, page_url: str) -> list[Track]:
        """관련곡 목록. 유튜브 믹스(list=RD<영상ID>)를 읽는다."""
        video_id = _youtube_video_id(page_url)
        if not video_id:
            return []

        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        info = await self._ytdl_extract(mix_url, flat=True)
        if not info or "entries" not in info:
            return []

        tracks: list[Track] = []
        for e in info["entries"][:AUTOPLAY_FETCH]:
            if not e:
                continue
            url = e.get("webpage_url") or e.get("url") or ""
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={e.get('id')}"
            if not url or _youtube_video_id(url) == video_id:  # 시드 곡 제외
                continue
            tracks.append(
                Track(
                    title=e.get("title") or "제목 없음",
                    url=url,
                    duration=e.get("duration"),
                    thumbnail=e.get("thumbnail"),
                    uploader=e.get("uploader") or e.get("channel"),
                    auto=True,
                )
            )
        return tracks

    async def search(self, query: str, requester_id: int, limit: int = 1) -> list[Track]:
        """검색어 또는 URL로 Track 목록 생성. limit은 텍스트 검색 시 후보 수."""
        is_url = query.startswith(("http://", "https://"))
        target = query if is_url else f"ytsearch{limit}:{query}"

        info = await self._ytdl_extract(target, flat=True)
        if not info:
            return []

        entries = info["entries"] if "entries" in info else [info]
        tracks: list[Track] = []
        for e in entries:
            if not e:
                continue
            # flat 모드는 영상 ID만 주는 경우가 있어 보정한다.
            page_url = e.get("webpage_url") or e.get("url") or ""
            if page_url and not page_url.startswith("http"):
                page_url = f"https://www.youtube.com/watch?v={e.get('id')}"
            if not page_url:
                continue
            tracks.append(
                Track(
                    title=e.get("title") or "제목 없음",
                    url=page_url,
                    duration=e.get("duration"),
                    thumbnail=e.get("thumbnail"),
                    uploader=e.get("uploader") or e.get("channel"),
                    requester_id=requester_id,
                )
            )
        return tracks

    # ── 사용 위치 검사 ───────────────────────────────────────

    async def _guard(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        """사용 가능 여부 검사 후 음성 채널 반환. 음성 채널의 내장 채팅창 ID는 채널 ID와 같다."""
        voice_state = getattr(interaction.user, "voice", None)
        if not voice_state or not voice_state.channel:
            await self._reply(interaction, "먼저 음성 채널에 들어가주세요.", ephemeral=True)
            return None

        channel = voice_state.channel
        if interaction.channel_id != channel.id:
            await self._reply(
                interaction,
                f"여기서는 사용할 수 없습니다.\n"
                f"**{channel.name}** 음성 채널의 채팅창에서 입력해주세요.\n"
                f"(음성 채널을 클릭하면 오른쪽에 채팅창이 열립니다)",
                ephemeral=True,
            )
            return None
        return channel

    async def _ensure_voice(self, interaction: discord.Interaction) -> GuildPlayer | None:
        """검사를 통과했으면 재생기를 돌려줍니다. 봇이 음성채널에 없으면 접속시킵니다."""
        channel = await self._guard(interaction)
        if not channel:
            return None

        player = self.players.get(interaction.guild_id)
        if player and player.voice.is_connected():
            # 다른 채널에서 재생 중이고 청취자가 있으면 이동하지 않는다.
            if player.voice.channel != channel and len(player.voice.channel.members) > 1:
                await self._reply(
                    interaction,
                    f"봇이 이미 **{player.voice.channel.name}** 에서 재생 중입니다.",
                    ephemeral=True,
                )
                return None
            player.text_channel = interaction.channel
            return player

        voice = await channel.connect(self_deaf=True)
        player = GuildPlayer(self, interaction, voice)
        self.players[interaction.guild_id] = player
        return player

    async def _active_player(self, interaction: discord.Interaction) -> GuildPlayer | None:
        """이미 재생 중인 재생기를 가져옵니다. (봇을 새로 접속시키지는 않습니다)"""
        if not await self._guard(interaction):
            return None
        player = self.players.get(interaction.guild_id)
        if not player:
            await self._reply(interaction, "재생 중이 아닙니다.", ephemeral=True)
            return None
        return player

    @staticmethod
    async def _reply(interaction: discord.Interaction, content: str = "", **kwargs) -> None:
        """빈 문자열 content는 거부되므로 None으로 바꿔 보낸다."""
        text = content if content else None
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, **kwargs)
            else:
                await interaction.response.send_message(text, **kwargs)
        except discord.HTTPException:
            # 실패 원인을 로그에 남겨야 조용히 묻히지 않습니다.
            log.exception("응답 전송 실패 (content=%r, kwargs=%s)", text, list(kwargs))
            raise

    # ── 자동완성 ─────────────────────────────────────────────

    async def _queue_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """대기열을 자동완성 후보로 제공한다. 디스코드 제한상 최대 25개."""
        player = self.players.get(interaction.guild_id)
        if not player or not player.queue:
            return []
        results: list[app_commands.Choice[int]] = []
        for i, track in enumerate(player.queue, start=1):
            label = f"{i}. {track.title}"
            if current and current.lower() not in label.lower():
                continue
            results.append(app_commands.Choice(name=label[:100], value=i))
            if len(results) >= 25:
                break
        return results

    # ── 슬래시 명령어 ────────────────────────────────────────

    @app_commands.command(name="재생", description="노래를 재생하거나 대기열에 추가합니다.")
    @app_commands.describe(위치="대기열의 어디에 넣을지 — 비워두면 맨 뒤")
    @app_commands.choices(
        위치=[
            app_commands.Choice(name="대기열 맨 뒤 (기본)", value="back"),
            app_commands.Choice(name="다음 곡으로 — 현재 곡이 끝나면 재생", value="front"),
            app_commands.Choice(name="지금 바로 — 현재 곡을 중단하고 재생", value="now"),
        ]
    )
    async def play(
        self,
        interaction: discord.Interaction,
        검색어: str,
        위치: app_commands.Choice[str] | None = None,
    ) -> None:
        # 디스코드는 3초 내 응답이 없으면 실패 처리하므로 먼저 defer 한다.
        await interaction.response.defer()

        player = await self._ensure_voice(interaction)
        if not player:
            return

        try:
            tracks = await self.search(검색어, interaction.user.id)
        except Exception as e:
            log.exception("검색 실패")
            return await self._reply(interaction, f"검색 중 오류가 발생했습니다: `{e}`")

        if not tracks:
            return await self._reply(interaction, "검색 결과가 없습니다.")

        was_idle = not player.voice.is_playing() and not player.queue
        mode = 위치.value if 위치 else "back"

        if mode == "back":
            for t in tracks:
                player.add(t)
        else:
            # appendleft는 순서를 뒤집으므로 역순으로 넣는다.
            for t in reversed(tracks):
                player.add(t, front=True)

        # now 모드: 맨 앞에 넣고 현재 곡을 중단시킨다.
        interrupted = None
        if mode == "now" and not was_idle:
            interrupted = player.current
            player.skip()

        head = tracks[0]
        label = {"back": "대기열에 담았어용가리 🐲", "front": "다음 노래로 담았어용가리 🐲", "now": "바로 틀게용가리 🐲"}[mode]

        if len(tracks) == 1:
            msg = (
                "노래를 준비할게용가리 🐲"
                if was_idle
                else f"{label}\n**{head.title}** ({head.duration_str})"
            )
        else:
            msg = f"{label}\n재생목록 **{len(tracks)}곡**"

        if interrupted:
            msg += f"\n**{interrupted.title}**{particle(interrupted.title, '은/는')} 건너뛸게용가리"

        await self._reply(interaction, msg)

    @app_commands.command(name="검색", description="노래를 재생하거나 대기열에 추가합니다.")
    async def search_cmd(self, interaction: discord.Interaction, 검색어: str) -> None:
        await interaction.response.defer()

        player = await self._ensure_voice(interaction)
        if not player:
            return

        try:
            tracks = await self.search(검색어, interaction.user.id, limit=SEARCH_RESULTS)
        except Exception as e:
            log.exception("검색 실패")
            return await self._reply(interaction, f"검색 중 오류가 발생했습니다: `{e}`")

        if not tracks:
            return await self._reply(interaction, "검색 결과가 없습니다.")

        embed = discord.Embed(
            title=f"'{검색어}'{particle(검색어)} 검색했어용가리 🐲",
            color=EMBED_COLOR,
            description="\n".join(
                f"`{i}.` [{t.title} ({t.duration_str})]({t.url})"
                for i, t in enumerate(tracks, start=1)
            ),
        )

        view = SearchView(player, tracks, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)
        # on_timeout에서 수정하기 위해 메시지 객체를 보관
        view.message = await interaction.original_response()

    @app_commands.command(name="지금곡", description="현재 재생 중인 곡과 진행 상황을 봅니다.")
    async def now_playing(self, interaction: discord.Interaction) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        if not player.current:
            return await self._reply(interaction, "재생 중인 곡이 없습니다.", ephemeral=True)
        await self._reply(interaction, "", embed=player.now_playing_embed(with_progress=True))

    async def _do_skip(self, interaction: discord.Interaction) -> None:
        """/스킵 과 /다음 이 공유하는 실제 동작."""
        player = await self._active_player(interaction)
        if not player:
            return
        if not (player.voice.is_playing() or player.voice.is_paused()):
            return await self._reply(interaction, "재생 중인 곡이 없습니다.", ephemeral=True)
        # skip() 이후에는 재생 루프가 이미 pop 했을 수 있어 미리 조회한다.
        if player.queue:
            upcoming = player.queue[0]
        elif player.loop_mode in ("all", "shuffle"):
            upcoming = player.current  # 전체 반복 중이고 큐가 비면 현재 곡이 다시 걸립니다
        else:
            upcoming = None            # 자동재생이 곡을 찾아올 수도 있어 아직 알 수 없음

        if upcoming:
            player._suppress_announce = True  # 카드를 여기서 띄우니 루프는 조용히
        player.skip()

        if upcoming:
            await self._reply(
                interaction,
                "",
                embed=player.now_playing_embed(
                    title="다음 노래를 재생했어용가리 🐲", track=upcoming
                ),
            )
        else:
            await self._reply(interaction, "다음 노래로 넘어갈게용가리 🐲")

    @app_commands.command(name="스킵", description="다음 노래가 재생됩니다.")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self._do_skip(interaction)

    @app_commands.command(name="다음", description="다음 노래가 재생됩니다.")
    async def next_track(self, interaction: discord.Interaction) -> None:
        await self._do_skip(interaction)

    @app_commands.command(name="이전", description="직전에 들었던 노래가 재생됩니다.")
    async def previous_track(self, interaction: discord.Interaction) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        track = player.play_previous()
        if not track:
            return await self._reply(interaction, "이전 노래가 없어용가리 🐲", ephemeral=True)
        await self._reply(
            interaction,
            "",
            embed=player.now_playing_embed(
                title="이전 노래를 재생했어용가리 🐲", track=track
            ),
        )

    @app_commands.command(name="최근", description="최근에 재생한 곡 목록을 봅니다.")
    async def recent(self, interaction: discord.Interaction) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        if not player.history:
            return await self._reply(interaction, "아직 재생 기록이 없습니다.", ephemeral=True)

        # 최신순 정렬
        items = list(reversed(player.history))[:MAX_QUEUE_SHOW]
        lines = [
            f"`{i}.` [{t.title} ({t.duration_str})]({t.url})"
            for i, t in enumerate(items, start=1)
        ]
        embed = discord.Embed(
            title="최근 재생한 곡", color=EMBED_COLOR, description="\n".join(lines)
        )
        if player.current:
            embed.add_field(
                name="지금 재생 중",
                value=f"[{player.current.title} ({player.current.duration_str})]({player.current.url})",
                inline=False,
            )
        embed.set_footer(text=f"총 {len(player.history)}곡 기록 · `/이전` 으로 되돌아갈 수 있어요")
        await self._reply(interaction, "", embed=embed)

    @app_commands.command(name="일시정지", description="노래를 일시정지합니다.")
    async def pause(self, interaction: discord.Interaction) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        if not player.voice.is_playing():
            return await self._reply(interaction, "재생 중인 곡이 없습니다.", ephemeral=True)
        player.voice.pause()
        player.mark_paused()
        await self._reply(interaction, "노래를 일시정지했어용가리 🐲")

    @app_commands.command(name="다시재생", description="일시정지를 해제합니다.")
    async def resume(self, interaction: discord.Interaction) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        if not player.voice.is_paused():
            return await self._reply(interaction, "일시정지 상태가 아닙니다.", ephemeral=True)
        player.voice.resume()
        player.mark_resumed()
        # 곡이 시작될 때와 같은 카드(링크 한 줄 + 큰 썸네일)를 문구만 바꿔서 씁니다.
        await self._reply(
            interaction, "", embed=player.now_playing_embed(title="노래를 다시 재생했어용가리 🐲")
        )

    @app_commands.command(name="대기열", description="대기 중인 곡 목록을 봅니다.")
    async def show_queue(self, interaction: discord.Interaction) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        if not player.queue and not player.current:
            return await self._reply(interaction, "대기열이 비어 있습니다.", ephemeral=True)

        embed = discord.Embed(title="재생 대기열", color=EMBED_COLOR)
        if player.current:
            embed.add_field(
                name="지금 재생 중",
                value=f"**[{player.current.title} ({player.current.duration_str})]({player.current.url})**\n"
                f"{_progress_bar(player.elapsed, player.current.duration)}",
                inline=False,
            )
        if player.queue:
            lines = [
                f"`{i}.` [{t.title} ({t.duration_str})]({t.url})"
                for i, t in enumerate(list(player.queue)[:MAX_QUEUE_SHOW], start=1)
            ]
            rest = len(player.queue) - MAX_QUEUE_SHOW
            if rest > 0:
                lines.append(f"...외 **{rest}곡**")
            embed.add_field(
                name=f"다음 곡 ({len(player.queue)}곡)", value="\n".join(lines), inline=False
            )

        mode_label = LOOP_LABELS[player.loop_mode]
        embed.set_footer(text=f"반복: {mode_label} · 볼륨: {int(player.volume * 100)}%")
        await self._reply(interaction, "", embed=embed)

    @app_commands.command(name="맨앞으로", description="대기열의 특정 곡을 맨 앞으로 끌어옵니다.")
    @app_commands.describe(곡="대기열에서 앞으로 보낼 곡")
    @app_commands.autocomplete(곡=_queue_autocomplete)
    async def move_front(self, interaction: discord.Interaction, 곡: int) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        if not player.queue:
            return await self._reply(interaction, "대기열이 비어 있습니다.", ephemeral=True)
        if not 1 <= 곡 <= len(player.queue):
            return await self._reply(
                interaction, f"1~{len(player.queue)} 사이의 번호를 골라주세요.", ephemeral=True
            )
        track = player.move_to_front(곡)
        await self._reply(interaction, f"맨 앞으로 옮겼어용가리 🐲\n**{track.title}**{particle(track.title)} 다음에 틀게용가리")

    @app_commands.command(name="제거", description="대기열에서 특정 곡을 뺍니다.")
    @app_commands.describe(곡="대기열에서 제거할 곡")
    @app_commands.autocomplete(곡=_queue_autocomplete)
    async def remove(self, interaction: discord.Interaction, 곡: int) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        if not player.queue:
            return await self._reply(interaction, "대기열이 비어 있습니다.", ephemeral=True)
        if not 1 <= 곡 <= len(player.queue):
            return await self._reply(
                interaction, f"1~{len(player.queue)} 사이의 번호를 골라주세요.", ephemeral=True
            )
        track = player.remove_at(곡)
        await self._reply(interaction, f"대기열에서 뺐어용가리 🐲\n**{track.title}**")

    @app_commands.command(
        name="자동재생", description="대기열이 떨어지면 비슷한 곡을 알아서 이어 재생합니다."
    )
    @app_commands.describe(설정="켜기 / 끄기")
    @app_commands.choices(
        설정=[
            app_commands.Choice(name="켜기", value="on"),
            app_commands.Choice(name="끄기", value="off"),
        ]
    )
    async def autoplay_cmd(
        self, interaction: discord.Interaction, 설정: app_commands.Choice[str]
    ) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        player.autoplay = 설정.value == "on"

        if not player.autoplay:
            return await self._reply(
                interaction, "자동재생을 껐어용가리 🐲\n대기열이 끝나면 멈출게용가리"
            )

        msg = "자동재생을 켰어용가리 🐲\n대기열이 떨어지면 비슷한 노래를 이어 틀게용가리"
        # 대기열이 비어 있으면 즉시 채운다
        if not player.queue and player.current:
            await self._reply(interaction, msg + "\n비슷한 노래를 찾아볼게용가리")
            await player._fill_autoplay()
            return
        await self._reply(interaction, msg)

    @app_commands.command(name="반복", description="반복 모드를 설정합니다.")
    @app_commands.choices(
        모드=[
            app_commands.Choice(name="한곡 반복", value="one"),
            app_commands.Choice(name="반복", value="all"),
            app_commands.Choice(name="무작위 반복", value="shuffle"),
            app_commands.Choice(name="반복 해제", value="off"),
        ]
    )
    async def loop(self, interaction: discord.Interaction, 모드: app_commands.Choice[str]) -> None:
        player = await self._active_player(interaction)
        if not player:
            return

        player.loop_mode = 모드.value
        message = LOOP_MESSAGES[모드.value]

        if player.current:
            await self._reply(
                interaction, "", embed=player.now_playing_embed(title=message)
            )
        else:
            await self._reply(interaction, message)

    @app_commands.command(name="볼륨", description="음량을 조절합니다. (0~100)")
    @app_commands.describe(크기="0에서 100 사이의 숫자")
    async def volume(
        self, interaction: discord.Interaction, 크기: app_commands.Range[int, 0, 100]
    ) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        player.volume = 크기 / 100
        # 재생 중인 소스에 즉시 반영
        if player.voice.source and isinstance(player.voice.source, discord.PCMVolumeTransformer):
            player.voice.source.volume = player.volume
        await self._reply(interaction, f"볼륨을 **{크기}%** 로 맞췄어용가리 🐲")

    @app_commands.command(name="셔플", description="대기열 순서를 무작위로 섞습니다.")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        if len(player.queue) < 2:
            return await self._reply(
                interaction, "섞으려면 대기열에 2곡 이상 필요합니다.", ephemeral=True
            )
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await self._reply(interaction, f"대기열 **{len(items)}곡**을 섞었어용가리 🐲")

    @app_commands.command(name="정지", description="노래를 종료합니다.")
    async def stop(self, interaction: discord.Interaction) -> None:
        player = await self._active_player(interaction)
        if not player:
            return
        await player.destroy()
        await self._reply(interaction, "노래를 종료할게용가리 🐲")

    # ── 자동 퇴장 ────────────────────────────────────────────

    def _listener_count(self, channel: discord.VoiceChannel) -> int:
        """봇을 제외한 접속자 수. channel.members는 멤버 캐시에 의존해 누락될 수 있어 voice_states를 쓴다."""
        my_id = self.bot.user.id if self.bot.user else 0
        return sum(1 for user_id in channel.voice_states if user_id != my_id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        """음성채널에 봇만 혼자 남으면 스스로 나갑니다. (자원 낭비 방지)"""
        if member.bot:
            return
        player = self.players.get(member.guild.id)
        if not player or not player.voice.is_connected():
            return

        if self._listener_count(player.voice.channel) > 0:
            return

        # 재입장 유예
        await asyncio.sleep(30)
        if not player.voice.is_connected():
            return
        if self._listener_count(player.voice.channel) > 0:
            return

        log.info("자동 퇴장: %s 에 남은 사람이 없습니다", player.voice.channel.name)
        await player._notify("아무도 없어서 음성 채널에서 나갈게용가리 🐲")
        await player.destroy()


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 불러올 때 자동으로 호출하는 함수입니다."""
    await bot.add_cog(Music(bot))
