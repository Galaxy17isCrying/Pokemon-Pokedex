
import hashlib
import io
import json
import os
import queue
import random
import threading
from concurrent.futures import ThreadPoolExecutor

import customtkinter as ctk
import requests
from PIL import Image

API = "https://pokeapi.co/api/v2"
SPRITES = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon"
MAX_ID = 1025
PAGE = 40
CACHE = os.path.join(os.path.expanduser("~"), ".pokedex_cache")
os.makedirs(CACHE, exist_ok=True)
session = requests.Session()

BG = "#0e1018"
PANEL = "#171a26"
PANEL_2 = "#1f2333"
TEXT = "#eef0f8"
MUTED = "#8a90a8"

TYPE_COLORS = {
    "normal": "#a8a77a", "fire": "#ee8130", "water": "#6390f0",
    "electric": "#f7d02c", "grass": "#7ac74c", "ice": "#96d9d6",
    "fighting": "#c22e28", "poison": "#a33ea1", "ground": "#e2bf65",
    "flying": "#a98ff3", "psychic": "#f95587", "bug": "#a6b91a",
    "rock": "#b6a136", "ghost": "#735797", "dragon": "#6f35fc",
    "dark": "#705746", "steel": "#b7b7ce", "fairy": "#d685ad",
}
STATS = [("hp", "HP"), ("attack", "ATK"), ("defense", "DEF"),
         ("special-attack", "SP.ATK"), ("special-defense", "SP.DEF"),
         ("speed", "SPD")]


def _cache_path(url, ext):
    return os.path.join(CACHE, hashlib.md5(url.encode()).hexdigest() + ext)


def get_json(url):
    path = _cache_path(url, ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    r = session.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def get_image(url):
    path = _cache_path(url, ".png")
    if not os.path.exists(path):
        r = session.get(url, timeout=20)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return Image.open(path).convert("RGBA")


def blend(hex_a, hex_b, t):
    """Mix colour a into b by t (0..1)."""
    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(b[i] + (a[i] - b[i]) * t) for i in range(3))


def pretty(name):
    return name.replace("-", " ").title()


def stat_color(v):
    if v < 50:
        return "#e5534b"
    if v < 80:
        return "#f0a43a"
    if v < 110:
        return "#d9c93b"
    if v < 140:
        return "#5fcf7a"
    return "#3ec6e0"
