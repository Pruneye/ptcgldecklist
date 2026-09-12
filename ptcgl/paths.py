"""Locations the PTCGL client reads and writes on Windows."""

import os
import glob

APPDATA = os.path.join(
    os.environ.get("USERPROFILE", ""),
    "AppData", "LocalLow", "pokemon", "Pokemon TCG Live",
)
CONFIG_CACHE = os.path.join(APPDATA, "config-cache")
INSTALL = os.path.join(
    os.environ.get("USERPROFILE", ""),
    "The Pokémon Company International",
    "Pokémon Trading Card Game Live",
)
MANAGED = os.path.join(INSTALL, "Pokemon TCG Live_Data", "Managed")

PLAYER_LOG = os.path.join(APPDATA, "Player.log")
PLAYER_LOG_PREV = os.path.join(APPDATA, "Player-prev.log")


def config(name):
    """Path to a config-cache document, with or without its _0.0 suffix."""
    direct = os.path.join(CONFIG_CACHE, name if name.endswith(".json") else name + ".json")
    if os.path.exists(direct):
        return direct
    hits = sorted(glob.glob(os.path.join(CONFIG_CACHE, name + "_*.json")))
    if not hits:
        raise FileNotFoundError(f"no config-cache document matching {name!r}")
    return hits[0]


def card_databases():
    """Every English card-database document, sorted."""
    return sorted(glob.glob(os.path.join(CONFIG_CACHE, "card-database-*_en_*.json")))


def profile_dirs():
    """Per-account directories, each holding that account's unsaved deck state."""
    out = []
    for entry in sorted(glob.glob(os.path.join(APPDATA, "*"))):
        if os.path.isdir(entry) and os.path.exists(
            os.path.join(entry, "unsaved-deckinfo.json")
        ):
            out.append(entry)
    return out


def game_logs():
    """Per-session gameplay logs, newest first."""
    hits = glob.glob(os.path.join(APPDATA, "Game*.log"))
    return sorted(hits, key=os.path.getmtime, reverse=True)


def check():
    """Report which expected locations are present."""
    return {
        "app data": os.path.isdir(APPDATA),
        "config cache": os.path.isdir(CONFIG_CACHE),
        "install": os.path.isdir(INSTALL),
        "player log": os.path.exists(PLAYER_LOG),
    }
