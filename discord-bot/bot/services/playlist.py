import json
from pathlib import Path


# ============================================================
# 설정
# ============================================================

DATA_DIR = Path("bot/data")
DATA_FILE = DATA_DIR / "playlists.json"


# ============================================================
# Playlist Manager
# ============================================================

class PlaylistManager:

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not DATA_FILE.exists():
            DATA_FILE.write_text(
                "{}",
                encoding="utf-8"
            )


    # ========================================================
    # 내부
    # ========================================================

    def _load(self):
        try:
            return json.loads(
                DATA_FILE.read_text(
                    encoding="utf-8"
                )
            )

        except (json.JSONDecodeError, FileNotFoundError):
            return {}


    def _save(self, data):
        DATA_FILE.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=4
            ),
            encoding="utf-8"
        )


    # ========================================================
    # Playlist
    # ========================================================

    def get_playlist(self, guild_id: int):
        data = self._load()

        return data.get(
            str(guild_id),
            []
        )


    def add_song(
        self,
        guild_id: int,
        title: str,
        url: str
    ):
        data = self._load()

        guild_id = str(guild_id)

        if guild_id not in data:
            data[guild_id] = []

        data[guild_id].append({
            "title": title,
            "url": url
        })

        self._save(data)


    def clear_playlist(self, guild_id: int):
        data = self._load()

        data[str(guild_id)] = []

        self._save(data)


    def remove_song(
        self,
        guild_id: int,
        index: int
    ):
        data = self._load()

        guild_id = str(guild_id)

        playlist = data.get(
            guild_id,
            []
        )

        if not 1 <= index <= len(playlist):
            return None

        removed = playlist.pop(index - 1)

        data[guild_id] = playlist

        self._save(data)

        return removed