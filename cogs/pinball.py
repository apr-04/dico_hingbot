"""
핀볼 추첨 + 랜덤 뽑기.

참가자 보유 개수만큼 구슬을 만들어 물리 시뮬레이션으로 낙하시키고,
골라인을 먼저 통과한 구슬의 주인이 당첨된다. 당첨자를 사전에 정하지 않으며,
구슬이 균등하므로 확률은 보유 개수 / 전체 개수가 된다. (core/marble_race.py)
"""

import asyncio
import json
import logging
import random
import re
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.marble_race import MAX_MARBLES, render_marble_race  # noqa: E402

log = logging.getLogger("pinball")

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "pinball.json"
EMBED_COLOR = 0xE8506E
MAX_GRANT = 1000
MAX_SHOW = 40

# `@A 10 @B 20` 순차 파싱용. 멘션을 먼저 매칭해야 멘션 내부 ID가 숫자로 잡히지 않는다.
GRANT_TOKEN_RE = re.compile(r"<@!?(\d+)>|<@&(\d+)>|(-?\d+)")


class PinballStore:
    """길드별 보유 현황을 JSON으로 영속화."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, int]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.data = {}
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("핀볼 데이터 로드 실패, 초기화: %s", e)
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_all(self, guild_id: int) -> dict[int, int]:
        return {int(k): v for k, v in self.data.get(str(guild_id), {}).items()}

    def grant(self, guild_id: int, user_id: int, amount: int) -> int:
        """증감 후 잔량 반환. 음수로 내려가지 않는다."""
        guild = self.data.setdefault(str(guild_id), {})
        new_value = max(0, guild.get(str(user_id), 0) + amount)
        guild[str(user_id)] = new_value
        self.save()
        return new_value

    def clear(self, guild_id: int) -> int:
        removed = len(self.data.get(str(guild_id), {}))
        self.data[str(guild_id)] = {}
        self.save()
        return removed


class Pinball(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = PinballStore(DATA_FILE)

    핀볼 = app_commands.Group(name="핀볼", description="핀볼 구슬 레이스")

    async def _fetch_member(
        self, guild: discord.Guild, user_id: int
    ) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return None
        return None if member.bot else member

    def _entries(self, guild_id: int) -> dict[int, int]:
        return {u: c for u, c in self.store.get_all(guild_id).items() if c > 0}

    def _list_embed(self, interaction: discord.Interaction, title: str = "") -> discord.Embed:
        entries = self._entries(interaction.guild_id)

        embed = discord.Embed(title="현재 핀볼 리스트", color=EMBED_COLOR)
        embed.description = f"**{title}**\n" if title else ""

        if not entries:
            embed.description += "\n아직 핀볼을 가진 사람이 없습니다.\n`/핀볼 부여` 로 나눠주세요."
            return embed

        ranked = sorted(entries.items(), key=lambda kv: kv[1], reverse=True)
        total = sum(entries.values())

        lines = [
            f"<@{user_id}> **{count}개** · 당첨 확률 {count / total * 100:.0f}%"
            for user_id, count in ranked[:MAX_SHOW]
        ]
        if len(ranked) > MAX_SHOW:
            lines.append(f"...외 {len(ranked) - MAX_SHOW}명")

        embed.description += "\n" + "\n".join(lines)
        embed.set_footer(text=f"참여 {len(ranked)}명 · 구슬 총 {total}개")
        return embed

    async def _parse_grants(
        self, interaction: discord.Interaction, text: str, default: int | None
    ) -> tuple[dict[int, int], list[str]]:
        """
        `@A 10 @B 20` -> {A: 10, B: 20}

        좌에서 우로 한 번 훑는다. 멘션은 대기열에 쌓고, 숫자를 만나면 대기 중인
        전원에게 배정한다. 숫자를 받지 못한 인원은 default를 받는다.
        따라서 `@A @B 10`(둘 다 10), `@A @B`(둘 다 default)도 그대로 동작한다.
        """
        guild = interaction.guild
        grants: dict[int, int] = {}
        pending: list[int] = []
        problems: list[str] = []

        def assign(amount: int) -> None:
            for uid in pending:
                grants[uid] = amount
            pending.clear()

        for match in GRANT_TOKEN_RE.finditer(text):
            user_id, role_id, number = match.group(1), match.group(2), match.group(3)
            if user_id:
                pending.append(int(user_id))
            elif role_id:
                role = guild.get_role(int(role_id))
                if role and role.members:
                    pending.extend(m.id for m in role.members if not m.bot)
                else:
                    problems.append(
                        "역할 멘션의 인원을 읽지 못했습니다 (SERVER MEMBERS INTENT 필요)"
                    )
            elif number is not None:
                assign(int(number))

        if pending:
            if default is None:
                problems.append(
                    f"{len(pending)}명은 개수가 없습니다. "
                    "멘션 뒤에 숫자를 적거나 `개수` 옵션을 채워주세요."
                )
                pending.clear()
            else:
                assign(default)

        return grants, problems

    @핀볼.command(name="부여", description="핀볼을 나눠줍니다. 사람마다 다른 개수도 가능.")
    @app_commands.describe(
        대상="예) @홍길동 10 @김철수 20 · 비워두면 내 음성 채널 전원",
        개수="숫자를 따로 안 적은 사람에게 줄 기본값 (음수면 회수)",
    )
    async def grant(
        self,
        interaction: discord.Interaction,
        대상: str | None = None,
        개수: app_commands.Range[int, -MAX_GRANT, MAX_GRANT] | None = None,
    ) -> None:
        await interaction.response.defer()
        guild = interaction.guild

        if 대상 and 대상.strip():
            grants, problems = await self._parse_grants(interaction, 대상, 개수)
            source = "멘션한 인원"
        else:
            if 개수 is None:
                return await interaction.followup.send(
                    "줄 개수를 정해주세요.\n"
                    "· 개별 지정: `/핀볼 부여 대상:@홍길동 10 @김철수 20`\n"
                    "· 전원 동일: `/핀볼 부여 개수:10`",
                    ephemeral=True,
                )
            voice_state = getattr(interaction.user, "voice", None)
            if not voice_state or not voice_state.channel:
                return await interaction.followup.send(
                    "음성 채널에 들어간 뒤 실행하거나, `대상`에 멘션을 적어주세요.",
                    ephemeral=True,
                )
            grants = {m.id: 개수 for m in voice_state.channel.members if not m.bot}
            problems = []
            source = f"{voice_state.channel.name} 채널"

        if not grants:
            msg = "대상을 찾지 못했습니다."
            if problems:
                msg += "\n" + "\n".join(f"· {p}" for p in problems)
            return await interaction.followup.send(msg, ephemeral=True)

        detail: list[str] = []
        for user_id, amount in grants.items():
            if amount == 0:
                continue
            total = self.store.grant(interaction.guild_id, user_id, amount)
            member = await self._fetch_member(guild, user_id)
            name = member.display_name if member else f"({user_id})"
            detail.append(f"{name} {amount:+}개 → {total}개")

        if not detail:
            return await interaction.followup.send("0개는 의미가 없습니다.", ephemeral=True)

        amounts = {a for a in grants.values() if a != 0}
        if len(detail) == 1:
            headline = f"핀볼 부여: {detail[0]}"
        elif len(amounts) == 1:
            headline = f"핀볼 부여: {source} {len(detail)}명에게 각 {amounts.pop():+}개"
        else:
            headline = f"핀볼 부여: {len(detail)}명 (개별 지정)"

        embed = self._list_embed(interaction, title=headline)
        if len(amounts) > 1:
            embed.add_field(name="이번에 준 내역", value="\n".join(detail)[:1024], inline=False)
        if problems:
            embed.add_field(name="확인 필요", value="\n".join(problems)[:1024], inline=False)
        await interaction.followup.send(embed=embed)

    @핀볼.command(name="목록", description="현재 핀볼 보유 현황과 당첨 확률을 봅니다.")
    async def show_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self._list_embed(interaction))

    @핀볼.command(name="시작", description="구슬 레이스를 굴려 당첨자를 뽑습니다.")
    async def start(self, interaction: discord.Interaction) -> None:
        entries = self._entries(interaction.guild_id)

        if len(entries) < 2:
            return await interaction.response.send_message(
                "핀볼을 가진 사람이 2명 이상이어야 합니다.", ephemeral=True
            )

        total = sum(entries.values())
        if total > MAX_MARBLES:
            return await interaction.response.send_message(
                f"구슬이 {total}개라 너무 많습니다. (최대 {MAX_MARBLES}개) "
                "비율만 같으면 확률은 동일하므로 개수를 줄여주세요.",
                ephemeral=True,
            )

        await interaction.response.defer()

        user_ids = list(entries.keys())
        named: list[tuple[str, int]] = []
        for user_id in user_ids:
            member = await self._fetch_member(interaction.guild, user_id)
            named.append((member.display_name if member else f"({user_id})", entries[user_id]))

        # 물리 계산과 인코딩이 블로킹이므로 별도 스레드로 넘긴다.
        try:
            buffer, winner_index = await asyncio.to_thread(render_marble_race, named)
        except Exception as e:
            log.exception("구슬 레이스 생성 실패")
            return await interaction.followup.send(f"영상을 만들지 못했습니다: `{e}`")

        winner_id = user_ids[winner_index]
        self.store.clear(interaction.guild_id)

        video = discord.File(buffer, filename="marble.mp4")
        embed = discord.Embed(title="🎲 핀볼 구슬 레이스", color=EMBED_COLOR)
        embed.description = (
            f"구슬 **{total}개**가 출발했습니다.\n"
            f"골라인을 가장 먼저 통과한 사람은... ||<@{winner_id}>||"
        )
        embed.set_footer(text="영상을 끝까지 보거나, 가려진 이름을 눌러 확인하세요")

        await interaction.followup.send(embed=embed, file=video)

    @핀볼.command(name="초기화", description="이 서버의 핀볼을 전부 지웁니다.")
    async def reset(self, interaction: discord.Interaction) -> None:
        removed = self.store.clear(interaction.guild_id)
        if not removed:
            return await interaction.response.send_message("지울 핀볼이 없습니다.", ephemeral=True)
        await interaction.response.send_message(f"핀볼을 초기화했습니다. ({removed}명)")

    @app_commands.command(name="골라줘", description="여러 항목 중 하나를 무작위로 뽑습니다.")
    @app_commands.describe(항목="쉼표로 구분. 비우면 음성 채널 인원 중에서 뽑습니다.")
    async def pick(self, interaction: discord.Interaction, 항목: str | None = None) -> None:
        if 항목:
            options = [s.strip() for s in 항목.split(",") if s.strip()]
            source = "입력한 항목"
        else:
            voice_state = getattr(interaction.user, "voice", None)
            if not voice_state or not voice_state.channel:
                return await interaction.response.send_message(
                    "항목을 쉼표로 입력하거나 음성 채널에 들어간 뒤 사용해주세요.\n"
                    "예) `/골라줘 짜장면, 짬뽕, 볶음밥`",
                    ephemeral=True,
                )
            options = [m.mention for m in voice_state.channel.members if not m.bot]
            source = f"{voice_state.channel.name} 채널"

        if len(options) < 2:
            return await interaction.response.send_message(
                "고를 항목이 2개 이상 필요합니다.", ephemeral=True
            )

        winner = random.choice(options)
        embed = discord.Embed(title="🎲 랜덤 뽑기", description=f"# {winner}", color=EMBED_COLOR)
        embed.set_footer(text=f"{source} {len(options)}개 중 선택")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Pinball(bot))
