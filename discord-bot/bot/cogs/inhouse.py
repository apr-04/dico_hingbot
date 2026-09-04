import os
import json
import logging
import itertools
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("discord_bot.inhouse")

# 데이터 파일 경로 설정
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
SCORES_FILE = os.path.join(DATA_DIR, "scores.json")
DEFAULT_SCORE = 1000  # 등록되지 않은 유저의 기본 점수


def load_all_scores() -> dict:
    """전체 점수 데이터 로드"""
    if not os.path.exists(SCORES_FILE):
        return {}
    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load scores: {e}")
        return {}


def save_all_scores(data: dict):
    """전체 점수 데이터 저장"""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(SCORES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save scores: {e}")


def get_user_score(guild_id: int, user: discord.Member) -> Tuple[int, bool]:
    """
    유저의 점수를 조회합니다.
    Returns: (score, is_registered)
    """
    all_scores = load_all_scores()
    guild_scores = all_scores.get(str(guild_id), {})

    # 1. 유저 ID로 조회
    if str(user.id) in guild_scores:
        return guild_scores[str(user.id)]["score"], True

    # 2. 닉네임/디스플레이 이름으로 조회 (호환성)
    for entry in guild_scores.values():
        if entry.get("name") in (user.name, user.display_name):
            return entry["score"], True

    return DEFAULT_SCORE, False


def set_user_score(guild_id: int, key: str, name: str, score: int):
    """유저 점수 등록 및 저장"""
    all_scores = load_all_scores()
    g_key = str(guild_id)
    if g_key not in all_scores:
        all_scores[g_key] = {}

    all_scores[g_key][key] = {
        "name": name,
        "score": score
    }
    save_all_scores(all_scores)


class InhouseSession:
    """서버별 진행 중인 내전 세션 관리"""
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.participants: List[discord.Member] = []
        self.target_count: int = 10
        self.message: Optional[discord.Message] = None
        self.is_active: bool = False

    def add_participant(self, member: discord.Member) -> bool:
        if member.id in [m.id for m in self.participants]:
            return False
        if len(self.participants) >= self.target_count:
            return False
        self.participants.append(member)
        return True

    def remove_participant(self, member: discord.Member) -> bool:
        for i, m in enumerate(self.participants):
            if m.id == member.id:
                self.participants.pop(i)
                return True
        return False

    def build_embed(self) -> discord.Embed:
        """현재 모집 상태를 나타내는 Embed 생성"""
        count = len(self.participants)
        embed = discord.Embed(
            title="⚔️ 10인 내전 참가자 모집 중!",
            description=(
                f"**현재 참가 인원: `{count} / {self.target_count}`명**\n"
                "아래 버튼을 누르거나 `/내전참가` 명령어를 입력하여 신청할 수 있습니다."
            ),
            color=discord.Color.green() if count == self.target_count else discord.Color.gold()
        )

        if self.participants:
            lines = []
            for i, p in enumerate(self.participants, 1):
                score, is_reg = get_user_score(self.guild_id, p)
                tag = f"({score}점)" if is_reg else "(미등록 1000점)"
                lines.append(f"**{i}.** {p.mention} `{p.display_name}` {tag}")
            embed.add_field(name="📋 참가자 목록", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📋 참가자 목록", value="아직 참가자가 없습니다.", inline=False)

        if count == self.target_count:
            embed.set_footer(text="🎉 10명이 모두 모였습니다! [밸런스 맞추기] 버튼이나 /밸런스 를 실행하세요.")
        else:
            embed.set_footer(text=f"내전 시작까지 앞으로 {self.target_count - count}명 더 필요합니다.")

        return embed


class InhouseView(discord.ui.View):
    """내전 모집 메시지에 부착되는 인터랙티브 버튼 뷰"""
    def __init__(self, cog: 'InhouseCog', guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="참가", style=discord.ButtonStyle.success, emoji="🟢", custom_id="inhouse_join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.cog.get_session(self.guild_id)
        if not session.is_active:
            await interaction.response.send_message("⚠️ 현재 진행 중인 내전 모집이 없습니다. `/내전생성`으로 새로 열어주세요.", ephemeral=True)
            return

        success = session.add_participant(interaction.user)
        if not success:
            if interaction.user.id in [m.id for m in session.participants]:
                await interaction.response.send_message("⚠️ 이미 내전에 참가하셨습니다!", ephemeral=True)
            else:
                await interaction.response.send_message("⚠️ 이미 10명의 인원이 가득 찼습니다!", ephemeral=True)
            return

        embed = session.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

        if len(session.participants) == session.target_count:
            await interaction.followup.send("🎉 **10명의 인원이 모두 모집되었습니다!** `/밸런스` 명령어로 팀을 나눠보세요.")

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger, emoji="🔴", custom_id="inhouse_leave")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.cog.get_session(self.guild_id)
        if not session.is_active:
            await interaction.response.send_message("⚠️ 현재 진행 중인 내전 모집이 없습니다.", ephemeral=True)
            return

        success = session.remove_participant(interaction.user)
        if not success:
            await interaction.response.send_message("⚠️ 참가 명단에 없습니다.", ephemeral=True)
            return

        embed = session.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="밸런스 맞추기", style=discord.ButtonStyle.primary, emoji="⚖️", custom_id="inhouse_balance")
    async def balance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.execute_balance(interaction)


class InhouseCog(commands.Cog, name="내전"):
    """10인 내전 모집, 점수 관리 및 5:5 밸런스 팀 매칭 모듈"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: Dict[int, InhouseSession] = {}

    def get_session(self, guild_id: int) -> InhouseSession:
        if guild_id not in self.sessions:
            self.sessions[guild_id] = InhouseSession(guild_id)
        return self.sessions[guild_id]

    # -------------------------------------------------------------
    # 1. /내전생성 : 10명 인원 모집
    # -------------------------------------------------------------
    @app_commands.command(name="내전생성", description="10인 내전 참가자 모집을 시작합니다.")
    async def create_inhouse(self, interaction: discord.Interaction):
        session = self.get_session(interaction.guild_id)
        session.participants.clear()
        session.is_active = True

        embed = session.build_embed()
        view = InhouseView(self, interaction.guild_id)

        await interaction.response.send_message(embed=embed, view=view)
        session.message = await interaction.original_response()

    # -------------------------------------------------------------
    # 2. /내전참가 : 내전 참가 신청
    # -------------------------------------------------------------
    @app_commands.command(name="내전참가", description="현재 모집 중인 내전에 참가 신청합니다.")
    async def join_inhouse(self, interaction: discord.Interaction):
        session = self.get_session(interaction.guild_id)
        if not session.is_active:
            await interaction.response.send_message("⚠️ 현재 모집 중인 내전이 없습니다. 먼저 `/내전생성`으로 모집을 시작해 주세요.", ephemeral=True)
            return

        success = session.add_participant(interaction.user)
        if not success:
            if interaction.user.id in [m.id for m in session.participants]:
                await interaction.response.send_message("⚠️ 이미 참가 명단에 등록되어 있습니다.", ephemeral=True)
            else:
                await interaction.response.send_message("⚠️ 인원이 이미 가득 찼습니다 (10/10).", ephemeral=True)
            return

        embed = session.build_embed()
        # 모집 메시지가 존재하면 실시간 업데이트
        if session.message:
            try:
                await session.message.edit(embed=embed)
            except Exception:
                pass

        await interaction.response.send_message(
            f"✅ {interaction.user.mention}님이 내전에 참가했습니다! (현재 `{len(session.participants)}/10`명)"
        )

        if len(session.participants) == session.target_count:
            await interaction.channel.send("🎉 **10명이 모두 모였습니다!** `/밸런스` 명령어를 입력하여 팀을 나눠보세요.")

    # -------------------------------------------------------------
    # 3. /내전취소 : 내전 참가 취소
    # -------------------------------------------------------------
    @app_commands.command(name="내전취소", description="내전 참가 신청을 취소합니다.")
    async def leave_inhouse(self, interaction: discord.Interaction):
        session = self.get_session(interaction.guild_id)
        if not session.is_active:
            await interaction.response.send_message("⚠️ 현재 진행 중인 내전 모집이 없습니다.", ephemeral=True)
            return

        success = session.remove_participant(interaction.user)
        if not success:
            await interaction.response.send_message("⚠️ 참가 명단에 없습니다.", ephemeral=True)
            return

        embed = session.build_embed()
        if session.message:
            try:
                await session.message.edit(embed=embed)
            except Exception:
                pass

        await interaction.response.send_message(
            f"❎ {interaction.user.mention}님이 내전 참가를 취소했습니다. (현재 `{len(session.participants)}/10`명)"
        )

    # -------------------------------------------------------------
    # 4. /내전점수 [닉네임/유저] [점수] : 사용자의 점수 주기
    # -------------------------------------------------------------
    @app_commands.command(name="내전점수", description="유저의 내전 점수(MMR)를 등록하거나 수정합니다.")
    @app_commands.describe(
        점수="부여할 내전 점수 (예: 1500)",
        유저="점수를 부여할 디스코드 멤버 (선택)",
        닉네임="멤버 태그 대신 직접 입력할 닉네임 (선택)"
    )
    async def set_score(
        self,
        interaction: discord.Interaction,
        점수: int,
        유저: Optional[discord.Member] = None,
        닉네임: Optional[str] = None
    ):
        if not 유저 and not 닉네임:
            await interaction.response.send_message("⚠️ `유저`를 선택하거나 `닉네임`을 입력해 주세요!", ephemeral=True)
            return

        if 유저:
            key = str(유저.id)
            target_name = 유저.display_name
            mention_str = 유저.mention
        else:
            key = 닉네임.strip()
            target_name = 닉네임.strip()
            mention_str = f"**{target_name}**"

        set_user_score(interaction.guild_id, key, target_name, 점수)

        embed = discord.Embed(
            title="🎯 내전 점수 등록 완료",
            description=f"{mention_str}님의 점수가 **{점수}점**으로 설정되었습니다.",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------
    # 5. /내전목록 : 등록된 점수 목록 확인
    # -------------------------------------------------------------
    @app_commands.command(name="내전목록", description="서버에 등록된 유저들의 내전 점수 목록을 확인합니다.")
    async def list_scores(self, interaction: discord.Interaction):
        all_scores = load_all_scores()
        guild_scores = all_scores.get(str(interaction.guild_id), {})

        if not guild_scores:
            await interaction.response.send_message("📋 아직 등록된 내전 점수 데이터가 없습니다. `/내전점수`로 등록해보세요!", ephemeral=True)
            return

        # 점수 높은 순으로 정렬
        sorted_users = sorted(guild_scores.values(), key=lambda x: x["score"], reverse=True)

        lines = []
        for i, u in enumerate(sorted_users, 1):
            lines.append(f"**{i}.** {u['name']} — **{u['score']}점**")

        embed = discord.Embed(
            title="🏆 내전 점수 순위표",
            description="\n".join(lines[:25]),
            color=discord.Color.purple()
        )
        if len(sorted_users) > 25:
            embed.set_footer(text=f"...외 {len(sorted_users) - 25}명 더 있음")

        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------
    # 6. /밸런스 : 5:5 밸런스 맞추기
    # -------------------------------------------------------------
    @app_commands.command(name="밸런스", description="참가자들의 점수를 기반으로 점수 합 차이가 최소가 되도록 5:5 팀을 나눕니다.")
    async def balance_teams(self, interaction: discord.Interaction):
        await self.execute_balance(interaction)

    async def execute_balance(self, interaction: discord.Interaction):
        """실제 5:5 밸런스 계산 및 결과 전송"""
        session = self.get_session(interaction.guild_id)
        participants = session.participants

        if len(participants) < 2:
            await interaction.response.send_message("⚠️ 팀을 나누려면 최소 2명 이상의 참가자가 필요합니다.", ephemeral=True)
            return

        # 10명이 아닐 경우 경고 및 진행
        is_exact_10 = len(participants) == 10

        # 각 참가자의 점수 매핑
        players_data = []
        for p in participants:
            score, _ = get_user_score(interaction.guild_id, p)
            players_data.append({
                "member": p,
                "score": score
            })

        total_players = len(players_data)
        team_size = total_players // 2

        # C(total_players, team_size) 조합 탐색을 통해 점수 합 차이가 가장 적은 최적의 팀 찾기
        indices = list(range(total_players))
        best_diff = float('inf')
        best_team_a_indices = None

        total_score_sum = sum(p["score"] for p in players_data)

        for combo in itertools.combinations(indices, team_size):
            score_a = sum(players_data[i]["score"] for i in combo)
            score_b = total_score_sum - score_a
            diff = abs(score_a - score_b)

            if diff < best_diff:
                best_diff = diff
                best_team_a_indices = combo
                if diff == 0:
                    break

        team_a = [players_data[i] for i in best_team_a_indices]
        team_b = [players_data[i] for i in indices if i not in best_team_a_indices]

        sum_a = sum(p["score"] for p in team_a)
        sum_b = sum(p["score"] for p in team_b)
        avg_a = sum_a / len(team_a) if team_a else 0
        avg_b = sum_b / len(team_b) if team_b else 0

        embed = discord.Embed(
            title="⚖️ 5:5 내전 밸런스 팀 매칭 완료!",
            description=(
                f"**양 팀 점수 차이: `{best_diff}`점** "
                f"({'🔥 완벽한 황금 밸런스!' if best_diff == 0 else '치열한 접전 예상!'})\n"
                f"{'' if is_exact_10 else f'*(참고: 현재 인원이 {total_players}명으로 {len(team_a)}:{len(team_b)} 매칭되었습니다)*'}"
            ),
            color=discord.Color.teal()
        )

        # 1팀 목록 구성
        lines_a = [f"• {p['member'].mention} `{p['member'].display_name}` ({p['score']}점)" for p in team_a]
        lines_a.append(f"\n📊 **총점: {sum_a}점** (평균: {avg_a:.1f}점)")
        embed.add_field(name=f"🔵 1팀 ({len(team_a)}명)", value="\n".join(lines_a), inline=True)

        # 2팀 목록 구성
        lines_b = [f"• {p['member'].mention} `{p['member'].display_name}` ({p['score']}점)" for p in team_b]
        lines_b.append(f"\n📊 **총점: {sum_b}점** (평균: {avg_b:.1f}점)")
        embed.add_field(name=f"🔴 2팀 ({len(team_b)}명)", value="\n".join(lines_b), inline=True)

        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(InhouseCog(bot))
