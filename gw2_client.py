"""
GW2 API client and nourishment analysis for the Extract Nourishment Tool.

Finds the cheapest chef-crafted food per extract type (Fine, Masterwork, Rare,
Exotic) to compost with the Portable Composter. These extracts are combined
into Exquisite Extract of Nourishment (5 Fine + 5 Masterwork + 5 Rare + 10
Exotic). Ascended food (rating 500) is excluded — we do not salvage ascended food.

Salvage output by recipe min_rating (Portable Composter). We use wiki 100% extract
  ranges only so each recipe guarantees the correct extract type.
  See https://wiki.guildwars2.com/wiki/Portable_Composter/Salvage_research and
  https://wiki.guildwars2.com/wiki/Portable_Composter/Guide.
  - Fine:       min_rating 0–50 (100% fine)
  - Masterwork: min_rating 150–225 (100% masterwork)
  - Rare:       min_rating 300–375 (100% rare)
  - Exotic:     min_rating 400–450 (100% exotic)
  - Ascended (500) excluded.

  When no tradeable option exists for a tier, we show a placeholder and the user can use
  the wiki guide for crafting/karma alternatives.

API docs: https://wiki.guildwars2.com/wiki/API:2
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

import requests

# Default GW2 API base URL (no key required for items/commerce)
DEFAULT_API_BASE = "https://api.guildwars2.com"
# Item batch size per request (API-friendly)
ITEMS_PAGE_SIZE = 200
# Copper per gold (GW2 currency: 1 gold = 10000 copper)
COPPER_PER_GOLD = 10000
COPPER_PER_SILVER = 100
# Cache TTL in seconds (10 minutes)
CACHE_TTL_SECONDS = 600
# API rate limit: 300 burst, 5 tokens/sec refill (wiki). Delay between uncached batch requests.
REQUEST_DELAY_SECONDS = 0.21  # ~5 requests per second max
# Retry config: max attempts for 429 and for timeout/connection errors
MAX_RETRIES = 3
# When 429 has no Retry-After, wait this long (refill is 300/min = 5/sec)
RATE_LIMIT_WAIT_SECONDS = 60
# Initial backoff for timeout/connection retries (seconds), then doubled each time
BACKOFF_INITIAL_SECONDS = 2

logger = logging.getLogger(__name__)


def _cache_dir(base_url: str, lang: Optional[str] = None) -> Path:
    """Return cache directory for this base URL and optional lang."""
    safe = re.sub(r"[^\w\-.]", "_", base_url.rstrip("/").replace("https://", "").replace("http://", ""))
    root = _app_dir() / ".cache" / "gw2" / safe
    if lang:
        root = root / lang
    return root


def _batch_hash(ids: list[int]) -> str:
    """Stable hash for a list of IDs (for cache keys)."""
    key = ",".join(str(x) for x in sorted(ids))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _cache_get(path: Path, max_age_seconds: int = CACHE_TTL_SECONDS) -> Any:
    """Load JSON from cache if file exists and is fresh. Return None otherwise."""
    if not path.exists():
        return None
    try:
        if path.stat().st_mtime + max_age_seconds < time.time():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _cache_set(path: Path, data: Any) -> None:
    """Write JSON to cache file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as e:
        logger.debug("Could not write cache %s: %s", path, e)


def _app_dir() -> Path:
    """Return the application directory (where the script lives)."""
    return Path(__file__).resolve().parent


def purge_cache() -> bool:
    """
    Remove all GW2 API cache files under .cache/gw2 in the app directory.
    Returns True if the cache was purged (or did not exist), False on error.
    """
    cache_root = _app_dir() / ".cache" / "gw2"
    if not cache_root.exists():
        return True
    try:
        shutil.rmtree(cache_root)
        logger.info("Cache purged: %s", cache_root)
        return True
    except OSError as e:
        logger.warning("Could not purge cache %s: %s", cache_root, e)
        return False


def state_path() -> Path:
    """Path to the JSON state file inside the app directory."""
    return _app_dir() / "state.json"


