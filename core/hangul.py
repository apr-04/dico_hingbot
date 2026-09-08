"""
한글 조사 판별.

한글 코드포인트는 ((초성 × 21) + 중성) × 28 + 종성 구조라,
'가'(U+AC00)로부터의 거리를 28로 나눈 나머지가 곧 받침이다. 0이면 받침 없음.
"""

from __future__ import annotations

HANGUL_START = 0xAC00
HANGUL_END = 0xD7A3
JONGSUNG_COUNT = 28
JONG_RIEUL = 8  # ㄹ. '(으)로' 예외 판정에 사용

# 한글로 읽었을 때 받침으로 끝나는 것들
DIGITS_WITH_BATCHIM = set("013678")  # 영 일 삼 육 칠 팔
LETTERS_WITH_BATCHIM = set("lmnr")   # 엘 엠 엔 알


def _jongsung(char: str) -> int | None:
    code = ord(char)
    if HANGUL_START <= code <= HANGUL_END:
        return (code - HANGUL_START) % JONGSUNG_COUNT
    return None


def has_batchim(word: str) -> bool | None:
    """마지막 글자의 받침 유무. 판별 불가면 None."""
    word = (word or "").strip()
    if not word:
        return None

    char = word[-1]
    jong = _jongsung(char)
    if jong is not None:
        return jong != 0
    if char.isdigit():
        return char in DIGITS_WITH_BATCHIM
    if char.isascii() and char.isalpha():
        return char.lower() in LETTERS_WITH_BATCHIM
    return None


def particle(word: str, pair: str = "을/를") -> str:
    """
    조사만 반환한다. pair는 "받침있음/받침없음" 순.

        particle("뉴진스")  -> "를"
        particle("2NE1")   -> "을"
    """
    with_b, without_b = pair.split("/")
    batchim = has_batchim(word)

    if batchim is None:
        return f"{with_b}({without_b})"

    # ㄹ 받침은 '으로'가 아니라 '로'
    if with_b in ("으로", "으로써", "으로서"):
        if _jongsung((word or "").strip()[-1:]) == JONG_RIEUL:
            return without_b

    return with_b if batchim else without_b


def josa(word: str, pair: str = "을/를") -> str:
    """단어에 조사를 붙여 반환한다."""
    return f"{word}{particle(word, pair)}"
