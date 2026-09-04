import asyncio

import yt_dlp


# ============================================================
# yt-dlp 설정
# ============================================================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}


YTDL = yt_dlp.YoutubeDL(
    YTDL_OPTIONS
)


# ============================================================
# YouTube
# ============================================================

async def search_song(query: str):
    """
    검색어 또는 URL을 받아
    YouTube 영상 정보를 반환합니다.
    """

    loop = asyncio.get_running_loop()

    def extract():
        return YTDL.extract_info(
            query,
            download=False
        )

    data = await loop.run_in_executor(
        None,
        extract
    )

    if not data:
        raise RuntimeError(
            "검색 결과를 찾을 수 없습니다."
        )

    if "entries" in data:
        entries = [
            entry
            for entry in data["entries"]
            if entry
        ]

        if not entries:
            raise RuntimeError(
                "검색 결과를 찾을 수 없습니다."
            )

        data = entries[0]

    return {
        "title": data.get(
            "title",
            "Unknown"
        ),
        "url": data.get(
            "webpage_url",
            data.get("original_url")
        ),
        "stream_url": data.get("url"),
        "duration": data.get(
            "duration",
            0
        ),
        "thumbnail": data.get(
            "thumbnail"
        )
    }