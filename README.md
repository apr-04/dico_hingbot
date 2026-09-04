# dico_hingbot

디스코드 음악 재생 + 핀볼 추첨 봇.

## 요구사항

- Python 3.10+
- FFmpeg (오디오 변환, 영상 인코딩)

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian/Ubuntu
```

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## 설정 (.env)

| 키 | 필수 | 설명 |
|---|---|---|
| `DISCORD_TOKEN` | O | 봇 토큰 |
| `GUILD_ID` | | 지정 시 슬래시 명령어 즉시 등록. 미지정 시 전역 등록(최대 1시간) |
| `FFMPEG_PATH` | | FFmpeg가 PATH에 없을 때 |
| `YTDL_COOKIE_FILE` | | 유튜브 봇 차단 우회용 |

봇 초대 시 필요한 권한: `View Channels`, `Send Messages`, `Embed Links`, `Connect`, `Speak`
Scopes: `bot`, `applications.commands`

Privileged Intent는 불필요. 단, `@역할` 멘션으로 핀볼을 부여하려면 `SERVER MEMBERS INTENT` 필요.

## 구조

```
bot.py                진입점. cog 로딩, 명령어 등록, 오류 처리
cogs/music.py         음악 명령어, 재생 루프
cogs/pinball.py       핀볼 명령어, 보유량 저장
core/marble_race.py   구슬 물리 엔진, 코스 생성, MP4 렌더링
core/hangul.py        한글 조사 판별
core/theme.py         공용 색상/폰트
data/pinball.json     핀볼 보유 현황 (자동 생성)
```

cog 추가 시 `bot.py` 의 `INITIAL_COGS` 에 등록.

## 명령어

### 음악

음성 채널의 내장 채팅창에서만 동작.

| 명령어 | 설명 |
|---|---|
| `/재생 <검색어\|URL> [위치]` | 재생/대기열 추가. 위치: 맨 뒤, 다음 곡, 지금 바로 |
| `/검색 <검색어>` | 후보 5곡 중 선택 |
| `/일시정지` `/다시재생` `/정지` | 재생 제어 |
| `/이전` `/다음` `/스킵` | 곡 이동 |
| `/대기열` `/최근` `/지금곡` | 상태 조회 |
| `/맨앞으로 <곡>` `/제거 <곡>` `/셔플` | 대기열 편집 |
| `/반복 <모드>` | 한곡 반복, 반복, 무작위 반복, 반복 해제 |
| `/볼륨 <0-100>` | 음량 |
| `/자동재생 <켜기\|끄기>` | 대기열 소진 시 관련곡 자동 추가 |

### 핀볼

| 명령어 | 설명 |
|---|---|
| `/핀볼 부여 [대상] [개수]` | `@A 10 @B 3` 형식으로 개별 지정. 미지정 시 음성 채널 전원 |
| `/핀볼 목록` | 보유 현황, 당첨 확률 |
| `/핀볼 시작` | 추첨 실행. 종료 후 자동 초기화 |
| `/핀볼 초기화` | 전체 리셋 |
| `/골라줘 [항목]` | 쉼표 구분 항목 중 무작위 선택 |

## 동작

**음악** — 대기열에는 유튜브 페이지 URL만 저장하고, 재생 직전에 스트림 URL을 조회한다.
스트림 URL은 수 시간 후 만료되므로 미리 받아두면 뒷 순번 곡이 실패한다.
썸네일도 이 조회에서 함께 확보한다.

**핀볼** — 참가자별 보유 개수만큼 구슬을 생성해 물리 시뮬레이션으로 낙하시키고,
골라인을 먼저 통과한 구슬의 주인이 당첨된다. 당첨자를 사전에 결정하지 않으며,
구슬이 균등하므로 당첨 확률은 `보유 개수 / 전체 개수` 가 된다.
500회 시뮬레이션 검증 결과 이론값 50/15/35% 대비 실측 50.2/14.0/35.8%.
코스 수정 시 재검증 필요.

## 개발 노트

- `requirements.txt` 는 `discord.py[voice]` 형태여야 한다. discord.py 2.7부터 음성 백엔드가
  `davey` 로 변경되어, `discord.py` 와 `PyNaCl` 을 개별 지정하면 `davey` 가 누락된다.
  이 경우 봇은 정상 기동하고 명령어도 등록되지만 음성만 동작하지 않는다.
  또한 discord.py가 `PyNaCl<1.6` 을 요구하므로 상위 버전을 직접 지정하면 안 된다.

- 음성 채널 인원 확인에는 `channel.voice_states` 를 쓴다. `channel.members` 는 내부적으로
  `guild.get_member()` 를 호출해 멤버 캐시에 의존하는데, `SERVER MEMBERS INTENT` 가 없으면
  캐시가 비어 실제 접속자가 누락되고 자동 퇴장이 오작동한다.

- 임베드만 전송할 때 content에 빈 문자열을 넘기면 거부된다. `Music._reply()` 가 `None` 으로 변환한다.

- 영상은 MP4로 인코딩한다. 동일 길이 기준 GIF 5MB → MP4 0.9MB.

- 렌더링 시 정적 배경은 1회만 그려 캐시하고, 글자 테두리는 `stroke_width` 를 쓴다.
  프레임마다 재작성하면 인원수에 비례해 급격히 느려진다.

- 조사는 `core/hangul.py` 의 `particle()` 로 처리한다. 한글 유니코드가
  `((초성 × 21) + 중성) × 28 + 종성` 구조이므로 코드포인트를 28로 나눈 나머지로 받침을 판별한다.

- Windows에서 로그를 파일로 리다이렉트하거나 `pythonw` 로 실행할 경우를 위해
  `bot.py` 에서 표준 출력을 UTF-8로 전환한다.
