import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.match_manager import MatchManager
from bot.services.riot import (
    RiotService,
    SummonerNotFoundException,
    parse_summoner_input,
    parse_multiple_summoners,
    format_autopsy_text,
)


# ============================================================
# Tier Helpers
# ============================================================

TIER_ICONS = {
    "C": "👑",
    "GM": "💎",
    "M": "🔮",
    "D": "🔷",
    "E": "🟢",
    "P": "💠",
    "G": "🟡",
    "S": "⚪",
    "B": "🟤",
    "I": "🔘",
    "U": "❓",
}


def get_tier_icon(tier_short: str) -> str:
    if not tier_short:
        return "❓"
    tier_upper = tier_short.upper()
    if tier_upper.startswith("GM"):
        return "💎"
    return TIER_ICONS.get(tier_upper[0], "🎖️")


def make_progress_bar(current: int, total: int = 10) -> str:
    filled = min(max(current, 0), total)
    empty = max(0, total - filled)
    return "🟩" * filled + "⬜" * empty


# ============================================================
# Custom Match Cog
# ============================================================

class CustomMatch(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.match_manager = MatchManager()
        self.riot_service = RiotService()

    # ========================================================
    # Embed Helper
    # ========================================================

    def _build_participant_embed(
        self,
        participants: list,
        title: str = "📋 내전 참가자 명단",
        color: discord.Color = discord.Color.blue()
    ) -> discord.Embed:
        count = len(participants)
        embed = discord.Embed(
            title=title,
            color=color
        )

        progress = make_progress_bar(count, 10)
        if count < 10:
            status = f"👥 **참가 인원:** `{count}/10명` (내전까지 **{10 - count}명** 남음!)\n{progress}"
        elif count == 10:
            status = f"👥 **참가 인원:** `{count}/10명` (🎉 **10명 모집 완료!**)\n{progress}\n`/랜덤` 또는 `/밸런스`로 팀을 구성하세요!"
        else:
            status = f"👥 **참가 인원:** `{count}명` (대기 인원: {count - 10}명)\n{progress}"

        embed.description = status

        if participants:
            lines = []
            for i, p in enumerate(participants, 1):
                tier_str = p.get("tier_short", "U")
                full_name = p.get("full_name") or f"{p.get('game_name')} #{p.get('tag_line')}"
                icon = get_tier_icon(tier_str)
                lines.append(f"`{i:2d}.` {icon} **`[{tier_str:^2}]`** {full_name}")

            embed.add_field(
                name="📋 참가자 목록",
                value="\n".join(lines),
                inline=False
            )
        else:
            embed.add_field(
                name="📋 참가자 목록",
                value="*현재 참가자가 없습니다.*",
                inline=False
            )

        embed.set_footer(
            text="💡 팀 구성: /랜덤 | /밸런스 | 초기화: /초기화"
        )
        return embed

    # ========================================================
    # /참가
    # ========================================================

    # ========================================================
    # /참가
    # ========================================================

    @app_commands.command(
        name="참가",
        description="내전 참가자 입력 (쉼표로 구분해 여러 명 입력 가능)"
    )
    @app_commands.describe(
        소환사="닉네임 #태그를 입력해주세요. (예: 햅삐우레삐#KR4, 창원정통, 파란게좋겠군)"
    )
    async def join(
        self,
        interaction: discord.Interaction,
        소환사: str
    ):
        parsed_list = parse_multiple_summoners(소환사)
        if not parsed_list:
            await interaction.response.send_message(
                "소환사 이름을 입력해주세요. (예: 햅삐우레삐#KR4, 창원정통, 파란게좋겠군, 당신너무한심해#530)",
                ephemeral=True
            )
            return

        # 전적 조회는 외부 통신이 수반되므로 응답 지연 처리
        await interaction.response.defer()

        added_infos = []
        failed_names = []

        async def fetch_one(name: str, tag: str):
            try:
                info = await self.riot_service.get_summoner(name, tag)
                return True, info
            except Exception:
                return False, f"{name}#{tag}"

        tasks = [fetch_one(name, tag) for name, tag in parsed_list]
        results = await asyncio.gather(*tasks)

        for success, res in results:
            if success:
                added_infos.append(res)
                self.match_manager.add_participant(interaction.guild_id, res)
            else:
                failed_names.append(res)

        if not added_infos:
            await interaction.followup.send(
                f"입력하신 소환사 정보를 찾을 수 없습니다: `{', '.join(failed_names)}`\n"
                "닉네임과 태그를 다시 확인해주세요.",
                ephemeral=True
            )
            return

        participants = self.match_manager.get_participants(interaction.guild_id)

        embed = self._build_participant_embed(
            participants,
            title="📋 내전 참가자 명단 갱신",
            color=discord.Color.green()
        )

        content = "내전 인원을 추가할게용가리 🐲"
        if failed_names:
            content += f"\n⚠️ *조회 실패한 소환사:* `{', '.join(failed_names)}`"

        await interaction.followup.send(
            content=content,
            embed=embed
        )

    # ========================================================
    # /제외
    # ========================================================

    @app_commands.command(
        name="제외",
        description="내전 참가자 제외 (쉼표로 구분해 여러 명 제외 가능)"
    )
    @app_commands.describe(
        소환사="제외할 닉네임 #태그를 입력해주세요. (예: 창원정통, 파란게좋겠군)"
    )
    async def leave(
        self,
        interaction: discord.Interaction,
        소환사: str
    ):
        raw_items = re.split(r'[,/\n]+', 소환사)
        targets = [item.strip() for item in raw_items if item.strip()]

        if not targets:
            await interaction.response.send_message(
                "제외할 소환사 이름을 입력해주세요.",
                ephemeral=True
            )
            return

        removed_list = []
        not_found = []

        for target in targets:
            removed = self.match_manager.remove_participant(
                interaction.guild_id,
                target
            )
            if removed:
                removed_list.append(removed)
            else:
                not_found.append(target)

        if not removed_list:
            await interaction.response.send_message(
                f"입력하신 소환사(`{', '.join(not_found)}`)는 참가자 명단에 없습니다.",
                ephemeral=True
            )
            return

        participants = self.match_manager.get_participants(interaction.guild_id)

        embed = self._build_participant_embed(
            participants,
            title="📋 내전 참가자 명단 갱신",
            color=discord.Color.orange()
        )

        removed_str_list = [
            f"**[{r.get('tier_short', 'U')}] {r.get('full_name')}**"
            for r in removed_list
        ]
        content = f"{', '.join(removed_str_list)} 님을 내전 인원에서 제외할게용가리 🐲"
        if not_found:
            content += f"\n⚠️ *명단에 없는 소환사:* `{', '.join(not_found)}`"

        await interaction.response.send_message(
            content=content,
            embed=embed
        )

    # ========================================================
    # /초기화
    # ========================================================

    @app_commands.command(
        name="초기화",
        description="내전 참가자 목록 초기화"
    )
    async def clear(
        self,
        interaction: discord.Interaction
    ):
        self.match_manager.clear_participants(interaction.guild_id)

        await interaction.response.send_message(
            "내전 참가자 목록을 초기화했습니다."
        )

    # ========================================================
    # /내전
    # ========================================================

    @app_commands.command(
        name="내전",
        description="내전 참가자 목록"
    )
    async def list_match(
        self,
        interaction: discord.Interaction
    ):
        participants = self.match_manager.get_participants(interaction.guild_id)

        if not participants:
            await interaction.response.send_message(
                "현재 등록된 내전 참가자가 없습니다. `/참가 [소환사]`로 등록해주세요."
            )
            return

        embed = self._build_participant_embed(
            participants,
            title="⚔️ 내전 참가자 명단",
            color=discord.Color.blue()
        )

        await interaction.response.send_message(embed=embed)

    # ========================================================
    # /랜덤
    # ========================================================

    @app_commands.command(
        name="랜덤",
        description="내전 팀 랜덤 구성"
    )
    async def random_teams(
        self,
        interaction: discord.Interaction
    ):
        result = self.match_manager.split_random(interaction.guild_id)

        if result is None:
            await interaction.response.send_message(
                "참가자가 부족합니다. 팀을 구성하려면 최소 2명 이상 필요합니다."
            )
            return

        team1, team2 = result

        embed = discord.Embed(
            title="🎲 내전 랜덤 팀 구성 결과",
            color=discord.Color.purple()
        )

        t1_lines = []
        for p in team1:
            tier_str = p.get("tier_short", "U")
            full_name = p.get("full_name") or f"{p.get('game_name')} #{p.get('tag_line')}"
            icon = get_tier_icon(tier_str)
            t1_lines.append(f"{icon} **`[{tier_str:^2}]`** {full_name}")

        t2_lines = []
        for p in team2:
            tier_str = p.get("tier_short", "U")
            full_name = p.get("full_name") or f"{p.get('game_name')} #{p.get('tag_line')}"
            icon = get_tier_icon(tier_str)
            t2_lines.append(f"{icon} **`[{tier_str:^2}]`** {full_name}")

        embed.add_field(
            name=f"🔵 1팀 ({len(team1)}명)",
            value="\n".join(t1_lines) if t1_lines else "없음",
            inline=True
        )
        embed.add_field(
            name=f"🔴 2팀 ({len(team2)}명)",
            value="\n".join(t2_lines) if t2_lines else "없음",
            inline=True
        )

        await interaction.response.send_message(embed=embed)

    # ========================================================
    # /밸런스
    # ========================================================

    @app_commands.command(
        name="밸런스",
        description="내전 팀 밸런스 구성"
    )
    async def balance_teams(
        self,
        interaction: discord.Interaction
    ):
        result = self.match_manager.split_balanced(interaction.guild_id)

        if result is None:
            await interaction.response.send_message(
                "참가자가 부족합니다. 밸런스 팀을 구성하려면 최소 2명 이상 필요합니다."
            )
            return

        team1, team2, diff = result

        team1_avg = int(sum(p.get("mmr_score", 1200) for p in team1) / len(team1))
        team2_avg = int(sum(p.get("mmr_score", 1200) for p in team2) / len(team2))

        embed = discord.Embed(
            title="⚖️ 내전 밸런스 팀 구성 결과",
            description=f"📊 **두 팀 레이팅 점수 차이:** `{diff}점` (균형도 최적화)",
            color=discord.Color.gold()
        )

        t1_lines = []
        for p in team1:
            tier_str = p.get("tier_short", "U")
            full_name = p.get("full_name") or f"{p.get('game_name')} #{p.get('tag_line')}"
            icon = get_tier_icon(tier_str)
            t1_lines.append(f"{icon} **`[{tier_str:^2}]`** {full_name}")

        t2_lines = []
        for p in team2:
            tier_str = p.get("tier_short", "U")
            full_name = p.get("full_name") or f"{p.get('game_name')} #{p.get('tag_line')}"
            icon = get_tier_icon(tier_str)
            t2_lines.append(f"{icon} **`[{tier_str:^2}]`** {full_name}")

        embed.add_field(
            name=f"🔵 1팀 ({len(team1)}명 / 평균 레이팅: {team1_avg})",
            value="\n".join(t1_lines) if t1_lines else "없음",
            inline=True
        )
        embed.add_field(
            name=f"🔴 2팀 ({len(team2)}명 / 평균 레이팅: {team2_avg})",
            value="\n".join(t2_lines) if t2_lines else "없음",
            inline=True
        )

        await interaction.response.send_message(embed=embed)

    # ========================================================
    # /부검
    # ========================================================

    @app_commands.command(
        name="부검",
        description="소환사 전적 부검"
    )
    @app_commands.describe(
        소환사="전적을 부검할 닉네임 #태그를 입력해주세요. (태그 생략 시 #KR1)"
    )
    async def autopsy(
        self,
        interaction: discord.Interaction,
        소환사: str
    ):
        parsed = parse_summoner_input(소환사)
        if not parsed:
            await interaction.response.send_message(
                "소환사 이름을 입력해주세요. (예: 당신너무한심해 #530 또는 창원정통)",
                ephemeral=True
            )
            return

        game_name, tag_line = parsed

        # 전적 조회 및 매치 분석에 시간이 걸리므로 응답 지연 처리
        await interaction.response.defer()

        try:
            data = await self.riot_service.inspect_summoner(
                game_name,
                tag_line,
                count=30
            )
        except SummonerNotFoundException:
            await interaction.followup.send(
                f"소환사 정보를 찾을 수 없습니다: `{game_name}#{tag_line}`\n"
                "닉네임과 태그를 다시 확인해주세요.",
                ephemeral=True
            )
            return
        except Exception as e:
            await interaction.followup.send(
                f"전적 부검 중 오류가 발생했습니다: {e}",
                ephemeral=True
            )
            return

        result_text = format_autopsy_text(data, limit=10)
        await interaction.followup.send(result_text)

    # ========================================================
    # /정보
    # ========================================================

    @app_commands.command(
        name="정보",
        description="소환사 상세 프로필 정보 조회"
    )
    @app_commands.describe(
        소환사="조회할 닉네임 #태그를 입력해주세요. (태그 생략 시 #KR1)"
    )
    async def user_info(
        self,
        interaction: discord.Interaction,
        소환사: str
    ):
        parsed = parse_summoner_input(소환사)
        if not parsed:
            await interaction.response.send_message(
                "소환사 이름을 입력해주세요. (예: 창원정통 또는 당신너무한심해 #530)",
                ephemeral=True
            )
            return

        game_name, tag_line = parsed

        # 전적 조회는 외부 통신이 수반되므로 응답 지연 처리
        await interaction.response.defer()

        try:
            profile = await self.riot_service.get_user_profile(game_name, tag_line)
        except SummonerNotFoundException:
            await interaction.followup.send(
                f"소환사 정보를 찾을 수 없습니다: `{game_name}#{tag_line}`\n"
                "닉네임과 태그를 다시 확인해주세요.",
                ephemeral=True
            )
            return
        except Exception as e:
            await interaction.followup.send(
                f"소환사 정보 조회 중 오류가 발생했습니다: {e}",
                ephemeral=True
            )
            return

        # Embed 구성
        embed = discord.Embed(
            title=f"👤 [{profile['full_name']}] 소환사 정보",
            url=profile["opgg_url"],
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=profile["icon_url"])
        embed.description = f"⭐ **소환사 레벨:** `Lv. {profile['summoner_level']}`"

        # 솔랭 필드
        solo = profile.get("solo_entry")
        if solo:
            tier = solo["tier"]
            rank = solo.get("rank", "")
            lp = solo.get("leaguePoints", 0)
            wins = solo.get("wins", 0)
            losses = solo.get("losses", 0)
            total = wins + losses
            wr = (wins / total * 100) if total > 0 else 0
            tier_icon = get_tier_icon(tier)
            solo_val = f"{tier_icon} **{tier} {rank}** ({lp} LP)\n{wins}승 {losses}패 (승률 **{wr:.1f}%**)"
        else:
            solo_val = "❓ **UNRANKED**\n배치 미완료"

        # 자랭 필드
        flex = profile.get("flex_entry")
        if flex:
            tier = flex["tier"]
            rank = flex.get("rank", "")
            lp = flex.get("leaguePoints", 0)
            wins = flex.get("wins", 0)
            losses = flex.get("losses", 0)
            total = wins + losses
            wr = (wins / total * 100) if total > 0 else 0
            tier_icon = get_tier_icon(tier)
            flex_val = f"{tier_icon} **{tier} {rank}** ({lp} LP)\n{wins}승 {losses}패 (승률 **{wr:.1f}%**)"
        else:
            flex_val = "❓ **UNRANKED**\n배치 미완료"

        embed.add_field(name="🏆 솔로 랭크", value=solo_val, inline=True)
        embed.add_field(name="🏆 자유 랭크", value=flex_val, inline=True)

        # 모스트 챔피언 Top 3
        masteries = profile.get("top_masteries", [])
        if masteries:
            m_lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, m in enumerate(masteries):
                medal = medals[i] if i < len(medals) else f"`{i+1}.`"
                m_lines.append(
                    f"{medal} **{m['champion_name']}** - Lv.{m['level']} (`{m['points']:,}점`)"
                )
            embed.add_field(
                name="🎖️ 모스트 챔피언 숙련도 (Top 3)",
                value="\n".join(m_lines),
                inline=False
            )

        embed.add_field(
            name="🔗 전적 바로가기",
            value=f"[OP.GG에서 더 자세한 전적 보기]({profile['opgg_url']})",
            inline=False
        )

        embed.set_footer(text="Google Antigravity LoL Service")

        await interaction.followup.send(embed=embed)


# ============================================================
# Cog Setup
# ============================================================

async def setup(bot):
    await bot.add_cog(
        CustomMatch(bot)
    )
