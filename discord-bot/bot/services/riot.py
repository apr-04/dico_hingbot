import asyncio
import os
import re
import ssl
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv

load_dotenv()


# ============================================================
# Data Model
# ============================================================

@dataclass
class SummonerInfo:
    game_name: str
    tag_line: str
    full_name: str
    tier: str
    rank: Optional[str]
    lp: int
    tier_short: str
    mmr_score: int


class SummonerNotFoundException(Exception):
    """소환사를 찾을 수 없을 때 발생하는 예외"""
    pass


# ============================================================
# Tier & MMR Helper
# ============================================================

ROMAN_TO_NUM = {
    "I": "1",
    "II": "2",
    "III": "3",
    "IV": "4",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
}

TIER_BASE_SCORE = {
    "IRON": 100,
    "BRONZE": 500,
    "SILVER": 900,
    "GOLD": 1300,
    "PLATINUM": 1700,
    "EMERALD": 2100,
    "DIAMOND": 2500,
    "MASTER": 2900,
    "GRANDMASTER": 3700,
    "CHALLENGER": 4500,
}

DIVISION_OFFSET = {
    "4": 0,
    "3": 100,
    "2": 200,
    "1": 300,
}


def parse_summoner_input(input_text: str) -> Optional[Tuple[str, str]]:
    """
    사용자가 입력한 '소환사' 문자열을 (game_name, tag_line)으로 분리합니다.
    태그(#)가 생략되면 기본값으로 'KR1'을 사용합니다.
    예: '열무비빔면 #따뜻한' -> ('열무비빔면', '따뜻한')
        '창원정통'          -> ('창원정통', 'KR1')
    """
    cleaned = input_text.strip()
    if not cleaned:
        return None

    if "#" not in cleaned:
        return cleaned, "KR1"

    parts = cleaned.split("#", 1)
    game_name = parts[0].strip()
    tag_line = parts[1].strip()

    if not game_name:
        return None

    if not tag_line:
        tag_line = "KR1"

    return game_name, tag_line


def parse_multiple_summoners(input_text: str) -> List[Tuple[str, str]]:
    """
    쉼표(,) 또는 줄바꿈으로 구분된 여러 소환사 문자열을 파싱합니다.
    예: '햅삐우레삐#KR4, 창원정통, 파란게좋겠군, 당신너무한심해#530'
    """
    items = re.split(r'[,/\n]+', input_text)
    summoners = []
    for item in items:
        cleaned = item.strip()
        if not cleaned:
            continue
        parsed = parse_summoner_input(cleaned)
        if parsed:
            summoners.append(parsed)
    return summoners


def format_tier_short(tier: str, rank: Optional[str]) -> str:
    """
    티어 및 랭크를 약어로 변환합니다.
    예: ('EMERALD', 'III') -> 'E3'
        ('MASTER', 'I') -> 'M'
        ('DIAMOND', 'IV') -> 'D4'
        ('UNRANKED', None) -> 'U'
    """
    tier_upper = tier.upper()
    if tier_upper in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        mapping = {"MASTER": "M", "GRANDMASTER": "GM", "CHALLENGER": "C"}
        return mapping.get(tier_upper, tier_upper[0])

    if tier_upper == "UNRANKED":
        return "U"

    rank_num = ROMAN_TO_NUM.get(str(rank).upper(), "") if rank else ""
    return f"{tier_upper[0]}{rank_num}"


def calculate_mmr(tier: str, rank: Optional[str], lp: int = 0) -> int:
    """
    팀 밸런스 구성을 위한 MMR 점수를 산출합니다.
    """
    tier_upper = tier.upper()
    if tier_upper == "UNRANKED":
        return 1100  # 기본 실버/골드 사이 수준

    base = TIER_BASE_SCORE.get(tier_upper, 1200)

    if tier_upper in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return base + min(max(lp, 0), 1000)

    rank_num = ROMAN_TO_NUM.get(str(rank).upper(), "4")
    offset = DIVISION_OFFSET.get(rank_num, 0)
    lp_offset = int(min(max(lp, 0), 100) * 0.5)

    return base + offset + lp_offset


