"""
구슬 레이스 물리 시뮬레이션 및 MP4 렌더링.

보유 개수만큼 구슬을 낙하시켜 골라인을 먼저 통과한 구슬의 주인이 당첨된다.
당첨자를 사전에 정하지 않으며, 구슬이 균등하므로 확률은 보유 개수 / 전체 개수가 된다.
코스를 수정하면 확률 편향이 생기지 않았는지 재검증할 것.

월드는 세로로 긴 좌표계이고 카메라가 선두를 따라 이동한다.
"""

from __future__ import annotations

import io
import math
import os
import random
import shutil
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from core.theme import BG, DIM, GOLD, PANEL, TEXT
from core.theme import font as _font
from core.theme import make_colors as _make_colors
from core.theme import truncate as _truncate

# ── 월드 / 화면 ──────────────────────────────────────────────

WORLD_W = 520          # 코스 가로 폭
GOAL_Y = 1900          # 골라인
WORLD_H = GOAL_Y + 90

VIEW_W = WORLD_W
VIEW_H = 620           # 카메라 뷰포트 높이

PANEL_W = 320
WIDTH = VIEW_W + PANEL_W + 26
HEADER_H = 52
FOOTER_H = 24

# ── 물리 상수 ────────────────────────────────────────────────

DT = 1 / 120
GRAVITY = 1500.0
RESTITUTION = 0.42     # 반발계수
FRICTION = 0.9992
MAX_SPEED = 1500.0
SUBSTEPS_PER_FRAME = 3 # 프레임당 물리 스텝. 작을수록 느리고 부드럽다

MAX_STEPS = 20000
FPS = 20
HOLD_FRAMES = 40       # 결과 화면 유지 프레임
MAX_FRAMES = 460

MAX_MARBLES = 120

PEG_C = (104, 113, 130)
WALL_C = (126, 136, 155)
SPIN_C = (108, 196, 216)


# ── 코스 구성 요소 ───────────────────────────────────────────


@dataclass
class Peg:
    x: float
    y: float
    r: float = 7.0


@dataclass
class Wall:
    """정적 선분 (경사판, 깔때기)."""

    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class Spinner:
    """회전 십자 막대."""

    x: float
    y: float
    arm: float
    omega: float        # rad/s. 부호가 회전 방향
    arms: int = 4
    angle: float = 0.0

    def segments(self) -> list[tuple[float, float, float, float]]:
        segs = []
        for k in range(self.arms):
            a = self.angle + math.tau * k / self.arms
            segs.append((self.x, self.y,
                         self.x + math.cos(a) * self.arm,
                         self.y + math.sin(a) * self.arm))
        return segs


@dataclass
class Marble:
    owner: int
    color: tuple[int, int, int]
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    r: float = 9.0
    finished_at: int | None = None
    trail: list[tuple[float, float]] = field(default_factory=list)


# ── 코스 만들기 ──────────────────────────────────────────────


def build_course(rng: random.Random) -> tuple[list[Peg], list[Wall], list[Spinner]]:
    """구간을 위에서 아래로 쌓는다. 좌우 대칭을 유지해 특정 위치가 유리해지지 않게 한다."""
    pegs: list[Peg] = []
    walls: list[Wall] = []
    spinners: list[Spinner] = []

    def peg_field(y0: float, rows: int, gap_y: float = 46) -> float:
        for r in range(rows):
            y = y0 + r * gap_y
            count = 8 + (r % 2)
            gap = WORLD_W / (count + 1)
            for c in range(count):
                pegs.append(Peg(gap * (c + 1), y))
        return y0 + rows * gap_y

    def zigzag(y0: float, levels: int = 3, gap_y: float = 92) -> float:
        """좌우 번갈아 기울어진 경사판. 구슬이 미끄러지며 갈립니다."""
        for i in range(levels):
            y = y0 + i * gap_y
            if i % 2 == 0:
                walls.append(Wall(0, y, WORLD_W * 0.72, y + 54))
            else:
                walls.append(Wall(WORLD_W, y, WORLD_W * 0.28, y + 54))
        return y0 + levels * gap_y

    def windmill(y0: float, arm: float, speed: float, count: int = 2) -> float:
        for i in range(count):
            cx = WORLD_W * (i + 1) / (count + 1)
            direction = 1 if i % 2 == 0 else -1
            spinners.append(
                Spinner(cx, y0, arm, direction * speed, angle=rng.uniform(0, math.tau))
            )
        return y0 + arm + 80

    y = 180
    y = peg_field(y, 4)
    y = windmill(y + 40, 68, 2.4, count=2)
    y = peg_field(y + 20, 3)
    y = zigzag(y + 30, levels=3)
    y = windmill(y + 50, 92, 3.0, count=1)
    y = peg_field(y + 30, 4)
    y = zigzag(y + 20, levels=2)
    y = windmill(y + 50, 74, 2.7, count=2)
    y = peg_field(y + 30, 3)

    # 골라인 앞 병목
    neck = 46
    walls.append(Wall(0, GOAL_Y - 190, WORLD_W / 2 - neck, GOAL_Y - 28))
    walls.append(Wall(WORLD_W, GOAL_Y - 190, WORLD_W / 2 + neck, GOAL_Y - 28))

    return pegs, walls, spinners


