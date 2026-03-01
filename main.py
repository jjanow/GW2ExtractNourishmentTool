#!/usr/bin/env python3
"""
GW2 Extract Nourishment Tool — GUI entry point.

Finds the cheapest food per extract type (Fine, Masterwork, Rare, Exotic) to
compost with the Portable Composter for the Exquisite Extract of Nourishment
recipe (5 Fine + 5 Masterwork + 5 Rare + 10 Exotic). Ascended food is not used.
State is stored in state.json in the application directory.
"""

from __future__ import annotations

import logging
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Callable, Optional

from gw2_client import (
    load_state,
    save_state,
    purge_cache,
    run_analysis,
    load_food_list,
    save_food_list,
    fetch_food_names_from_wiki,
    GW2APIError,
)

# Configure logging to a simple format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class Application:
    """
    Main application window: settings panel, run button, and results table.
    All configurable parameters are persisted in state.json.
    """

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("GW2 Extract Nourishment Tool")
        self.root.minsize(520, 400)
        self.root.geometry("720x520")

        self._state = load_state()
        self._apply_saved_geometry()
        self._build_ui()
        self._populate_from_state()
        self._bind_save_on_close()

    def _apply_saved_geometry(self) -> None:
        """Restore window size and position from state if present."""
        geom = self._state.get("window_geometry")
        if isinstance(geom, str) and geom:
            try:
                self.root.geometry(geom)
            except Exception:
                pass

    def _apply_theme(self) -> None:
        """Apply a dark theme (clam allows full control on Linux/Win)."""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # Dark palette: dark surfaces, light text, clear hierarchy
        bg = "#1e1e24"
        surface = "#2d2d33"
        surface_alt = "#25252b"
        fg = "#e4e4e7"
        fg_muted = "#9ca3af"
        border = "#3f3f46"
        accent = "#3b82f6"
        accent_fg = "#ffffff"
        style.configure(".", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("TFrame", background=bg)
        style.configure(
            "TLabelframe",
            background=surface,
            foreground=fg,
            bordercolor=border,
            relief="solid",
        )
        style.configure("TLabelframe.Label", background=surface, foreground=fg, font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure(
            "TButton",
            background=border,
            foreground=fg,
            padding=(12, 6),
            font=("Segoe UI", 10),
        )
        style.map("TButton", background=[("active", "#52525b"), ("pressed", "#71717a")])
        # Primary action: Fetch (blue)
        style.configure(
            "Primary.TButton",
            background=accent,
            foreground=accent_fg,
            padding=(14, 8),
            font=("Segoe UI", 10, "bold"),
        )
        style.map("Primary.TButton", background=[("active", "#2563eb"), ("pressed", "#1d4ed8")])
        # Stop (red)
        style.configure(
            "Stop.TButton",
            background="#dc2626",
            foreground=accent_fg,
            padding=(12, 6),
            font=("Segoe UI", 10),
        )
        style.map("Stop.TButton", background=[("active", "#b91c1c"), ("pressed", "#991b1b")])
        # Purge cache (amber)
        style.configure(
            "Purge.TButton",
            background="#d97706",
            foreground=accent_fg,
            padding=(12, 6),
            font=("Segoe UI", 10),
        )
        style.map("Purge.TButton", background=[("active", "#b45309"), ("pressed", "#92400e")])
        # Populate food list (teal/green)
        style.configure(
            "Populate.TButton",
            background="#0d9488",
            foreground=accent_fg,
            padding=(12, 6),
            font=("Segoe UI", 10),
        )
        style.map("Populate.TButton", background=[("active", "#0f766e"), ("pressed", "#115e59")])
        # Recipe & help (violet)
        style.configure(
            "Recipe.TButton",
            background="#7c3aed",
            foreground=accent_fg,
            padding=(12, 6),
            font=("Segoe UI", 10),
        )
        style.map("Recipe.TButton", background=[("active", "#6d28d9"), ("pressed", "#5b21b6")])
        style.configure(
            "TEntry",
            fieldbackground=surface,
            foreground=fg,
            insertcolor=fg,
            padding=6,
        )
        # Alternating row colors: soft blue-gray (even) / darker blue-gray (odd)
        tree_even = "#2d3138"
        tree_odd = "#272b32"
        style.configure(
            "Treeview",
            background=tree_even,
            foreground=fg,
            fieldbackground=tree_even,
            rowheight=32,
            font=("Segoe UI", 10),
            borderwidth=0,
        )
        style.configure("Treeview.Heading", background=surface_alt, foreground=fg, font=("Segoe UI", 10, "bold"), padding=(8, 6))
        # Store for tag_configure (Treeview tags don't use ttk style)
        self._tree_even_bg = tree_even
        self._tree_odd_bg = tree_odd
        style.map("Treeview", background=[("selected", accent)], foreground=[("selected", accent_fg)])
        style.map("Treeview.Heading", background=[("active", "#3f3f46")])
        style.configure("Vertical.TScrollbar", background=border, troughcolor=bg, borderwidth=0)
        style.configure("Horizontal.TScrollbar", background=border, troughcolor=bg, borderwidth=0)
        style.configure("TProgressbar", background=accent, troughcolor=border, thickness=8)
        style.configure(
            "Muted.TLabel",
            background=surface,
            foreground=fg_muted,
            font=("Segoe UI", 9),
        )
        self.root.configure(bg=bg)

    def _build_ui(self) -> None:
        """Build the main layout: settings frame, run button, results area."""
        self._apply_theme()
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        # --- Settings ---
        settings = ttk.LabelFrame(main, text="Settings", padding=12)
        settings.pack(fill=tk.X, pady=(0, 12))

        grid = dict(row=0, column=0, sticky=tk.W, padx=(0, 12), pady=4)

        ttk.Label(settings, text="API base URL:").grid(**grid)
        self.api_url_var = tk.StringVar(value=self._state.get("api_base_url", ""))
        self.api_url_entry = ttk.Entry(settings, textvariable=self.api_url_var, width=42)
        self.api_url_entry.grid(row=0, column=1, sticky=tk.EW, padx=(0, 8), pady=2)

        settings.columnconfigure(1, weight=1)

        # --- Run + progress ---
        run_frame = ttk.Frame(main)
        run_frame.pack(fill=tk.X, pady=(0, 12))
        self.run_btn = ttk.Button(
            run_frame, text="Fetch cheapest food", command=self._on_run, style="Primary.TButton"
        )
        self.run_btn.pack(side=tk.LEFT, padx=(0, 10))
        self.stop_btn = ttk.Button(run_frame, text="Stop", command=self._on_stop, state=["disabled"], style="Stop.TButton")
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(run_frame, text="Purge cache", command=self._on_purge_cache, style="Purge.TButton").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(run_frame, text="Populate food list from Wiki", command=self._on_populate_food_list, style="Populate.TButton").pack(
            side=tk.LEFT, padx=(0, 10)
        )
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(run_frame, textvariable=self.status_var).pack(side=tk.LEFT, padx=(16, 0))
        self.progress = ttk.Progressbar(run_frame, mode="determinate", maximum=100, value=0, length=220)
        self.progress.pack(side=tk.LEFT)
        self.progress.pack_forget()  # Hidden until fetch starts
        self._progress_visible = False
        self._stop_requested = threading.Event()
        self._details_pct = 0.0  # so progress bar never goes backwards during details phase

        # --- Results ---
        results_lf = ttk.LabelFrame(
            main,
            text="Cheapest food per extract type (for Exquisite Extract recipe)",
            padding=12,
        )
        results_lf.pack(fill=tk.BOTH, expand=True)

        # Treeview with scrollbars
        tree_frame = ttk.Frame(results_lf)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("extract_type", "name", "extracts", "instant_buy", "per_ext_sell", "buy_order", "per_ext_buy", "rarity"),
            show="headings",
            height=12,
            selectmode="browse",
        )
        self._tree_id_to_iid: dict[int, list[str]] = {}  # id -> list of tree iids (same id can appear in multiple rows)
        self._current_results: list[dict[str, Any]] = []  # full row dicts for sort-by-column
        self._sort_column: Optional[str] = None
        self._sort_reverse: bool = False
        # Column heading click sorts by that column (toggle reverse on same column)
        _columns = (
            ("extract_type", "Extract type"),
            ("name", "Item name"),
            ("extracts", "Extracts"),
            ("instant_buy", "Instant buy"),
            ("per_ext_sell", "Per ext. (sell)"),
            ("buy_order", "Buy order"),
            ("per_ext_buy", "Per ext. (order)"),
            ("rarity", "Item rarity"),
        )
        for col_id, col_text in _columns:
            self.tree.heading(
                col_id,
                text=col_text,
                command=lambda c=col_id: self._sort_by_column(c),
            )
        self.tree.column("extract_type", width=90, minwidth=70)
        self.tree.column("name", width=260, minwidth=120)
        self.tree.column("extracts", width=56, minwidth=50)
        self.tree.column("instant_buy", width=82, minwidth=70)
        self.tree.column("per_ext_sell", width=82, minwidth=70)
        self.tree.column("buy_order", width=82, minwidth=70)
        self.tree.column("per_ext_buy", width=82, minwidth=70)
        self.tree.column("rarity", width=72, minwidth=60)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("odd", background=self._tree_odd_bg)

        # Recipe / help in separate window
        recipe_btn_frame = ttk.Frame(results_lf)
        recipe_btn_frame.pack(anchor=tk.W, pady=(10, 0))
        ttk.Button(
            recipe_btn_frame,
            text="View recipe & help",
            command=self._show_recipe_window,
            style="Recipe.TButton",
        ).pack(side=tk.LEFT)

    def _populate_from_state(self) -> None:
        """Set widget values from loaded state and restore last results table if applicable."""
        self.api_url_var.set(self._state.get("api_base_url", ""))

        # Restore last analysis results if they match current settings (always sorted by instant buy)
        last = self._state.get("last_results")
        last_api = self._state.get("last_results_api_base_url", "")
        last_lang = self._state.get("last_results_lang", "")
        api = self._state.get("api_base_url", "").strip() or "https://api.guildwars2.com"
        lang = "en"
        if (
            isinstance(last, list)
            and len(last) > 0
            and last_api == api
            and last_lang == lang
        ):
            self._current_results = [r for r in last if isinstance(r, dict)]
            for idx, r in enumerate(self._current_results):
                iid = f"row_{idx}"
                row_tag = "even" if idx % 2 == 0 else "odd"
                self.tree.insert("", tk.END, iid=iid, values=(
                    r.get("extract_type", "—"),
                    r.get("name", ""),
                    r.get("extracts_per_compost", "—"),
                    r.get("price_display", ""),
                    r.get("price_per_extract_display", ""),
                    r.get("buy_price_display", ""),
                    r.get("buy_price_per_extract_display", ""),
                    r.get("rarity", ""),
                ), tags=(row_tag,))
                rid = r.get("id")
                if rid not in self._tree_id_to_iid:
                    self._tree_id_to_iid[rid] = []
                self._tree_id_to_iid[rid].append(iid)

    def _collect_state(self) -> dict[str, Any]:
        """Build current state dict from UI."""
        return {
            "api_base_url": self.api_url_var.get().strip() or "https://api.guildwars2.com",
            "window_geometry": self.root.geometry(),
        }

    def _save_state(self) -> None:
        """Persist current state to state.json."""
        self._state.update(self._collect_state())
        save_state(self._state)

    def _bind_save_on_close(self) -> None:
        """Save state when window is closed."""

        def on_closing() -> None:
            self._save_state()
            self.root.destroy()

        self.root.protocol("WM_DELETE_WINDOW", on_closing)

    _RECIPE_HELP_TEXT = (
        "Recipe: 5 Fine + 5 Masterwork + 5 Rare + 10 Exotic → 1 Exquisite Extract. "
        "Salvage the listed foods with the Portable Composter. Feasts give 10 extracts per item.\n\n"
        "\"Extract type\" = type of extract you get when composting; \"Item rarity\" = the item's in-game rarity "
        "(they can differ: e.g. a Masterwork item can yield Rare or Exotic extract).\n\n"
        "Recipe rating ranges (Chef min_rating, 100% chance per wiki):\n"
        "  Fine: 0–50  |  Masterwork: 150–225  |  Rare: 300–375  |  Exotic: 400–450. "
        "Ascended (500) not used.\n\n"
        "If \"No tradeable option\", see wiki (Portable Composter/Guide)."
    )

    def _show_recipe_window(self) -> None:
        """Open a separate window with the recipe and help text (readable, wrapped)."""
        win = tk.Toplevel(self.root)
        win.title("Recipe & help")
        win.minsize(420, 420)
        win.geometry("560x320")
        win.transient(self.root)
        win.configure(bg="#1e1e24")

        main = ttk.Frame(win, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        text = tk.Text(
            main,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            padx=8,
            pady=8,
            state=tk.DISABLED,
            cursor="arrow",
            bg="#2d2d33",
            fg="#e4e4e7",
            relief=tk.FLAT,
        )
        text.pack(fill=tk.BOTH, expand=True)
        text.configure(state=tk.NORMAL)
        text.insert(tk.END, self._RECIPE_HELP_TEXT)
        text.configure(state=tk.DISABLED)

    # Column id -> (result dict key, numeric). For extract_type we use logical order.
    _EXTRACT_TYPE_ORDER = ("Fine", "Masterwork", "Rare", "Exotic")
    _SORT_KEYS = {
        "extract_type": ("extract_type", False),  # string, special-cased by order
        "name": ("name", False),
        "extracts": ("extracts_per_compost", True),
        "instant_buy": ("sell_price", True),
        "per_ext_sell": ("sell_price_per_extract", True),
        "buy_order": ("buy_price", True),
        "per_ext_buy": ("buy_price_per_extract", True),
        "rarity": ("rarity", False),
    }

    def _sort_by_column(self, column_id: str) -> None:
        """Sort table by the given column; toggle reverse if same column clicked again."""
        if not self._current_results:
            return
        if self._sort_column == column_id:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column_id
            self._sort_reverse = False
        key_name, numeric = self._SORT_KEYS.get(column_id, (column_id, False))
        if column_id == "extract_type":
            order_map = {v: i for i, v in enumerate(self._EXTRACT_TYPE_ORDER)}
            def sort_key(r: dict) -> Any:
                v = r.get(key_name) or ""
                return (order_map.get(v, 99), v)
        elif numeric:
            def sort_key(r: dict) -> Any:
                v = r.get(key_name)
                if v is None:
                    return 0
                return v if isinstance(v, (int, float)) else 0
        else:
            def sort_key(r: dict) -> Any:
                v = r.get(key_name) or ""
                return (v.lower(), v)
        sorted_rows = sorted(self._current_results, key=sort_key, reverse=self._sort_reverse)
        self._current_results = sorted_rows
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._tree_id_to_iid.clear()
        for idx, r in enumerate(sorted_rows):
            iid = f"row_{idx}"
            row_tag = "even" if idx % 2 == 0 else "odd"
            self.tree.insert("", tk.END, iid=iid, values=(
                r.get("extract_type", "—"),
                r.get("name", ""),
                r.get("extracts_per_compost", "—"),
                r.get("price_display", ""),
                r.get("price_per_extract_display", ""),
                r.get("buy_price_display", ""),
                r.get("buy_price_per_extract_display", ""),
                r.get("rarity", ""),
            ), tags=(row_tag,))
            rid = r.get("id")
            if rid not in self._tree_id_to_iid:
                self._tree_id_to_iid[rid] = []
            self._tree_id_to_iid[rid].append(iid)

    def _show_progress(self, visible: bool, indeterminate: bool = False, value: int = 0, maximum: int = 100) -> None:
        """Show or hide the progress bar; optionally set determinate value or indeterminate mode."""
        if visible and not self._progress_visible:
            self.progress.pack(side=tk.LEFT, padx=(0, 8))
            self._progress_visible = True
        elif not visible and self._progress_visible:
            self.progress.pack_forget()
            self._progress_visible = False
        if visible:
            if indeterminate:
                self.progress.config(mode="indeterminate")
                self.progress.start(8)
            else:
                self.progress.stop()
                self.progress.config(mode="determinate", maximum=max(1, maximum), value=value)

    def _on_stop(self) -> None:
        """Request the running fetch to stop."""
        self._stop_requested.set()

    def _on_purge_cache(self) -> None:
        """Purge the GW2 API cache and show feedback."""
        if not messagebox.askyesno("Purge cache", "Remove all cached API data? The next fetch will re-download everything."):
            return
        if purge_cache():
            self.status_var.set("Cache purged.")
            messagebox.showinfo("Purge cache", "Cache has been purged.")
        else:
            self.status_var.set("Failed to purge cache.")
            messagebox.showerror("Purge cache", "Could not purge the cache. Check logs.")

    def _on_populate_food_list(self) -> None:
        """Fetch food names from the GW2 Wiki and save to food.json (runs in background)."""
        self.status_var.set("Fetching food list from Wiki…")
        self.root.update_idletasks()

        result_holder: list = []
        error_holder: list = []

        def work() -> None:
            names, err = fetch_food_names_from_wiki()
            if err:
                error_holder.append(err)
            else:
                result_holder.extend(names)

        def on_done() -> None:
            if error_holder:
                self.status_var.set("Wiki fetch failed.")
                messagebox.showerror("Populate food list", error_holder[0])
                return
            names = result_holder
            save_food_list(names)
            self.status_var.set(f"Food list updated ({len(names)} items).")
            messagebox.showinfo(
                "Populate food list",
                f"Saved {len(names)} food names from the Wiki to food.json.\n"
                "Fetch cheapest food will now only consider these items.",
            )

        def run_then_schedule() -> None:
            work()
            self.root.after(0, on_done)

        thread = threading.Thread(target=run_then_schedule, daemon=True)
        thread.start()

    def _on_run(self) -> None:
        """Run the analysis and fill the results table (starts background thread)."""
        self._save_state()
        api_url = self.api_url_var.get().strip() or "https://api.guildwars2.com"
        lang = "en"
        price_type = "sells"  # always sort by instant buy

        self._stop_requested.clear()
        self._details_pct = 0.0
        self.status_var.set("Starting…")
        self.run_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self._show_progress(True, indeterminate=True)
        self.root.update_idletasks()

        result_holder: list = []
        error_holder: list = []
        stopped_flag: list[bool] = [False]

        def schedule(fn: Callable[[], None]) -> None:
            self.root.after(0, fn)

        def progress_callback(phase: str, message: str, current: Optional[int], total: Optional[int]) -> None:
            def update() -> None:
                self.status_var.set(message)
                if phase == "ids":
                    self._show_progress(True, indeterminate=True)
                elif phase == "details" and current is not None and total is not None and total > 0:
                    # Details: 0–50%; never decrease (total can change from recipe to item batches)
                    self._details_pct = max(self._details_pct, 50.0 * current / total)
                    self._show_progress(True, indeterminate=False, value=int(self._details_pct), maximum=100)
                elif phase == "prices" and current is not None and total is not None and total > 0:
                    # Prices: 50–100%
                    pct = 50 + 50.0 * current / total
                    self._show_progress(True, indeterminate=False, value=int(pct), maximum=100)
                elif phase == "filter":
                    self._show_progress(True, indeterminate=False, value=int(self._details_pct), maximum=100)
                elif phase == "done":
                    self._show_progress(False)
            schedule(update)

        def on_items_ready(items: list) -> None:
            def update() -> None:
                for row in self.tree.get_children():
                    self.tree.delete(row)
                self._tree_id_to_iid.clear()
                for idx, it in enumerate(items):
                    iid = f"row_{idx}"
                    row_tag = "even" if idx % 2 == 0 else "odd"
                    self.tree.insert("", tk.END, iid=iid, values=(
                        it.get("extract_type", "—"),
                        it.get("name", ""),
                        it.get("extracts_per_compost", "—"),
                        it.get("price_display", "…"),
                        it.get("price_per_extract_display", "…"),
                        it.get("buy_price_display", "…"),
                        it.get("buy_price_per_extract_display", "…"),
                        it.get("rarity", ""),
                    ), tags=(row_tag,))
                    rid = it.get("id")
                    if rid not in self._tree_id_to_iid:
                        self._tree_id_to_iid[rid] = []
                    self._tree_id_to_iid[rid].append(iid)
            schedule(update)

        def on_prices_batch(updates: list) -> None:
            def update() -> None:
                for u in updates:
                    for iid in self._tree_id_to_iid.get(u.get("id"), []):
                        self.tree.set(iid, "instant_buy", u.get("price_display", "—"))
                        self.tree.set(iid, "per_ext_sell", u.get("price_per_extract_display", "—"))
                        self.tree.set(iid, "buy_order", u.get("buy_price_display", "—"))
                        self.tree.set(iid, "per_ext_buy", u.get("buy_price_per_extract_display", "—"))
            schedule(update)

        def work() -> None:
            try:
                result_holder.extend(
                    run_analysis(
                        api_base_url=api_url,
                        lang=lang,
                        price_type=price_type,
                        progress_callback=progress_callback,
                        on_items_ready=on_items_ready,
                        on_prices_batch=on_prices_batch,
                        stop_requested=lambda: self._stop_requested.is_set(),
                        stopped_flag=stopped_flag,
                    )
                )
            except GW2APIError as e:
                error_holder.append(("api", str(e)))
            except Exception as e:
                logger.exception("Analysis failed")
                error_holder.append(("error", str(e)))

        def on_done() -> None:
            self._show_progress(False)
            self.run_btn.state(["!disabled"])
            self.stop_btn.state(["disabled"])
            if error_holder:
                kind, msg = error_holder[0]
                self.status_var.set("Error")
                if kind == "api" and ("timed out" in msg.lower() or "timeout" in msg.lower()):
                    msg = (
                        msg + "\n\nYou can try again; the app will retry automatically. "
                        "If the API is slow, use Purge cache and run again to refresh in the background."
                    )
                messagebox.showerror("API Error" if kind == "api" else "Error", msg)
                return
            if stopped_flag and stopped_flag[0]:
                self.status_var.set("Stopped.")
                return
            results = result_holder
            self._current_results = results
            # Replace table with sorted results (by instant buy / sell price per extract)
            for row in self.tree.get_children():
                self.tree.delete(row)
            self._tree_id_to_iid.clear()
            for idx, r in enumerate(results):
                iid = f"row_{idx}"
                row_tag = "even" if idx % 2 == 0 else "odd"
                self.tree.insert("", tk.END, iid=iid, values=(
                    r.get("extract_type", "—"),
                    r.get("name", ""),
                    r.get("extracts_per_compost", "—"),
                    r.get("price_display", ""),
                    r.get("price_per_extract_display", ""),
                    r.get("buy_price_display", ""),
                    r.get("buy_price_per_extract_display", ""),
                    r.get("rarity", ""),
                ), tags=(row_tag,))
                rid = r.get("id")
                if rid not in self._tree_id_to_iid:
                    self._tree_id_to_iid[rid] = []
                self._tree_id_to_iid[rid].append(iid)
            self.status_var.set(
                f"Done. Cheapest food for {len(results)} extract type(s) (Fine, Masterwork, Rare, Exotic)."
            )
            # Persist results so they load on next launch
            self._state["last_results"] = results
            self._state["last_results_api_base_url"] = api_url
            self._state["last_results_lang"] = lang
            save_state(self._state)

        def run_then_schedule() -> None:
            work()
            schedule(on_done)

        thread = threading.Thread(target=run_then_schedule, daemon=True)
        thread.start()

    def run(self) -> None:
        """Start the main loop."""
        self.root.mainloop()


def main() -> int:
    """Entry point for the GUI application."""
    app = Application()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
