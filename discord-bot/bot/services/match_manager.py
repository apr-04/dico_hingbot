import json
import random
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bot.services.riot import SummonerInfo


# ============================================================
# 설정
# ============================================================

DATA_DIR = Path("bot/data")
DATA_FILE = DATA_DIR / "matches.json"


# ============================================================
# Match Manager
# ============================================================

class MatchManager:

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not DATA_FILE.exists():
            DATA_FILE.write_text("{}", encoding="utf-8")

    # ========================================================
    # 내부 저장소 입출력
    # ========================================================

    def _load(self) -> Dict[str, List[dict]]:
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save(self, data: Dict[str, List[dict]]) -> None:
        DATA_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=4),
            encoding="utf-8"
        )

    # ========================================================
    # 참가자 관리
    # ========================================================

    def get_participants(self, guild_id: int) -> List[dict]:
        """서버의 현재 참가자 목록을 반환합니다."""
        data = self._load()
        return data.get(str(guild_id), [])

    def add_participant(self, guild_id: int, info: SummonerInfo) -> List[dict]:
        """
        소환사를 참가자 목록에 추가합니다.
        이미 등록된 소환사라면 정보를 최신으로 갱신합니다.
        """
        data = self._load()
        gid = str(guild_id)
        if gid not in data:
            data[gid] = []

        participants = data[gid]

        # 정규화된 키로 중복 검사
        def normalize_key(name: str, tag: str) -> str:
            return f"{name.replace(' ', '').lower()}#{tag.replace(' ', '').lower()}"

        target_key = normalize_key(info.game_name, info.tag_line)

        participant_dict = {
            "game_name": info.game_name,
            "tag_line": info.tag_line,
            "full_name": info.full_name,
            "tier": info.tier,
            "rank": info.rank,
            "lp": info.lp,
            "tier_short": info.tier_short,
            "mmr_score": info.mmr_score,
        }

        existing_index = None
        for i, p in enumerate(participants):
            if normalize_key(p.get("game_name", ""), p.get("tag_line", "")) == target_key:
                existing_index = i
                break

        if existing_index is not None:
            participants[existing_index] = participant_dict
        else:
            participants.append(participant_dict)

        data[gid] = participants
        self._save(data)
        return participants

    def remove_participant(self, guild_id: int, summoner_input: str) -> Optional[dict]:
        """
        소환사를 참가자 목록에서 제외합니다.
        닉네임 #태그 또는 닉네임 형식 모두 유연하게 매칭합니다.
        """
        data = self._load()
        gid = str(guild_id)
        participants = data.get(gid, [])

        clean_input = summoner_input.replace(" ", "").lower()
        if "#" in clean_input:
            input_name, input_tag = clean_input.split("#", 1)
        else:
            input_name, input_tag = clean_input, None

        removed_item = None
        new_participants = []

        for p in participants:
            p_name = p.get("game_name", "").replace(" ", "").lower()
            p_tag = p.get("tag_line", "").replace(" ", "").lower()

            match = False
            if input_tag is not None:
                if p_name == input_name and p_tag == input_tag:
                    match = True
            else:
                if p_name == input_name:
                    match = True

            if match and removed_item is None:
                removed_item = p
            else:
                new_participants.append(p)

        if removed_item is not None:
            data[gid] = new_participants
            self._save(data)

        return removed_item

    def clear_participants(self, guild_id: int) -> None:
        """참가자 목록을 초기화합니다."""
        data = self._load()
        data[str(guild_id)] = []
        self._save(data)

    # ========================================================
    # 포맷팅 헬퍼
    # ========================================================

    @staticmethod
    def format_participant_list(participants: List[dict]) -> str:
        """
        사용자 요구 양식에 맞는 참가자 목록 텍스트를 반환합니다.
        예:
        참가자
        E3 열무비빔면 #따뜻한
        M 창원정통 #KR1
        D4 홍삼점수도둑 #KR1
        총 참가자 3명
        """
        if not participants:
            return "참가자가 없습니다."

        lines = ["참가자"]
        for p in participants:
            tier_str = p.get("tier_short", "U")
            full_name = p.get("full_name") or f"{p.get('game_name')} #{p.get('tag_line')}"
            lines.append(f"{tier_str} {full_name}")

        lines.append(f"총 참가자 {len(participants)}명")
        return "\n".join(lines)

    # ========================================================
    # 팀 구성 알고리즘
    # ========================================================

    def split_random(self, guild_id: int) -> Optional[Tuple[List[dict], List[dict]]]:
        """
        참가자들을 무작위(Random)로 2팀으로 분배합니다.
        참가자가 2명 미만이면 None을 반환합니다.
        """
        participants = list(self.get_participants(guild_id))
        if len(participants) < 2:
            return None

        random.shuffle(participants)
        half = len(participants) // 2
        team1 = participants[:half]
        team2 = participants[half:]
        return team1, team2

    def split_balanced(
        self,
        guild_id: int
    ) -> Optional[Tuple[List[dict], List[dict], int]]:
        """
        참가자들의 MMR 점수를 바탕으로 두 팀의 점수 차이가 최소가 되도록 분배합니다.
        반환: (team1, team2, 점수차이)
        참가자가 2명 미만이면 None을 반환합니다.
        """
        participants = list(self.get_participants(guild_id))
        n = len(participants)
        if n < 2:
            return None

        k = n // 2
        total_score = sum(p.get("mmr_score", 1200) for p in participants)

        best_diff = float("inf")
        best_combinations: List[Tuple[Tuple[int, ...], int]] = []

        indices = list(range(n))
        for team1_indices in combinations(indices, k):
            team1_score = sum(participants[i].get("mmr_score", 1200) for i in team1_indices)
            team2_score = total_score - team1_score
            diff = abs(team1_score - team2_score)

            if diff < best_diff:
                best_diff = diff
                best_combinations = [(team1_indices, diff)]
            elif diff == best_diff:
                best_combinations.append((team1_indices, diff))

        # 차이가 가장 적은 조합 중 하나를 무작위 선택
        chosen_indices, diff = random.choice(best_combinations)
        team1_set = set(chosen_indices)

        team1 = [participants[i] for i in indices if i in team1_set]
        team2 = [participants[i] for i in indices if i not in team1_set]

        return team1, team2, int(diff)