# ── 물리 ─────────────────────────────────────────────────────


def _resolve_circle(m: Marble, cx: float, cy: float, cr: float,
                    sx: float = 0.0, sy: float = 0.0) -> None:
    """원형 장애물 충돌. (sx, sy)는 장애물 표면 속도."""
    dx, dy = m.x - cx, m.y - cy
    dist = math.hypot(dx, dy)
    limit = cr + m.r
    if dist >= limit or dist < 1e-9:
        return

    nx, ny = dx / dist, dy / dist
    m.x = cx + nx * limit          # 침투 보정
    m.y = cy + ny * limit

    rvx, rvy = m.vx - sx, m.vy - sy
    vn = rvx * nx + rvy * ny
    if vn < 0:                      # 접근 중일 때만
        j = -(1 + RESTITUTION) * vn
        m.vx += j * nx
        m.vy += j * ny


def _resolve_segment(m: Marble, x1: float, y1: float, x2: float, y2: float,
                     pivot: tuple[float, float] | None = None,
                     omega: float = 0.0) -> None:
    """선분 충돌. 최근접점을 원으로 보고 처리하며, 회전팔이면 표면 속도를 함께 넘긴다."""
    ex, ey = x2 - x1, y2 - y1
    length2 = ex * ex + ey * ey
    if length2 < 1e-9:
        return
    t = ((m.x - x1) * ex + (m.y - y1) * ey) / length2
    t = min(max(t, 0.0), 1.0)
    px, py = x1 + ex * t, y1 + ey * t

    sx = sy = 0.0
    if pivot is not None and omega:
        # 회전점 속도 = 중심 변위를 90도 회전
        rx, ry = px - pivot[0], py - pivot[1]
        sx, sy = -omega * ry, omega * rx

    _resolve_circle(m, px, py, 3.0, sx, sy)


def _resolve_marbles(a: Marble, b: Marble) -> None:
    """구슬 간 충돌. 동일 질량 가정."""
    dx, dy = b.x - a.x, b.y - a.y
    dist = math.hypot(dx, dy)
    limit = a.r + b.r
    if dist >= limit or dist < 1e-9:
        return

    nx, ny = dx / dist, dy / dist
    overlap = (limit - dist) / 2
    a.x -= nx * overlap
    a.y -= ny * overlap
    b.x += nx * overlap
    b.y += ny * overlap

    vn = (b.vx - a.vx) * nx + (b.vy - a.vy) * ny
    if vn < 0:
        j = -(1 + RESTITUTION) * vn / 2
        a.vx -= j * nx
        a.vy -= j * ny
        b.vx += j * nx
        b.vy += j * ny