class Pokedex(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Pokédex")
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.configure(fg_color=BG)

        self.q = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.all_pokemon = []          # [(id, name)]
        self.filtered = []
        self.type_ids = None           # set of ids when a type filter is active
        self.shown = PAGE
        self.rows = {}                 # id -> button
        self.thumbs = {}               # id -> CTkImage
        self.thumb_requested = set()
        self.current_id = None
        self.search_job = None

        self._build_sidebar()
        self._build_detail()
        self.after(50, self._poll)
        self.pool.submit(self._load_list)

    def _build_sidebar(self):
        side = ctk.CTkFrame(self, width=340, fg_color=PANEL, corner_radius=0)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        ctk.CTkLabel(side, text="POKÉDEX", text_color=TEXT,
                     font=ctk.CTkFont("Helvetica", 28, "bold")
                     ).pack(anchor="w", padx=20, pady=(20, 0))
        self.status = ctk.CTkLabel(side, text="Loading Pokémon…", text_color=MUTED,
                                   font=ctk.CTkFont(size=12))
        self.status.pack(anchor="w", padx=20, pady=(0, 12))

        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", self._on_search)
        ctk.CTkEntry(side, textvariable=self.search_var, height=40, corner_radius=20,
                     placeholder_text="🔍  Search name or number",
                     fg_color=PANEL_2, border_width=0, text_color=TEXT
                     ).pack(fill="x", padx=20)

        self.type_menu = ctk.CTkOptionMenu(
            side, values=["All types"] + [pretty(t) for t in TYPE_COLORS],
            command=self._on_type, height=34, corner_radius=17,
            fg_color=PANEL_2, button_color=PANEL_2, button_hover_color="#2a2f45")
        self.type_menu.pack(fill="x", padx=20, pady=(10, 10))

        self.list_frame = ctk.CTkScrollableFrame(side, fg_color="transparent")
        self.list_frame.pack(fill="both", expand=True, padx=(10, 6), pady=(0, 10))

    def _build_detail(self):
        self.detail = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.detail.pack(side="left", fill="both", expand=True, padx=24, pady=20)

        # Hero card
        self.hero = ctk.CTkFrame(self.detail, corner_radius=28, fg_color=PANEL_2)
        self.hero.pack(fill="x")
        self.hero.grid_columnconfigure(1, weight=1)

        self.art = ctk.CTkLabel(self.hero, text="", width=300, height=300)
        self.art.grid(row=0, column=0, rowspan=2, padx=20, pady=20)

        info = ctk.CTkFrame(self.hero, fg_color="transparent")
        info.grid(row=0, column=1, sticky="nsew", padx=(0, 24), pady=(28, 0))
        self.num_lbl = ctk.CTkLabel(info, text="#000", text_color="#d0d3dc",
                                    font=ctk.CTkFont("Helvetica", 18, "bold"))
        self.num_lbl.pack(anchor="w")
        self.name_lbl = ctk.CTkLabel(info, text="—", text_color="white",
                                     font=ctk.CTkFont("Helvetica", 42, "bold"))
        self.name_lbl.pack(anchor="w")
        self.genus_lbl = ctk.CTkLabel(info, text="", text_color="#e6e8ee",
                                      font=ctk.CTkFont(size=15, slant="italic"))
        self.genus_lbl.pack(anchor="w", pady=(0, 10))
        self.type_row = ctk.CTkFrame(info, fg_color="transparent")
        self.type_row.pack(anchor="w")

        bottom = ctk.CTkFrame(self.hero, fg_color="transparent")
        bottom.grid(row=1, column=1, sticky="sew", padx=(0, 24), pady=(0, 24))
        self.shiny = ctk.CTkSwitch(bottom, text="✨ Shiny", command=self._reload_art,
                                   text_color="white", progress_color="#ffd84a")
        self.shiny.pack(side="left")
        for txt, cmd in (("🎲", self._random), ("▶", lambda: self._step(1)),
                         ("◀", lambda: self._step(-1))):
            ctk.CTkButton(bottom, text=txt, width=42, height=36, corner_radius=18,
                          fg_color="#12141e", hover_color="#0a0b11",
                          command=cmd).pack(side="right", padx=3)

        # Description
        desc_card = ctk.CTkFrame(self.detail, corner_radius=20, fg_color=PANEL)
        desc_card.pack(fill="x", pady=(16, 0))
        ctk.CTkLabel(desc_card, text="POKÉDEX ENTRY", text_color=MUTED,
                     font=ctk.CTkFont(size=11, weight="bold")
                     ).pack(anchor="w", padx=20, pady=(14, 2))
        self.desc_lbl = ctk.CTkLabel(desc_card, text="", text_color=TEXT, justify="left",
                                     anchor="w", wraplength=700,
                                     font=ctk.CTkFont(size=15))
        self.desc_lbl.pack(fill="x", padx=20, pady=(0, 16))

        # Facts + stats
        lower = ctk.CTkFrame(self.detail, fg_color="transparent")
        lower.pack(fill="both", expand=True, pady=(16, 0))
        lower.grid_columnconfigure(0, weight=1, uniform="a")
        lower.grid_columnconfigure(1, weight=2, uniform="a")
        lower.grid_rowconfigure(0, weight=1)

        facts = ctk.CTkFrame(lower, corner_radius=20, fg_color=PANEL)
        facts.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.fact_labels = {}
        for key in ("Height", "Weight", "Abilities"):
            ctk.CTkLabel(facts, text=key.upper(), text_color=MUTED,
                         font=ctk.CTkFont(size=11, weight="bold")
                         ).pack(anchor="w", padx=20, pady=(16, 0))
            lbl = ctk.CTkLabel(facts, text="—", text_color=TEXT, justify="left",
                               anchor="w", wraplength=200, font=ctk.CTkFont(size=16))
            lbl.pack(anchor="w", padx=20)
            self.fact_labels[key] = lbl

        stats = ctk.CTkFrame(lower, corner_radius=20, fg_color=PANEL)
        stats.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(stats, text="BASE STATS", text_color=MUTED,
                     font=ctk.CTkFont(size=11, weight="bold")
                     ).pack(anchor="w", padx=20, pady=(14, 6))
        self.stat_widgets = {}
        for key, label in STATS:
            row = ctk.CTkFrame(stats, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=4)
            ctk.CTkLabel(row, text=label, width=64, anchor="w", text_color=MUTED,
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            val = ctk.CTkLabel(row, text="0", width=40, text_color=TEXT,
                               font=ctk.CTkFont(size=14, weight="bold"))
            val.pack(side="left")
            bar = ctk.CTkProgressBar(row, height=10, corner_radius=5, fg_color=PANEL_2)
            bar.set(0)
            bar.pack(side="left", fill="x", expand=True, padx=(8, 0))
            self.stat_widgets[key] = (val, bar)
        self.total_lbl = ctk.CTkLabel(stats, text="", text_color=MUTED, anchor="e",
                                      font=ctk.CTkFont(size=12))
        self.total_lbl.pack(anchor="e", padx=20, pady=(4, 12))

   
    def _on_search(self, *_):
        if self.search_job:
            self.after_cancel(self.search_job)
        self.search_job = self.after(200, self._apply_filter)

    def _on_type(self, choice):
        if choice == "All types":
            self.type_ids = None
            self._apply_filter()
            return
        self.status.configure(text=f"Loading {choice} types…")
        name = choice.lower()
        self.pool.submit(self._load_type, name)

    def _apply_filter(self, keep_page=False):
        term = self.search_var.get().strip().lower().lstrip("#")
        res = []
        for pid, name in self.all_pokemon:
            if self.type_ids is not None and pid not in self.type_ids:
                continue
            if term and term not in name and term != str(pid) and not (
                    term.isdigit() and str(pid).zfill(4).endswith(term.zfill(1)) and len(term) > 2):
                continue
            res.append((pid, name))
        self.filtered = res
        if not keep_page:
            self.shown = PAGE
        self.status.configure(text=f"{len(res)} Pokémon")
        self._render_rows()

    def _render_rows(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.rows.clear()
        for pid, name in self.filtered[:self.shown]:
            btn = ctk.CTkButton(
                self.list_frame, text=f"  #{pid:04d}   {pretty(name)}", anchor="w",
                height=56, corner_radius=14, fg_color="transparent",
                hover_color=PANEL_2, text_color=TEXT, compound="left",
                font=ctk.CTkFont(size=14, weight="bold"),
                image=self.thumbs.get(pid),
                command=lambda p=pid: self.select(p))
            btn.pack(fill="x", pady=1)
            self.rows[pid] = btn
            if pid not in self.thumbs and pid not in self.thumb_requested:
                self.thumb_requested.add(pid)
                self.pool.submit(self._load_thumb, pid)
        if len(self.filtered) > self.shown:
            more = ctk.CTkButton(self.list_frame, text="Show more ↓", height=36,
                                 corner_radius=18, fg_color=PANEL_2, hover_color="#2a2f45",
                                 command=self._more)
            more.pack(fill="x", pady=8)
        self._highlight()

    def _more(self):
        self.shown += PAGE
        self._render_rows()

    def _highlight(self):
        for pid, btn in self.rows.items():
            btn.configure(fg_color=PANEL_2 if pid == self.current_id else "transparent")
    def select(self, pid):
        self.current_id = pid
        self._highlight()
        self.name_lbl.configure(text="Loading…")
        self.pool.submit(self._load_detail, pid, self.shiny.get())

    def _random(self):
        pool = self.filtered or self.all_pokemon
        if pool:
            self.select(random.choice(pool)[0])

    def _step(self, d):
        if not self.current_id:
            return
        self.select((self.current_id - 1 + d) % MAX_ID + 1)

    def _reload_art(self):
        if self.current_id:
            self.pool.submit(self._load_art_only, self.current_id, self.shiny.get())

    
    def _load_list(self):
        try:
            data = get_json(f"{API}/pokemon?limit={MAX_ID}")
            out = []
            for item in data["results"]:
                pid = int(item["url"].rstrip("/").split("/")[-1])
                if pid <= MAX_ID:
                    out.append((pid, item["name"]))
            self.q.put(("list", out))
        except Exception as e:
            self.q.put(("error", f"Couldn't reach PokeAPI: {e}"))

    def _load_type(self, name):
        try:
            data = get_json(f"{API}/type/{name}")
            ids = {int(p["pokemon"]["url"].rstrip("/").split("/")[-1])
                   for p in data["pokemon"]}
            self.q.put(("type", ids))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _load_thumb(self, pid):
        try:
            img = get_image(f"{SPRITES}/{pid}.png")
            self.q.put(("thumb", pid, img))
        except Exception:
            pass

    def _fetch_art(self, pid, shiny):
        urls = [f"{SPRITES}/other/official-artwork/{'shiny/' if shiny else ''}{pid}.png",
                f"{SPRITES}/{'shiny/' if shiny else ''}{pid}.png"]
        for u in urls:
            try:
                return get_image(u)
            except Exception:
                continue
        return None

    def _load_art_only(self, pid, shiny):
        img = self._fetch_art(pid, shiny)
        if img:
            self.q.put(("art", pid, img))

    def _load_detail(self, pid, shiny):
        try:
            p = get_json(f"{API}/pokemon/{pid}")
            s = get_json(f"{API}/pokemon-species/{pid}")
            art = self._fetch_art(pid, shiny)
            self.q.put(("detail", pid, p, s, art))
        except Exception as e:
            self.q.put(("error", f"Couldn't load Pokémon #{pid}: {e}"))

    
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "list":
                    self.all_pokemon = msg[1]
                    self._apply_filter()
                    self.select(25)
                elif kind == "type":
                    self.type_ids = msg[1]
                    self._apply_filter()
                elif kind == "thumb":
                    _, pid, img = msg
                    ci = ctk.CTkImage(img, size=(48, 48))
                    self.thumbs[pid] = ci
                    if pid in self.rows:
                        self.rows[pid].configure(image=ci)
                elif kind == "art":
                    if msg[1] == self.current_id:
                        self._set_art(msg[2])
                elif kind == "detail":
                    if msg[1] == self.current_id:
                        self._show_detail(*msg[2:])
                elif kind == "error":
                    self.status.configure(text=msg[1][:60])
        except queue.Empty:
            pass
        self.after(50, self._poll)

    def _set_art(self, img):
        ci = ctk.CTkImage(img, size=(300, 300))
        self.art.configure(image=ci)
        self._art_img = ci  # keep a reference so it isn't garbage-collected

    def _show_detail(self, p, s, art):
        types = [t["type"]["name"] for t in p["types"]]
        main = TYPE_COLORS.get(types[0], "#777777")
        self.hero.configure(fg_color=blend(main, BG, 0.55))

        self.num_lbl.configure(text=f"#{p['id']:04d}")
        self.name_lbl.configure(text=pretty(p["name"]))
        genus = next((g["genus"] for g in s["genera"] if g["language"]["name"] == "en"), "")
        self.genus_lbl.configure(text=genus)

        for w in self.type_row.winfo_children():
            w.destroy()
        for t in types:
            ctk.CTkLabel(self.type_row, text=t.upper(), text_color="white",
                         fg_color=TYPE_COLORS.get(t, "#777"), corner_radius=14,
                         height=28, width=96,
                         font=ctk.CTkFont(size=12, weight="bold")
                         ).pack(side="left", padx=(0, 8))

        entries = [e for e in s["flavor_text_entries"] if e["language"]["name"] == "en"]
        text = entries[-1]["flavor_text"] if entries else "No description available."
        self.desc_lbl.configure(text=" ".join(text.replace("\f", " ").split()))

        self.fact_labels["Height"].configure(text=f"{p['height'] / 10:.1f} m")
        self.fact_labels["Weight"].configure(text=f"{p['weight'] / 10:.1f} kg")
        self.fact_labels["Abilities"].configure(
            text="\n".join(pretty(a["ability"]["name"]) + (" (hidden)" if a["is_hidden"] else "")
                           for a in p["abilities"]))

        total = 0
        for st in p["stats"]:
            key = st["stat"]["name"]
            v = st["base_stat"]
            total += v
            val, bar = self.stat_widgets[key]
            val.configure(text=str(v))
            bar.configure(progress_color=stat_color(v))
            bar.set(min(v / 200, 1))
        self.total_lbl.configure(text=f"Total  {total}")

        if art:
            self._set_art(art)
        self._highlight()


if __name__ == "__main__":
    Pokedex().mainloop()