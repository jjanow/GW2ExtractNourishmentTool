# GW2 Extract Nourishment Tool

This tool finds the **cheapest chef-crafted food per extract type** (Fine, Masterwork, Rare, Exotic) to compost in the **Portable Composter**, by querying the [Guild Wars 2 API](https://wiki.guildwars2.com/wiki/API:2).

Composting food in the Portable Composter yields extracts by recipe rating; these are combined into **Exquisite Extract of Nourishment** (5 Fine + 5 Masterwork + 5 Rare + 10 Exotic), used to craft **Pile of Enriched Compost** and varietal seed pouches (e.g. Varietal Cilantro Seed Pouch). The app lists tradeable consumable food by extract tier and Trading Post price so you can choose the most cost-effective option for each tier.

## Features

- **Cross-platform GUI** (Windows, macOS, Linux) using Python and tkinter
- **Configurable API base URL**
- **Extract tiers**: Fine, Masterwork, Rare, Exotic (ascended excluded); sorted by instant buy (sell listing) price
- **Food list filter**: optional `food.json` list of item names; use **Populate food list from Wiki** to fetch and save names so the tool only considers those items
- **State persistence**: settings and window geometry saved in `state.json` in the app directory
- **API cache**: responses cached under `.cache/gw2/` with optional purge from the UI

## Requirements

- **Python 3.8+**
- Dependencies: see `requirements.txt` (e.g. `requests`)

## Setup

### 1. Clone or download the project

```bash
cd /path/to/GW2ExtractNourishmentTool
```

### 2. Create and activate a virtual environment (recommended)

**Linux / macOS:**

```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows (Command Prompt):**

```cmd
python -m venv venv
venv\Scripts\activate.bat
```

**Windows (PowerShell):**

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the application

```bash
python main.py
```

## Usage

1. **Settings**
   - **API base URL**: Leave as `https://api.guildwars2.com` unless you use a different endpoint.

2. (Optional) Click **Populate food list from Wiki** to fetch food item names and save them to `food.json`; **Fetch cheapest food** will then only consider those items. If `food.json` is empty or missing, all tradeable compostable food from recipes is considered.

3. Click **Fetch cheapest food** to load food per extract tier (Fine, Masterwork, Rare, Exotic) and their Trading Post prices from the API. The first run may take 1–2 minutes while recipe and item data is fetched. Results are sorted by instant buy price (cheapest first).

4. Use the listed food in the **Portable Composter** to obtain extracts, then combine them into Exquisite Extract of Nourishment for your seed pouch crafting.

## State and data files

- **`state.json`** (project root): API base URL, window geometry. The app overwrites it when you change settings or close the window. To reset, delete it and restart.
- **`food.json`** (project root): Optional list of food item names (`item_names`). If present and non-empty, only these items are considered when fetching cheapest food. Populate via **Populate food list from Wiki** or edit by hand.
- **`.cache/gw2/`**: Cached API responses; use **Purge cache** in the app to clear.

## Project layout

```
GW2ExtractNourishmentTool/
├── main.py           # GUI entry point (tkinter)
├── gw2_client.py     # API client, state load/save, food list, analysis logic
├── food.json         # Optional list of food item names (from Wiki or manual)
├── requirements.txt  # Python dependencies
├── state.json        # Created at runtime; stores settings (optional in git)
├── .cache/gw2/       # API cache (created at runtime)
└── README.md         # This file
```

## API usage

The tool uses public GW2 API endpoints; no API key is required:

- `GET /v2/recipes` – recipe list and details (to find food by extract tier via min_rating)
- `GET /v2/items?ids=...` – item details (type, name, rarity, consumable)
- `GET /v2/commerce/prices?ids=...` – Trading Post buy/sell prices

Only **Consumable** food items from recipes with the correct **min_rating** ranges (Fine, Masterwork, Rare, Exotic) are considered; ascended (500) is excluded. Account-bound and no-sell items are excluded so only tradeable options are shown.

## License

Use and modify as you like. The Guild Wars 2 API is provided by ArenaNet; respect their [terms and best practices](https://wiki.guildwars2.com/wiki/API:Best_practices).