def simulate(
    marbles: list[Marble],
    pegs: list[Peg],
    walls: list[Wall],
    spinners: list[Spinner],
    record_every: int,
    max_frames: int,
) -> tuple[list[list[tuple[float, float]]], list[list[float]], int | None]:
    """반환: (프레임별 위치, 프레임별 풍차 각도, 우승 구슬 index)."""
    # 공간 분할. 매 스텝 전체 핀을 훑지 않기 위함
    CELL = 70
    grid: dict[tuple[int, int], list[Peg]] = {}
    for peg in pegs:
        grid.setdefault((int(peg.x // CELL), int(peg.y // CELL)), []).append(peg)

    frames: list[list[tuple[float, float]]] = []
    angles: list[list[float]] = []
    winner: int | None = None
    step = 0

    while step < MAX_STEPS and len(frames) < max_frames:
        for spinner in spinners:
            spinner.angle += spinner.omega * DT

        for i, m in enumerate(marbles):
            if m.finished_at is not None:
                continue

            m.vy += GRAVITY * DT
            m.vx *= FRICTION
            m.vy *= FRICTION
            speed = math.hypot(m.vx, m.vy)
            if speed > MAX_SPEED:
                m.vx *= MAX_SPEED / speed
                m.vy *= MAX_SPEED / speed

            m.x += m.vx * DT
            m.y += m.vy * DT

            # 좌우 경계
            if m.x < m.r:
                m.x = m.r
                m.vx = abs(m.vx) * RESTITUTION
            elif m.x > WORLD_W - m.r:
                m.x = WORLD_W - m.r
                m.vx = -abs(m.vx) * RESTITUTION

            # 인접 셀만 검사
            gx, gy = int(m.x // CELL), int(m.y // CELL)
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    for peg in grid.get((gx + ox, gy + oy), ()):
                        _resolve_circle(m, peg.x, peg.y, peg.r)

            for w in walls:
                if min(w.y1, w.y2) - 60 < m.y < max(w.y1, w.y2) + 60:
                    _resolve_segment(m, w.x1, w.y1, w.x2, w.y2)

            for sp in spinners:
                if abs(m.y - sp.y) < sp.arm + 30:
                    for x1, y1, x2, y2 in sp.segments():
                        _resolve_segment(m, x1, y1, x2, y2, (sp.x, sp.y), sp.omega)

            if m.y >= GOAL_Y and m.finished_at is None:
                m.finished_at = step
                if winner is None:
                    winner = i

        # 구슬 간 충돌. 근접한 쌍만
        for i in range(len(marbles)):
            a = marbles[i]
            if a.finished_at is not None:
                continue
            for j in range(i + 1, len(marbles)):
                b = marbles[j]
                if b.finished_at is not None:
                    continue
                if abs(a.y - b.y) < 24 and abs(a.x - b.x) < 24:
                    _resolve_marbles(a, b)

        step += 1
        if step % record_every == 0:
            frames.append([(m.x, m.y) for m in marbles])
            angles.append([sp.angle for sp in spinners])

        # 우승 후 잠시 더 기록하고 종료
        if winner is not None and marbles[winner].finished_at is not None:
            if step - marbles[winner].finished_at > record_every * 8:
                break

    return frames, angles, winner


# PIL은 안티에일리어싱을 하지 않으므로 S배로 그린 뒤 축소한다.
S = 2


def _shadow_text(d: ImageDraw.ImageDraw, xy, text: str, fnt, fill) -> None:
    """검은 테두리를 둘러 배경과 무관하게 읽히게 한다. stroke_width로 1회 호출."""
    d.text(xy, text, font=fnt, fill=fill, stroke_width=2, stroke_fill=(12, 14, 18))


def _draw_world_background(pegs: list[Peg], walls: list[Wall]) -> Image.Image:
    """정적 요소를 한 장에 미리 그려둔다. 프레임마다 카메라 위치로 crop해서 쓴다."""
    img = Image.new("RGB", (WORLD_W * S, WORLD_H * S), PANEL)
    d = ImageDraw.Draw(img)

    for peg in pegs:
        x, y, r = peg.x * S, peg.y * S, peg.r * S
        d.ellipse([(x - r, y - r), (x + r, y + r)], fill=PEG_C)

    for w in walls:
        d.line([(w.x1 * S, w.y1 * S), (w.x2 * S, w.y2 * S)], fill=WALL_C, width=7 * S)

    # 골라인
    gy = GOAL_Y * S
    box = 11 * S
    for x in range(0, WORLD_W * S, box * 2):
        d.rectangle([(x, gy), (x + box, gy + box)], fill=GOLD)
        d.rectangle([(x + box, gy + box), (x + box * 2, gy + box * 2)], fill=GOLD)
    d.text((10 * S, gy + 28 * S), "GOAL", font=_font(20 * S), fill=GOLD)
    return img


def render_marble_race(
    entries: list[tuple[str, int]], seed: int | None = None
) -> tuple[io.BytesIO, int]:
    """entries: [(이름, 구슬개수), ...] -> (MP4 버퍼, 우승자 index)."""
    rng = random.Random(seed)
    names = [e[0] for e in entries]
    counts = [e[1] for e in entries]
    total = sum(counts)
    colors = _make_colors(len(entries))

    radius = 10 if total <= 30 else (8 if total <= 60 else 6)

    pegs, walls, spinners = build_course(rng)

    # 출발 위치에 따른 유불리를 없애기 위해 소유자 순서를 섞는다.
    owners: list[int] = []
    for owner, count in enumerate(counts):
        owners += [owner] * count
    rng.shuffle(owners)

    per_row = 8
    marbles = [
        Marble(
            owner=owner,
            color=colors[owner],
            x=WORLD_W * (0.12 + 0.76 * ((k % per_row) + 0.5) / per_row) + rng.uniform(-3, 3),
            y=40 - (k // per_row) * (radius * 3),
            vx=rng.uniform(-25, 25),
            r=radius,
        )
        for k, owner in enumerate(owners)
    ]

    frames_pos, angles, winner_marble = simulate(
        marbles, pegs, walls, spinners,
        record_every=SUBSTEPS_PER_FRAME, max_frames=MAX_FRAMES,
    )
    winner = marbles[winner_marble].owner if winner_marble is not None else 0

    lay = {"row_h": 34, "name": 15, "sub": 12} if len(entries) <= 9 else (
        {"row_h": 25, "name": 13, "sub": 11} if len(entries) <= 15 else
        {"row_h": 20, "name": 12, "sub": 10}
    )
    panel_need = HEADER_H + lay["row_h"] * len(entries) + FOOTER_H + 40
    height = max(HEADER_H + VIEW_H + FOOTER_H, panel_need)

    world_bg = _draw_world_background(pegs, walls)

    tag_size = 15 if total <= 24 else (13 if total <= 50 else 11)
    f_tag = _font(tag_size * S)
    f_name = _font(lay["name"] * S)
    f_sub = _font(lay["sub"] * S)
    f_title = _font(22 * S)
    f_foot = _font(11 * S)

    frames: list[Image.Image] = []
    cam = 0.0
    for fi, positions in enumerate(frames_pos):
        ys = sorted(p[1] for p in positions)
        lead_y = ys[-1]
        mid_y = ys[len(ys) // 2]

        # 선두만 쫓으면 뒤처진 구슬이 전부 화면 밖으로 나가므로 무리 중앙을 함께 잡는다.
        target = (lead_y + mid_y) / 2 - VIEW_H * 0.45
        target = max(target, lead_y - VIEW_H * 0.86)
        target = min(max(target, 0), WORLD_H - VIEW_H)

        cam = target if fi == 0 else cam * 0.55 + target * 0.45

        img = Image.new("RGB", (WIDTH * S, height * S), BG)
        view = world_bg.crop((0, int(cam) * S, WORLD_W * S, (int(cam) + VIEW_H) * S))
        img.paste(view, (14 * S, HEADER_H * S))
        d = ImageDraw.Draw(img)

        ox, oy = 14 * S, HEADER_H * S

        # 풍차는 프레임마다 각도가 달라 배경에 넣을 수 없다
        frame_angles = angles[fi] if fi < len(angles) else [0.0] * len(spinners)
        for si, sp in enumerate(spinners):
            if not (cam - sp.arm < sp.y < cam + VIEW_H + sp.arm):
                continue
            ang = frame_angles[si]
            cx, cy = ox + sp.x * S, oy + (sp.y - cam) * S
            for k in range(sp.arms):
                a = ang + math.tau * k / sp.arms
                d.line(
                    [(cx, cy), (cx + math.cos(a) * sp.arm * S, cy + math.sin(a) * sp.arm * S)],
                    fill=SPIN_C, width=9 * S,
                )
            d.ellipse([(cx - 7 * S, cy - 7 * S), (cx + 7 * S, cy + 7 * S)],
                      fill=(235, 250, 253))

        leader = max(range(len(positions)), key=lambda k: positions[k][1])

        for mi, (mx, my) in enumerate(positions):
            sy = my - cam
            if not (-24 < sy < VIEW_H + 24):
                continue
            m = marbles[mi]
            cx, cy = ox + mx * S, oy + sy * S
            r = m.r * S

            d.ellipse([(cx - r, cy - r), (cx + r, cy + r)],
                      fill=m.color, outline=(18, 21, 27), width=2 * S)
            d.ellipse([(cx - r * 0.55, cy - r * 0.6), (cx - r * 0.1, cy - r * 0.15)],
                      fill=(255, 255, 255))
            if mi == leader:
                d.ellipse([(cx - r - 4 * S, cy - r - 4 * S), (cx + r + 4 * S, cy + r + 4 * S)],
                          outline=GOLD, width=3 * S)

        # 이름표는 구슬을 모두 그린 뒤 얹어야 가려지지 않는다
        for mi, (mx, my) in enumerate(positions):
            sy = my - cam
            if not (-24 < sy < VIEW_H + 24):
                continue
            m = marbles[mi]
            cx, cy = ox + mx * S, oy + sy * S
            _shadow_text(
                d, (cx + m.r * S + 5 * S, cy - tag_size * S * 0.6),
                names[m.owner], f_tag,
                GOLD if mi == leader else TEXT,
            )

        _shadow_text(d, (20 * S, 14 * S), "구슬 레이스", f_title, TEXT)
        progress = min(lead_y / GOAL_Y, 1.0)
        bx0, bx1 = 200 * S, (VIEW_W - 10) * S
        d.rounded_rectangle([(bx0, 24 * S), (bx1, 34 * S)], radius=5 * S, fill=(52, 58, 72))
        d.rounded_rectangle([(bx0, 24 * S), (bx0 + (bx1 - bx0) * progress, 34 * S)],
                            radius=5 * S, fill=GOLD)

        px = (VIEW_W + 40) * S
        d.text((px, 18 * S), f"참가 {len(entries)}명 · 구슬 {total}개", font=f_sub, fill=DIM)
        for i in range(len(entries)):
            cy = (HEADER_H + lay["row_h"] * i + lay["row_h"] // 2) * S
            d.ellipse([(px, cy - 6 * S), (px + 12 * S, cy + 6 * S)], fill=colors[i])
            d.text((px + 20 * S, cy - lay["name"] * S // 2 - S),
                   _truncate(d, names[i], f_name, 150 * S), font=f_name, fill=TEXT)
            d.text((px + 182 * S, cy - lay["sub"] * S // 2),
                   f"{counts[i]}개  {counts[i] / total * 100:.0f}%", font=f_sub, fill=DIM)

        d.text((20 * S, (height - FOOTER_H + 2) * S),
               "당첨 확률 = 내 구슬 ÷ 전체 구슬 · 결과는 물리 그대로입니다",
               font=f_foot, fill=DIM)

        # 2배로 그린 뒤 축소해 계단 현상을 없앤다
        frames.append(img.resize((WIDTH, height), Image.BOX))

    big = frames[-1].resize((WIDTH * S, height * S), Image.NEAREST)
    d = ImageDraw.Draw(big)
    d.rectangle([(14 * S, (HEADER_H + VIEW_H // 2 - 46) * S),
                 ((14 + VIEW_W) * S, (HEADER_H + VIEW_H // 2 + 46) * S)], fill=(16, 19, 25))
    f_big = _font(34 * S)
    label = f"WINNER  {names[winner]}"
    w = d.textlength(label, font=f_big)
    _shadow_text(d, ((14 + VIEW_W / 2) * S - w / 2, (HEADER_H + VIEW_H // 2 - 22) * S),
                 label, f_big, GOLD)
    final = big.resize((WIDTH, height), Image.BOX)
    frames.append(final)

    # 결과 화면 유지
    frames.extend([final] * HOLD_FRAMES)

    return _encode_mp4(frames, (WIDTH, height)), winner


def _find_ffmpeg() -> str:
    """FFMPEG_PATH -> PATH -> winget 설치 경로 순으로 탐색."""
    configured = os.getenv("FFMPEG_PATH")
    if configured and Path(configured).exists():
        return configured

    found = shutil.which("ffmpeg")
    if found:
        return found

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        pattern = "Microsoft/WinGet/Packages/Gyan.FFmpeg*/*/bin/ffmpeg.exe"
        for candidate in Path(local).glob(pattern):
            return str(candidate)

    raise RuntimeError(
        "FFmpeg를 찾지 못했습니다. `winget install Gyan.FFmpeg` 로 설치하거나 "
        ".env의 FFMPEG_PATH에 전체 경로를 적어주세요."
    )


def _encode_mp4(frames: list[Image.Image], size: tuple[int, int]) -> io.BytesIO:
    """rawvideo를 FFmpeg에 파이프해 H.264로 인코딩. GIF 대비 용량 1/7."""
    w, h = size
    ffmpeg = _find_ffmpeg()

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "race.mp4")
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{w}x{h}", "-r", str(FPS),
            "-i", "-",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",           # 호환성
            "-crf", "22",
            "-preset", "veryfast",
            "-movflags", "+faststart",
            out_path,
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for frame in frames:
                proc.stdin.write(frame.tobytes())
        finally:
            proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg 인코딩 실패: {err[:400]}")

        buffer = io.BytesIO(Path(out_path).read_bytes())

    buffer.seek(0)
    return buffer