# ============================================================
# Riot Service
# ============================================================

class RiotService:

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("RIOT_API_KEY")

        # SSL Context for Windows environments
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    async def get_summoner(self, game_name: str, tag_line: str) -> SummonerInfo:
        """
        소환사 정보를 가져옵니다.
        1차: Riot 공식 API
        2차: OP.GG 웹페이지 메타태그 Fallback
        """
        # 1. Riot API 시도
        if self.api_key:
            try:
                result = await self._fetch_from_riot_api(game_name, tag_line)
                if result:
                    return result
            except Exception as e:
                print(f"[RiotService] Riot API error: {e}. Falling back to OP.GG...")

        # 2. OP.GG Fallback 시도
        try:
            result = await self._fetch_from_opgg(game_name, tag_line)
            if result:
                return result
        except Exception as e:
            print(f"[RiotService] OP.GG fallback error: {e}")

        raise SummonerNotFoundException(
            f"소환사 정보를 찾을 수 없습니다: {game_name}#{tag_line}"
        )

    # ========================================================
    # Riot Official API
    # ========================================================

    async def _fetch_from_riot_api(
        self,
        game_name: str,
        tag_line: str
    ) -> Optional[SummonerInfo]:
        headers = {"X-Riot-Token": self.api_key}
        encoded_name = urllib.parse.quote(game_name)
        encoded_tag = urllib.parse.quote(tag_line)

        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 1. PUUID 조회
            account_url = (
                f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
                f"{encoded_name}/{encoded_tag}"
            )
            async with session.get(account_url, headers=headers) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise RuntimeError(f"Account API failed with status {resp.status}")

                account_data = await resp.json()
                puuid = account_data["puuid"]
                actual_name = account_data.get("gameName", game_name)
                actual_tag = account_data.get("tagLine", tag_line)

            # 2. 리그 랭크 정보 조회
            league_url = f"https://kr.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
            async with session.get(league_url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"League API failed with status {resp.status}")

                entries = await resp.json()

            # 솔로랭크 항목 찾기
            solo_entry = next(
                (e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"),
                None
            )

            if solo_entry:
                tier = solo_entry["tier"]
                rank = solo_entry.get("rank")
                lp = solo_entry.get("leaguePoints", 0)
            else:
                tier = "UNRANKED"
                rank = None
                lp = 0

            tier_short = format_tier_short(tier, rank)
            mmr = calculate_mmr(tier, rank, lp)

            return SummonerInfo(
                game_name=actual_name,
                tag_line=actual_tag,
                full_name=f"{actual_name} #{actual_tag}",
                tier=tier,
                rank=rank,
                lp=lp,
                tier_short=tier_short,
                mmr_score=mmr,
            )

    # ========================================================
    # OP.GG Fallback Scraper
    # ========================================================

    async def _fetch_from_opgg(
        self,
        game_name: str,
        tag_line: str
    ) -> Optional[SummonerInfo]:
        encoded = f"{urllib.parse.quote(game_name)}-{urllib.parse.quote(tag_line)}"
        url = f"https://www.op.gg/summoners/kr/{encoded}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None

                html = await resp.text()

        # Meta description 파싱
        desc_match = re.search(
            r'<meta\s+(?:name|property)="description"\s+content="([^"]*)"',
            html,
            re.IGNORECASE,
        )
        if not desc_match:
            desc_match = re.search(
                r'<meta\s+content="([^"]*)"\s+(?:name|property)="description"',
                html,
                re.IGNORECASE,
            )

        desc = desc_match.group(1) if desc_match else ""

        tier_pattern = (
            r'/\s*(?:(Iron|Bronze|Silver|Gold|Platinum|Emerald|Diamond)\s*([1-4])|'
            r'(Master|Grandmaster|Challenger))(?:\s*([0-9,]+)\s*LP)?'
        )
        match = re.search(tier_pattern, desc, re.IGNORECASE)

        if match:
            div_tier = match.group(1)
            division = match.group(2)
            apex_tier = match.group(3)
            lp_str = match.group(4)

            tier = (div_tier or apex_tier).upper()
            rank = division if division else None
            lp = int(lp_str.replace(",", "")) if lp_str else 0
        else:
            tier = "UNRANKED"
            rank = None
            lp = 0

        tier_short = format_tier_short(tier, rank)
        mmr = calculate_mmr(tier, rank, lp)

        return SummonerInfo(
            game_name=game_name,
            tag_line=tag_line,
            full_name=f"{game_name} #{tag_line}",
            tier=tier,
            rank=rank,
            lp=lp,
            tier_short=tier_short,
            mmr_score=mmr,
        )

    # ========================================================
    # 전적 부검 (Autopsy)
    # ========================================================

    async def inspect_summoner(
        self,
        game_name: str,
        tag_line: str,
        count: int = 30
    ) -> dict:
        """
        소환사의 솔랭/자랭 티어 및 최근 전적을 분석하여 부검 데이터를 반환합니다.
        """
        if not self.api_key:
            raise RuntimeError("Riot API 키가 설정되지 않았습니다.")

        headers = {"X-Riot-Token": self.api_key}
        encoded_name = urllib.parse.quote(game_name)
        encoded_tag = urllib.parse.quote(tag_line)

        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 1. 챔피언 한글명 맵 가져오기 (캐시)
            champ_map = await self._get_champion_map(session)

            # 2. PUUID 조회
            # 2. PUUID 조회
            account_url = (
                f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
                f"{encoded_name}/{encoded_tag}"
            )
            for _ in range(3):
                async with session.get(account_url, headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", 1.0))
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status == 404:
                        raise SummonerNotFoundException(f"소환사를 찾을 수 없습니다: {game_name}#{tag_line}")
                    if resp.status != 200:
                        raise RuntimeError(f"Account API 오류 (상태 코드: {resp.status})")

                    acc = await resp.json()
                    puuid = acc["puuid"]
                    real_name = acc.get("gameName", game_name)
                    real_tag = acc.get("tagLine", tag_line)
                    break

            # 3. 리그 랭크 정보 조회
            league_url = f"https://kr.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
            for _ in range(3):
                async with session.get(league_url, headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", 1.0))
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status == 200:
                        entries = await resp.json()
                    else:
                        entries = []
                    break

            solo_entry = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
            flex_entry = next((e for e in entries if e.get("queueType") == "RANKED_FLEX_SR"), None)

            solo_str = f"{solo_entry['tier']} {solo_entry['rank']} ({solo_entry['leaguePoints']} LP)" if solo_entry else "UNRANKED"
            flex_str = f"{flex_entry['tier']} {flex_entry['rank']} ({flex_entry['leaguePoints']} LP)" if flex_entry else "UNRANKED"

            # 4. 최근 매치 ID 목록 조회
            match_list_url = f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count={count}"
            for _ in range(3):
                async with session.get(match_list_url, headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", 1.0))
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status == 200:
                        match_ids = await resp.json()
                    else:
                        match_ids = []
                    break

            # 5. 각 매치 상세 정보 병렬 조회 (세마포어로 Rate Limit 방지)
            sem = asyncio.Semaphore(5)

            async def fetch_match(mid: str, retries: int = 3):
                url = f"https://asia.api.riotgames.com/lol/match/v5/matches/{mid}"
                async with sem:
                    for _ in range(retries):
                        try:
                            async with session.get(url, headers=headers) as r:
                                if r.status == 200:
                                    return await r.json()
                                elif r.status == 429:
                                    retry_after = float(r.headers.get("Retry-After", 1.0))
                                    await asyncio.sleep(retry_after)
                                    continue
                        except Exception:
                            pass
                return None

            match_tasks = [fetch_match(mid) for mid in match_ids]
            matches_raw = await asyncio.gather(*match_tasks)

            # 6. 매치 데이터 가공
            parsed_games = []
            queue_map = {
                420: "솔랭",
                440: "자랭",
                450: "칼바람",
                400: "일반",
                430: "일반",
                490: "빠른대전",
                1700: "아레나",
                1900: "URF",
            }

            for m in matches_raw:
                if not m:
                    continue
                info = m.get("info", {})
                qid = info.get("queueId", 0)
                end_ts = info.get("gameEndTimestamp") or (
                    info.get("gameCreation", 0) + info.get("gameDuration", 0) * 1000
                )

                participant = next(
                    (p for p in info.get("participants", []) if p.get("puuid") == puuid),
                    None
                )
                if not participant:
                    continue

                raw_champ = participant.get("championName", "Unknown")
                champ_name = champ_map.get(raw_champ.lower(), raw_champ)
                win = bool(participant.get("win", False))
                kills = participant.get("kills", 0)
                deaths = participant.get("deaths", 0)
                assists = participant.get("assists", 0)

                parsed_games.append({
                    "queue": queue_map.get(qid, "기타"),
                    "champion": champ_name,
                    "win": win,
                    "kills": kills,
                    "deaths": deaths,
                    "assists": assists,
                    "end_ts": end_ts,
                })

            total_games = len(parsed_games)
            wins = sum(1 for g in parsed_games if g["win"])
            losses = total_games - wins
            win_rate = (wins / total_games * 100) if total_games > 0 else 0.0

            total_k = sum(g["kills"] for g in parsed_games)
            total_d = sum(g["deaths"] for g in parsed_games)
            total_a = sum(g["assists"] for g in parsed_games)

            avg_k = total_k / total_games if total_games > 0 else 0.0
            avg_d = total_d / total_games if total_games > 0 else 0.0
            avg_a = total_a / total_games if total_games > 0 else 0.0
            kda = (total_k + total_a) / max(1, total_d)

            return {
                "real_name": real_name,
                "real_tag": real_tag,
                "full_name": f"{real_name}#{real_tag}",
                "solo_tier_str": solo_str,
                "flex_tier_str": flex_str,
                "total_games": total_games,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "kda": kda,
                "avg_k": avg_k,
                "avg_d": avg_d,
                "avg_a": avg_a,
                "recent_games": parsed_games,
            }

    # ========================================================
    # DDragon 챔피언 캐시
    # ========================================================

    async def _get_champion_map(self, session: aiohttp.ClientSession) -> dict:
        if hasattr(self, "_champ_map") and self._champ_map:
            return self._champ_map

        try:
            async with session.get("https://ddragon.leagueoflegends.com/api/versions.json") as resp:
                if resp.status == 200:
                    versions = await resp.json()
                    latest = versions[0]
                else:
                    latest = "14.17.1"

            self._ddragon_version = latest
            url = f"https://ddragon.leagueoflegends.com/cdn/{latest}/data/ko_KR/champion.json"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    champs = data.get("data", {})
                    cmap = {}
                    for cid, cinfo in champs.items():
                        cname = cinfo.get("name", cid)
                        cmap[cid.lower()] = cname
                        cmap[cinfo.get("id", "").lower()] = cname
                        if "key" in cinfo:
                            cmap[str(cinfo["key"])] = cname
                    self._champ_map = cmap
                    return cmap
        except Exception as e:
            print(f"[RiotService] DDragon fetch error: {e}")

        return {}

    # ========================================================
    # 소환사 상세 정보 (User Profile)
    # ========================================================

    async def get_user_profile(
        self,
        game_name: str,
        tag_line: str
    ) -> dict:
        """
        소환사의 레벨, 아이콘, 솔랭/자랭 전적, 모스트 숙련도 챔피언 Top 3을 조회합니다.
        """
        if not self.api_key:
            raise RuntimeError("Riot API 키가 설정되지 않았습니다.")

        headers = {"X-Riot-Token": self.api_key}
        encoded_name = urllib.parse.quote(game_name)
        encoded_tag = urllib.parse.quote(tag_line)

        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 1. 챔피언 맵 및 DDragon 버전
            champ_map = await self._get_champion_map(session)
            ddragon_ver = getattr(self, "_ddragon_version", "14.17.1")

            # 2. PUUID 조회
            account_url = (
                f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
                f"{encoded_name}/{encoded_tag}"
            )
            for _ in range(3):
                async with session.get(account_url, headers=headers) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(float(resp.headers.get("Retry-After", 1.0)))
                        continue
                    if resp.status == 404:
                        raise SummonerNotFoundException(f"소환사를 찾을 수 없습니다: {game_name}#{tag_line}")
                    if resp.status != 200:
                        raise RuntimeError(f"Account API 오류: {resp.status}")

                    acc = await resp.json()
                    puuid = acc["puuid"]
                    real_name = acc.get("gameName", game_name)
                    real_tag = acc.get("tagLine", tag_line)
                    break

            # 3. 소환사 기본 정보 (레벨, 아이콘)
            sum_url = f"https://kr.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
            icon_id = 1
            summoner_level = 1
            async with session.get(sum_url, headers=headers) as resp:
                if resp.status == 200:
                    sum_data = await resp.json()
                    icon_id = sum_data.get("profileIconId", 1)
                    summoner_level = sum_data.get("summonerLevel", 1)

            # 4. 리그 랭크 정보 조회
            league_url = f"https://kr.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
            async with session.get(league_url, headers=headers) as resp:
                entries = await resp.json() if resp.status == 200 else []

            solo_entry = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
            flex_entry = next((e for e in entries if e.get("queueType") == "RANKED_FLEX_SR"), None)

            # 5. 챔피언 마스터리 Top 3
            mastery_url = f"https://kr.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top?count=3"
            async with session.get(mastery_url, headers=headers) as resp:
                mastery_list = await resp.json() if resp.status == 200 else []

            top_masteries = []
            for m in mastery_list:
                cid = str(m.get("championId"))
                cname = champ_map.get(cid, f"챔피언 {cid}")
                clevel = m.get("championLevel", 1)
                cpoints = m.get("championPoints", 0)
                top_masteries.append({
                    "champion_name": cname,
                    "level": clevel,
                    "points": cpoints
                })

            icon_url = f"https://ddragon.leagueoflegends.com/cdn/{ddragon_ver}/img/profileicon/{icon_id}.png"
            opgg_url = f"https://www.op.gg/summoners/kr/{urllib.parse.quote(real_name)}-{urllib.parse.quote(real_tag)}"

            return {
                "game_name": real_name,
                "tag_line": real_tag,
                "full_name": f"{real_name}#{real_tag}",
                "summoner_level": summoner_level,
                "icon_url": icon_url,
                "opgg_url": opgg_url,
                "solo_entry": solo_entry,
                "flex_entry": flex_entry,
                "top_masteries": top_masteries,
            }


# ============================================================
# Autopsy Format Helper
# ============================================================

def format_autopsy_time(game_end_ts: int) -> str:
    import time
    now_ms = time.time() * 1000
    diff_sec = max(0, (now_ms - game_end_ts) / 1000)

    if diff_sec < 60:
        return "방금 전"
    elif diff_sec < 3600:
        return f"{int(diff_sec // 60)}분 전"
    elif diff_sec < 86400:
        return f"{int(diff_sec // 3600)}시간 전"
    else:
        return f"{int(diff_sec // 86400)}일 전"


def format_autopsy_text(data: dict, limit: int = 10) -> str:
    """
    사용자가 요청한 양식에 맞춰 부검 결과를 포맷팅합니다.
    """
    lines = [
        f"🔎 [{data['full_name']}] 전적 부검",
        f"🏆 솔랭: {data['solo_tier_str']}",
        f"🏆 자랭: {data['flex_tier_str']}",
        f"📊 최근 {data['total_games']}전: 승률 {data['win_rate']:.1f}% ({data['wins']}승 {data['losses']}패)",
        f"✨ 평점 {data['kda']:.2f} ({data['avg_k']:.1f} / {data['avg_d']:.1f} / {data['avg_a']:.1f})",
        "---------------------------------",
    ]

    for g in data.get("recent_games", [])[:limit]:
        icon = "💙" if g["win"] else "💔"
        time_ago = format_autopsy_time(g["end_ts"])
        kda_str = f"{g['kills']}/{g['deaths']}/{g['assists']}"
        lines.append(
            f"{icon} [{g['queue']}] {g['champion']:<6}   {kda_str} ({time_ago})"
        )

    return "\n".join(lines)

