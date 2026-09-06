"""Keyword-dictionary expansion for the RbxScout omni-search crawler.

The original 662-word dictionary lives verbatim in ``scout_core.py`` — its
positions must never change because the crawl cursor
(``keyword_crawl_state.next_index``) is positional. This module is APPENDED
after those seeds, so the cursor migrates seamlessly: positions 0..660 stay
identical, the sweep simply continues into the expansion.

Design:
    BASES (~240 curated Roblox game topics/genres/mechanics)
  × MODIFIERS (~55 quality/setting/style qualifiers)
  = ~13,400 unique two-word queries, deduped and order-stable.

Determinism: no randomness, no network — the expansion is a fixed cross
product filtered in a fixed order, so the list is byte-identical on every
machine and every test run. Regenerate or extend by editing BASES/MODIFIERS;
new entries are appended, keeping the whole dictionary append-only.

Sweep maths: 14,682 total words / 100 per sync = 147 syncs; at the
10-minute finder cadence that is a full sweep in ~24.5 hours.
"""

from __future__ import annotations

# Curated base topics: genres, mechanics, settings, animals, jobs, sports,
# cultures and activities that are productive on Roblox omni-search.
BASES = [
    # -- mechanics & formats ------------------------------------------------
    "steal a", "rob a", "grow a", "build a", "escape the", "survive the",
    "raise a", "feed a", "catch a", "collect the", "upgrade your", "buy a",
    "sell a", "duplicate", "trade", "heist", "raid", "absorb", "merge",
    "fuse", "evolve", "hatch", "spin for", "roll for", "rebirth", "prestige",
    "auto farm", "auto click", "speedrun", "obby but", "tycoon but",
    "simulator but", "every second", "every click", "+1 speed", "+1 jump",
    "climb the", "don't fall", "don't die",
    # -- core genres --------------------------------------------------------
    "obby", "tycoon", "simulator", "horror", "anime", "parkour", "clicker",
    "roleplay", "zombie", "pet", "race", "tower", "fighting", "shooter",
    "survival", "escape", "puzzle", "builder", "farming", "city", "story",
    "adventure", "magic", "sword", "ninja", "pirate", "space", "dragon",
    "monster", "dungeon", "arena", "battle", "war", "army", "kingdom",
    "empire", "castle", "hero", "superhero", "villain", "prison", "school",
    "hospital", "hotel", "restaurant", "cafe", "bakery", "salon", "spa",
    "gym", "dance", "music", "art", "fashion", "model", "beauty", "makeup",
    "dress", "wedding", "baby", "family", "date", "love", "romance",
    "vampire", "werewolf", "ghost", "haunted", "spooky", "creepy", "dark",
    "murder", "mystery", "detective", "spy", "military", "naval", "aviation",
    "spaceflight", "sci-fi", "cyberpunk", "steampunk", "wasteland",
    "wilderness", "summer", "disaster", "tornado", "tsunami", "volcano",
    "battlegrounds", "sniper", "battle royale", "deathmatch", "tournament",
    "tower defense", "idle", "incremental", "gacha", "unboxing",
    # -- animals & creatures ------------------------------------------------
    "cat", "dog", "fox", "wolf", "dinosaur", "shark", "whale", "elephant",
    "monkey", "bird", "eagle", "penguin", "panda", "tiger", "lion", "bear",
    "snake", "spider", "bat", "hamster", "rabbit", "horse", "pony",
    "unicorn", "mermaid", "fairy", "elf", "orc", "goblin", "troll", "giant",
    "demon", "angel", "wizard", "witch", "knight", "king", "queen",
    "prince", "princess", "slime", "blob", "robot", "alien", "kaiju",
    "godzilla", "kraken", "phoenix", "griffin",
    # -- jobs & vehicles ----------------------------------------------------
    "police", "firefighter", "doctor", "teacher", "chef", "pilot",
    "astronaut", "farmer", "miner", "taxi", "bus", "truck", "train",
    "plane", "boat", "ship", "submarine", "rocket", "helicopter", "car",
    "drift", "drag race", "skateboard", "bike", "scooter",
    # -- sports & activities ------------------------------------------------
    "bowling", "golf", "basketball", "football", "soccer", "baseball",
    "tennis", "hockey", "boxing", "wrestling", "karate", "archery",
    "hunting", "fishing", "camping", "hiking", "climbing", "swimming",
    "surfing", "diving", "skiing", "snowboarding", "skating", "cheer",
    "gymnastics", "dodgeball", "kickball", "capture the flag",
    # -- places, cultures & eras -------------------------------------------
    "medieval", "western", "cowboy", "viking", "egypt", "rome", "greece",
    "japan", "china", "korea", "india", "brazil", "mexico", "philippines",
    "indonesia", "nigeria", "africa", "hawaii", "alaska", "amazon",
    "sahara", "arctic", "island", "desert", "jungle", "swamp", "cave",
    "mountain", "skyblock", "underwater", "moon", "mars", "galaxy",
    "backrooms", "liminal", "subway", "mall", "amusement park", "waterpark",
    "zoo", "aquarium", "museum", "library", "haunted house", "asylum",
    # -- brainrot & meme themes --------------------------------------------
    "brainrot", "skibidi", "sigma", "ohio", "capybara", "doge", "pepe",
    "chad", "gigachad", "mascot", "digital circus", "smurf cat",
    "strawberry elephant", "grimace", "quandale", "caseoh", "streamer",
    "viral", "tiktoker", "youtuber",
]

# Qualifiers crossed onto every base. Attribute words only — no base terms —
# so the product reads like natural search queries.
MODIFIERS = [
    "simulator", "tycoon", "obby", "game", "2", "3", "3d", "2d", "8 bit",
    "pixel", "blocky", "low poly", "realistic", "roleplay", "story",
    "hardcore", "hard mode", "extreme", "impossible", "easy mode", "casual",
    "pro", "beginner", "multiplayer", "single player", "coop", "pvp", "pve",
    "sandbox", "open world", "anime", "cartoon", "cute", "funny", "scary",
    "aesthetic", "vaporwave", "retro", "future", "medieval", "modern",
    "deluxe", "gold", "mega", "ultra", "chapter 1",
]


def _expand() -> list[str]:
    """Cross-join BASES × MODIFIERS into unique queries, order-stable.

    A combo is skipped when the base already contains the modifier as a
    whole word (``parkour simulator`` is useful; ``simulator simulator``
    and ``anime anime`` are not). First occurrence wins, so inserting new
    bases/modifiers never reshuffles existing entries.
    """
    seen: set[str] = set()
    out: list[str] = []
    for base in BASES:
        base_words = set(base.split())
        for mod in MODIFIERS:
            if mod in base_words:
                continue
            kw = f"{base} {mod}"
            if kw in seen:
                continue
            seen.add(kw)
            out.append(kw)
    return out


KEYWORD_EXPANSION = _expand()