def food_list_path() -> Path:
    """Path to the food list JSON file (item names to consider for trading post fetch)."""
    return _app_dir() / "food.json"


def load_food_list() -> list[str]:
    """
    Load the list of food item names from food.json.
    When the list is non-empty, run_analysis only considers items in this list.
    Includes both item_names (single-serving food) and feast_names (feasts; 10 extracts each).
    Returns empty list if file is missing or both item_names and feast_names are empty.
    """
    path = food_list_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        names = data.get("item_names")
        if not isinstance(names, list):
            names = []
        feast_names = data.get("feast_names")
        if not isinstance(feast_names, list):
            feast_names = []
        combined = [str(n).strip() for n in names + feast_names if str(n).strip()]
        return list(dict.fromkeys(combined))  # preserve order, dedupe
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load food list from %s: %s", path, e)
        return []


def load_food_list_feast_names() -> list[str]:
    """
    Load only feast names from food.json (for UI/documentation).
    Feasts yield 10 extracts per compost and can be more cost-effective than single-extract food.
    """
    path = food_list_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        feast_names = data.get("feast_names")
        if isinstance(feast_names, list):
            return [str(n).strip() for n in feast_names if str(n).strip()]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_food_list(
    item_names: list[str],
    feast_names: Optional[list[str]] = None,
) -> None:
    """Save food and optional feast names to food.json. Feasts give 10 extracts per compost."""
    path = food_list_path()
    data = {
        "item_names": list(item_names),
        "source": "https://wiki.guildwars2.com/wiki/Food",
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if feast_names is not None:
        data["feast_names"] = list(feast_names)
        data["feast_source"] = "https://wiki.guildwars2.com/wiki/Category:Feasts"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.warning("Could not save food list to %s: %s", path, e)


def fetch_food_names_from_wiki() -> tuple[list[str], Optional[str]]:
    """
    Fetch the list of food item names from the GW2 Wiki Food page.
    Parses the wikitext {{Food table row|Item Name|...}} templates.
    Returns (list of item names, None) on success, or ([], error_message) on failure.
    """
    api_url = "https://wiki.guildwars2.com/api.php"
    params = {
        "action": "parse",
        "page": "Food",
        "prop": "wikitext",
        "format": "json",
    }
    try:
        r = requests.get(api_url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return [], str(e)
    except ValueError as e:
        return [], f"Invalid JSON: {e}"

    parse = data.get("parse")
    if not parse:
        return [], "Wiki API did not return parse data"
    wikitext = (parse.get("wikitext") or {}).get("*") or ""
    if not wikitext:
        return [], "No wikitext in response"

    # GW2 Wiki uses {{Food table row|Item Name|filters=...}} templates
    seen: set[str] = set()
    names: list[str] = []
    for m in re.finditer(r"\{\{Food table row\|([^|}+]+)(?:\|[^}]*)?\}\}", wikitext):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names, None


# Meta pages in Category:Feasts to exclude from the feast item list
_WIKI_FEAST_EXCLUDE = frozenset({"feast (food)", "ascended feast"})


def fetch_feast_names_from_wiki() -> tuple[list[str], Optional[str]]:
    """
    Fetch feast item names from the GW2 Wiki Category:Feasts.
    Feasts give 10 extracts per compost (vs 1 for regular food) and can be more cost-effective.
    Returns (list of feast names, None) on success, or ([], error_message) on failure.
    """
    api_url = "https://wiki.guildwars2.com/api.php"
    all_names: list[str] = []
    cmcontinue: Optional[str] = None
    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": "Category:Feasts",
            "cmlimit": 500,
            "format": "json",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        try:
            r = requests.get(api_url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            return [], str(e)
        except ValueError as e:
            return [], f"Invalid JSON: {e}"
        query = data.get("query") or {}
        members = query.get("categorymembers") or []
        for m in members:
            title = (m.get("title") or "").strip()
            if not title:
                continue
            key = title.lower()
            if key in _WIKI_FEAST_EXCLUDE:
                continue
            all_names.append(title)
        cmcontinue = (data.get("continue") or {}).get("cmcontinue")
        if not cmcontinue:
            break
    return all_names, None


def fetch_food_and_feast_names_from_wiki() -> tuple[list[str], list[str], Optional[str]]:
    """
    Fetch both food and feast names from the Wiki.
    Returns (item_names, feast_names, None) on success, or ([], [], error_message) on failure.
    """
    names, err = fetch_food_names_from_wiki()
    if err:
        return [], [], err
    feast_names, feast_err = fetch_feast_names_from_wiki()
    if feast_err:
        return names, [], None  # still return food names if feast fetch fails
    return names, feast_names, None


def load_state() -> dict[str, Any]:
    """
    Load persisted state from state.json in the app directory.
    Returns a dict with defaults for missing keys.
    """
    path = state_path()
    defaults: dict[str, Any] = {
        "api_base_url": DEFAULT_API_BASE,
        "lang": "en",
        "window_geometry": None,
        "price_type": "sells",  # "sells" = instant buy, "buys" = buy order
    }
    if not path.exists():
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            defaults.update(data)
        return defaults
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load state from %s: %s", path, e)
        return defaults


def save_state(state: dict[str, Any]) -> None:
    """Persist state to state.json in the app directory."""
    path = state_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.warning("Could not save state to %s: %s", path, e)


class GW2APIError(Exception):
    """Raised when a GW2 API request fails."""

    pass


class GW2Client:
    """
    Client for the Guild Wars 2 REST API.
    Used to fetch items and trading post prices.
    """

    def __init__(self, base_url: str = DEFAULT_API_BASE, lang: str = "en", timeout: float = 45.0):
        self.base_url = base_url.rstrip("/")
        self.lang = lang
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.setdefault("Accept", "application/json")

    def _url(self, path: str, **params: str) -> str:
        """Build full URL with optional query parameters."""
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode(params)
        return url

    def _get(self, path: str, **params: str) -> Any:
        """GET a JSON endpoint; respect 429 rate limit and retry on timeout. Raise GW2APIError on failure."""
        params.setdefault("lang", self.lang)
        url = self._url(path, **params)
        last_exc: Optional[Exception] = None
        # Retry loop: handle 429 (rate limit) and timeout/connection errors
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self._session.get(url, timeout=self.timeout)
                if r.status_code == 429:
                    # Rate limited: wait Retry-After if present, else use default
                    wait = RATE_LIMIT_WAIT_SECONDS
                    retry_after = r.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            wait = int(retry_after)
                        except ValueError:
                            pass
                    if attempt < MAX_RETRIES:
                        logger.info("API rate limited (429), waiting %s seconds before retry %s/%s", wait, attempt + 1, MAX_RETRIES)
                        time.sleep(w)
                        continue
                    raise GW2APIError(
                        f"Rate limited by API (429). Waited and retried {MAX_RETRIES} times. Try again later."
                    ) from None
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    backoff = BACKOFF_INITIAL_SECONDS * (2 ** attempt)
                    logger.warning("API request timed out, retrying in %s s (attempt %s/%s)", backoff, attempt + 1, MAX_RETRIES)
                    time.sleep(backoff)
                    continue
            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    backoff = BACKOFF_INITIAL_SECONDS * (2 ** attempt)
                    logger.warning("API connection error, retrying in %s s (attempt %s/%s): %s", backoff, attempt + 1, MAX_RETRIES, e)
                    time.sleep(backoff)
                    continue
            except requests.RequestException as e:
                raise GW2APIError(f"Request failed: {e}") from e
            except ValueError as e:
                raise GW2APIError(f"Invalid JSON: {e}") from e
        if last_exc is not None:
            raise GW2APIError(f"Request failed: {last_exc}") from last_exc
        raise GW2APIError("Request failed after retries") from last_exc

    def get_item_ids(self) -> list[int]:
        """Return all item IDs from /v2/items."""
        time.sleep(REQUEST_DELAY_SECONDS)
        data = self._get("/v2/items")
        if not isinstance(data, list):
            raise GW2APIError("Unexpected response from /v2/items")
        return [int(x) for x in data]

    def get_items(self, ids: list[int], use_cache: bool = True) -> list[dict[str, Any]]:
        """Fetch item details for the given IDs (batched). Uses file cache when use_cache=True."""
        if not ids:
            return []
        result: list[dict[str, Any]] = []
        cache_root = _cache_dir(self.base_url, self.lang)
        for i in range(0, len(ids), ITEMS_PAGE_SIZE):
            chunk = ids[i : i + ITEMS_PAGE_SIZE]
            if use_cache:
                path = cache_root / f"items_{_batch_hash(chunk)}.json"
                data = _cache_get(path)
            else:
                data = None
            if data is None:
                time.sleep(REQUEST_DELAY_SECONDS)
                ids_param = ",".join(str(x) for x in chunk)
                data = self._get("/v2/items", ids=ids_param)
                if use_cache:
                    _cache_set(path, data)
            if isinstance(data, list):
                result.extend(data)
            else:
                raise GW2APIError("Unexpected response from /v2/items?ids=...")
        return result

    def get_commerce_prices(self, ids: list[int], use_cache: bool = True) -> list[dict[str, Any]]:
        """Fetch trading post prices for the given item IDs (batched). Uses file cache when use_cache=True."""
        if not ids:
            return []
        result: list[dict[str, Any]] = []
        cache_root = _cache_dir(self.base_url, None)  # prices are lang-independent
        for i in range(0, len(ids), ITEMS_PAGE_SIZE):
            chunk = ids[i : i + ITEMS_PAGE_SIZE]
            if use_cache:
                path = cache_root / f"prices_{_batch_hash(chunk)}.json"
                data = _cache_get(path)
            else:
                data = None
            if data is None:
                time.sleep(REQUEST_DELAY_SECONDS)
                ids_param = ",".join(str(x) for x in chunk)
                data = self._get("/v2/commerce/prices", ids=ids_param)
                if use_cache:
                    _cache_set(path, data)
            if isinstance(data, list):
                result.extend(data)
            else:
                raise GW2APIError("Unexpected response from /v2/commerce/prices?ids=...")
        return result

    def get_recipe_ids(self, use_cache: bool = True) -> list[int]:
        """Return all recipe IDs from /v2/recipes. Uses file cache when use_cache=True."""
        cache_root = _cache_dir(self.base_url, None)
        path = cache_root / "recipe_ids.json"
        if use_cache:
            data = _cache_get(path)
        else:
            data = None
        if data is None:
            time.sleep(REQUEST_DELAY_SECONDS)
            data = self._get("/v2/recipes")
            if use_cache:
                _cache_set(path, data)
        if not isinstance(data, list):
            raise GW2APIError("Unexpected response from /v2/recipes")
        return [int(x) for x in data]

    def get_recipes(self, ids: list[int], use_cache: bool = True) -> list[dict[str, Any]]:
        """Fetch recipe details for the given IDs (batched). Uses file cache when use_cache=True."""
        if not ids:
            return []
        result: list[dict[str, Any]] = []
        cache_root = _cache_dir(self.base_url, self.lang)
        for i in range(0, len(ids), ITEMS_PAGE_SIZE):
            chunk = ids[i : i + ITEMS_PAGE_SIZE]
            if use_cache:
                path = cache_root / f"recipes_{_batch_hash(chunk)}.json"
                data = _cache_get(path)
            else:
                data = None
            if data is None:
                time.sleep(REQUEST_DELAY_SECONDS)
                ids_param = ",".join(str(x) for x in chunk)
                data = self._get("/v2/recipes", ids=ids_param)
                if use_cache:
                    _cache_set(path, data)
            if isinstance(data, list):
                result.extend(data)
            else:
                raise GW2APIError("Unexpected response from /v2/recipes?ids=...")
        return result


# Extract types for Exquisite Extract recipe: 5 Fine + 5 Masterwork + 5 Rare + 10 Exotic.
# Chef recipe min_rating → extract type. We use wiki 100% ranges only (no overlap).
# Fine 0–50, Masterwork 150–225, Rare 300–375, Exotic 400–450. Ascended (500) excluded.
EXTRACT_RANGES: list[tuple[str, int, int]] = [
    ("Fine", 0, 50),          # 100% fine per wiki
    ("Masterwork", 150, 225), # 100% masterwork per wiki
    ("Rare", 300, 375),       # 100% rare per wiki
    ("Exotic", 400, 450),     # 100% exotic per wiki
]

# Extracts per item when composted: feasts give 10, regular food gives 1.
EXTRACTS_PER_FEAST = 10
EXTRACTS_PER_FOOD = 1

# Placeholder id for missing extract types (no tradeable food on TP for that tier).
# Use negative ids so placeholders have unique row ids in the UI.
PLACEHOLDER_ITEM_ID = 0
_PLACEHOLDER_IDS: dict[str, int] = {"Fine": -1, "Masterwork": -2, "Rare": -3, "Exotic": -4}


def _placeholder_row(extract_type: str) -> dict[str, Any]:
    """Build a result row when no tradeable food exists for this extract type (e.g. Rare)."""
    return {
        "id": _PLACEHOLDER_IDS.get(extract_type, PLACEHOLDER_ITEM_ID),
        "name": "No tradeable option (see wiki)",
        "extract_type": extract_type,
        "rarity": "",
        "extracts_per_compost": 1,
        "sell_price": 0,
        "buy_price": 0,
        "sell_price_per_extract": 0,
        "buy_price_per_extract": 0,
        "price_display": "—",
        "buy_price_display": "—",
        "price_per_extract_display": "—",
        "buy_price_per_extract_display": "—",
    }


def get_extracts_per_compost(item: dict[str, Any]) -> int:
    """
    Return how many extracts (of the same type) one gets when composting this item.
    Feasts (apply_count 10) give 10; regular food gives 1.
    """
    details = item.get("details") or {}
    apply_count = details.get("apply_count", EXTRACTS_PER_FOOD)
    return int(apply_count) if apply_count else EXTRACTS_PER_FOOD


def _recipe_min_rating_to_extract_type(min_rating: int) -> Optional[str]:
    """Map Chef recipe min_rating to a single extract type (first matching range). Used for backward compatibility."""
    for name, low, high in EXTRACT_RANGES:
        if low <= min_rating <= high:
            return name
    return None


def _recipe_min_rating_to_extract_types(min_rating: int) -> list[str]:
    """Map Chef recipe min_rating to matching extract type(s). Uses wiki 100% ranges (no overlap)."""
    return [name for name, low, high in EXTRACT_RANGES if low <= min_rating <= high]


def _is_compostable_food_item(item: dict[str, Any]) -> bool:
    """True if item is tradeable chef-crafted consumable food (can be composted with Portable Composter)."""
    if item.get("type") != "Consumable":
        return False
    details = item.get("details") or {}
    if details.get("type") != "Food":
        return False
    flags = item.get("flags") or []
    # AccountBound: cannot be listed on Trading Post. NoSell only means no vendor sale; TP listing is still allowed.
    if "AccountBound" in flags:
        return False
    return True


def copper_to_gold(copper: int) -> str:
    """Format copper as 'Xg Ys Zc' (gold/silver/copper)."""
    if copper < 0:
        return "—"
    g = copper // COPPER_PER_GOLD
    r = copper % COPPER_PER_GOLD
    s = r // COPPER_PER_SILVER
    c = r % COPPER_PER_SILVER
    parts = []
    if g:
        parts.append(f"{g}g")
    if s or parts:
        parts.append(f"{s}s")
    parts.append(f"{c}c")
    return " ".join(parts)


def _price_display(copper: int) -> str:
    """Format price for display; use '—' when 0 (no listing) instead of '0c'."""
    if copper <= 0:
        return "—"
    return copper_to_gold(copper)


def run_analysis(
    api_base_url: str,
    lang: str,
    price_type: str = "sells",
    *,
    progress_callback: Optional[
        Callable[[str, str, Optional[int], Optional[int]], None]
    ] = None,
    on_items_ready: Optional[Callable[[list[dict[str, Any]]], None]] = None,
    on_prices_batch: Optional[Callable[[list[dict[str, Any]]], None]] = None,
    stop_requested: Optional[Callable[[], bool]] = None,
    stopped_flag: Optional[list[bool]] = None,
) -> list[dict[str, Any]]:
    """
    Find the cheapest food per extract type (Fine, Masterwork, Rare, Exotic) to
    compost for the Exquisite Extract recipe. Uses Chef recipes and 100% salvage
    ranges; ascended food (rating 500) is never included.

    Returns one row per extract type (the cheapest tradeable food for that type).
    If stop_requested returns True and execution is aborted, sets stopped_flag[0]=True (caller provides a list like [False]) and returns []. Optional callbacks: progress_callback(phase, message, current, total);
    on_items_ready(items); on_prices_batch(updates).
    """
    def stopped() -> bool:
        return bool(stop_requested and stop_requested())

    def progress(phase: str, message: str, current: Optional[int] = None, total: Optional[int] = None) -> None:
        if progress_callback:
            progress_callback(phase, message, current, total)

    client = GW2Client(base_url=api_base_url, lang=lang)

    # 1) Fetch all recipe IDs
    progress("ids", "Fetching recipe list…", 1, 1)
    recipe_ids = client.get_recipe_ids()
    if stopped():
        if stopped_flag is not None:
            stopped_flag[0] = True
        progress("done", "Stopped.", 1, 1)
        return []
    if not recipe_ids:
        progress("done", "No recipes found.", 1, 1)
        return []

    # 2) Fetch recipes in batches; keep Chef food recipes whose min_rating maps to an extract type.
    # Same item can be produced by multiple recipes (e.g. 300 Rare and 400 Exotic); keep (item_id, extract_type) pairs.
    item_extract_pairs: list[tuple[int, str]] = []
    seen_pairs: set[tuple[int, str]] = set()
    total_recipe_batches = (len(recipe_ids) + ITEMS_PAGE_SIZE - 1) // ITEMS_PAGE_SIZE
    total_item_batches = 0  # set after we know unique_item_ids
    for i in range(0, len(recipe_ids), ITEMS_PAGE_SIZE):
        if stopped():
            if stopped_flag is not None:
                stopped_flag[0] = True
            progress("done", "Stopped.", 1, 1)
            return []
        chunk = recipe_ids[i : i + ITEMS_PAGE_SIZE]
        batch_num = (i // ITEMS_PAGE_SIZE) + 1
        # Progress: (current, total) with total=total_recipe_batches here; item loop will use total_detail_batches
        progress("details", f"Fetching recipes (batch {batch_num}/{total_recipe_batches})…", batch_num, total_recipe_batches)
        recipes = client.get_recipes(chunk)
        for rec in recipes:
            disciplines = rec.get("disciplines") or []
            if "Chef" not in disciplines:
                continue
            min_rating = rec.get("min_rating")
            if min_rating is None:
                continue
            extract_types = _recipe_min_rating_to_extract_types(int(min_rating))
            if not extract_types:
                continue
            oid = rec.get("output_item_id")
            if oid is not None:
                for extract_type in extract_types:
                    pair = (int(oid), extract_type)
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        item_extract_pairs.append(pair)

    if not item_extract_pairs:
        progress("done", "No Chef food recipes found in extract ranges.", 1, 1)
        return []

    unique_item_ids = list({iid for iid, _ in item_extract_pairs})
    total_item_batches = (len(unique_item_ids) + ITEMS_PAGE_SIZE - 1) // ITEMS_PAGE_SIZE
    total_detail_batches = total_recipe_batches + total_item_batches
    progress("filter", f"Found {len(unique_item_ids)} food recipe(s). Fetching item details…", 1, 1)

    # 3) Fetch item details; keep only tradeable consumable food
    items_by_id: dict[int, dict[str, Any]] = {}
    for i in range(0, len(unique_item_ids), ITEMS_PAGE_SIZE):
        if stopped():
            if stopped_flag is not None:
                stopped_flag[0] = True
            progress("done", "Stopped.", 1, 1)
            return []
        chunk = unique_item_ids[i : i + ITEMS_PAGE_SIZE]
        batch_num = (i // ITEMS_PAGE_SIZE) + 1
        detail_index = total_recipe_batches + batch_num  # 1-based index into combined detail batches
        progress("details", f"Fetching item details (batch {batch_num}/{total_item_batches})…", detail_index, total_detail_batches)
        for it in client.get_items(chunk):
            iid = it.get("id")
            if iid is None:
                continue
            if not _is_compostable_food_item(it):
                continue
            items_by_id[int(iid)] = it

    # Build list of (item, extract_type) for items we have
    food_items: list[tuple[dict[str, Any], str]] = []
    for iid, extract_type in item_extract_pairs:
        it = items_by_id.get(iid)
        if it is not None:
            food_items.append((it, extract_type))

    # When food list is populated, only consider items in that list (by name)
    allowed_names = load_food_list()
    if allowed_names:
        allowed_set = {n.strip().lower() for n in allowed_names if n.strip()}
        food_items = [(it, et) for it, et in food_items if (it.get("name") or "").strip().lower() in allowed_set]
        progress("filter", f"Filtered to {len(food_items)} food(s) in list. Fetching prices…", 1, 1)
    else:
        progress("filter", f"Found {len(food_items)} tradeable food item(s). Fetching prices…", 1, 1)

    if not food_items:
        progress("done", "No tradeable compostable food found.", 1, 1)
        return []

    food_item_list = [it for it, _ in food_items]
    if on_items_ready:
        ready = []
        for it, extract_type in food_items:
            ready.append({
                "id": it["id"],
                "name": it.get("name", "Unknown"),
                "extract_type": extract_type,
                "rarity": it.get("rarity", ""),
                "extracts_per_compost": get_extracts_per_compost(it),
                "price_display": "…",
                "buy_price_display": "…",
                "price_per_extract_display": "…",
                "buy_price_per_extract_display": "…",
            })
        on_items_ready(ready)

    food_ids = [it["id"] for it in food_item_list]
    price_by_id: dict[int, dict[str, Any]] = {}
    total_price_batches = (len(food_ids) + ITEMS_PAGE_SIZE - 1) // ITEMS_PAGE_SIZE

    try:
        for i in range(0, len(food_ids), ITEMS_PAGE_SIZE):
            if stopped():
                if stopped_flag is not None:
                    stopped_flag[0] = True
                progress("done", "Stopped.", 1, 1)
                return []
            chunk = food_ids[i : i + ITEMS_PAGE_SIZE]
            batch_num = (i // ITEMS_PAGE_SIZE) + 1
            progress("prices", f"Fetching prices (batch {batch_num}/{total_price_batches})…", batch_num, total_price_batches)
            prices = client.get_commerce_prices(chunk)
            for p in prices:
                pid = p.get("id")
                if pid is not None:
                    price_by_id[int(pid)] = p
            if on_prices_batch:
                updates = []
                for p in prices:
                    pid = p.get("id")
                    if pid is None:
                        continue
                    it = items_by_id.get(int(pid))
                    extracts = get_extracts_per_compost(it) if it else 1
                    sells = p.get("sells") or {}
                    buys = p.get("buys") or {}
                    sell_price = int((sells.get("unit_price") or 0))
                    buy_price = int((buys.get("unit_price") or 0))
                    sell_per = sell_price // extracts if extracts else 0
                    buy_per = buy_price // extracts if extracts else 0
                    updates.append({
                        "id": int(pid),
                        "price_display": _price_display(sell_price),
                        "buy_price_display": _price_display(buy_price),
                        "price_per_extract_display": _price_display(sell_per),
                        "buy_price_per_extract_display": _price_display(buy_per),
                    })
                if updates:
                    on_prices_batch(updates)
    except GW2APIError:
        progress("prices", "Fetching prices (fallback)…", None, None)
        for idx, iid in enumerate(food_ids):
            if stopped():
                if stopped_flag is not None:
                    stopped_flag[0] = True
                progress("done", "Stopped.", 1, 1)
                return []
            if progress_callback and idx % 10 == 0:
                progress("prices", f"Fetching prices ({idx + 1}/{len(food_ids)})…", idx + 1, len(food_ids))
            try:
                time.sleep(REQUEST_DELAY_SECONDS)
                single = client._get("/v2/commerce/prices/" + str(iid))
                if isinstance(single, dict):
                    price_by_id[int(iid)] = single
            except GW2APIError:
                pass
        if on_prices_batch:
            updates = []
            for iid in food_ids:
                p = price_by_id.get(iid)
                it = items_by_id.get(iid)
                extracts = get_extracts_per_compost(it) if it else 1
                sells = (p or {}).get("sells") or {}
                buys = (p or {}).get("buys") or {}
                sell_price = int((sells.get("unit_price") or 0))
                buy_price = int((buys.get("unit_price") or 0))
                sell_per = sell_price // extracts if extracts else 0
                buy_per = buy_price // extracts if extracts else 0
                updates.append({
                    "id": int(iid),
                    "price_display": _price_display(sell_price),
                    "buy_price_display": _price_display(buy_price),
                    "price_per_extract_display": _price_display(sell_per),
                    "buy_price_per_extract_display": _price_display(buy_per),
                })
            if updates:
                on_prices_batch(updates)

    # Build full results for every food item
    all_results: list[dict[str, Any]] = []
    for it, extract_type in food_items:
        iid = it["id"]
        p = price_by_id.get(iid)
        sells = (p or {}).get("sells") or {}
        buys = (p or {}).get("buys") or {}
        sell_price = int((sells.get("unit_price") or 0))
        buy_price = int((buys.get("unit_price") or 0))
        extracts = get_extracts_per_compost(it)
        sell_per_extract = sell_price // extracts if extracts else 0
        buy_per_extract = buy_price // extracts if extracts else 0
        all_results.append({
            "id": iid,
            "name": it.get("name", "Unknown"),
            "extract_type": extract_type,
            "rarity": it.get("rarity", ""),
            "extracts_per_compost": extracts,
            "sell_price": sell_price,
            "buy_price": buy_price,
            "sell_price_per_extract": sell_per_extract,
            "buy_price_per_extract": buy_per_extract,
            "price_display": _price_display(sell_price),
            "buy_price_display": _price_display(buy_price),
            "price_per_extract_display": _price_display(sell_per_extract),
            "buy_price_per_extract_display": _price_display(buy_per_extract),
        })

    # For each extract type, keep only the cheapest item (by selected price per extract).
    # Treat price 0 (no listing) as "no data" — never pick it over a positive price.
    key = "sell_price_per_extract" if price_type == "sells" else "buy_price_per_extract"
    by_type: dict[str, dict[str, Any]] = {}
    for r in all_results:
        et = r["extract_type"]
        r_val = r.get(key) or 0
        current = by_type.get(et)
        c_val = (current.get(key) or 0) if current else 0
        if current is None:
            by_type[et] = r
        elif r_val > 0 and (c_val <= 0 or r_val < c_val):
            by_type[et] = r

    # Return one row per extract type in fixed order (Fine, Masterwork, Rare, Exotic).
    # If no tradeable food exists for a type (e.g. Rare: many are NoSell/karma-only), show a placeholder
    # so the user sees all four tiers and can check the wiki for crafting/karma options.
    results: list[dict[str, Any]] = []
    for et, _, _ in EXTRACT_RANGES:
        if et in by_type:
            results.append(by_type[et])
        else:
            results.append(_placeholder_row(et))
    num_with_prices = len([r for r in results if r["id"] > 0])
    progress("done", f"Done. Found cheapest for {num_with_prices} tradeable extract type(s).", 1, 1)
    return results
