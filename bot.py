import os
import json
import time
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite

from dotenv import load_dotenv

# =========================
# Basics / Config loading
# =========================

load_dotenv()  # loads .env
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Put it in .env as: DISCORD_TOKEN=...")

CONFIG_PATH = "config.json"
DB_PATH = os.getenv("DB_PATH", "lager.db")

# Ensure DB folder exists (important for Railway volume mounts)
db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

print("Using DB_PATH:", DB_PATH)

TYPE_FBH = "FBH"
TYPE_KL = "KL"

# Default spoil times if seasonal spoilage is OFF
DEFAULT_FBH_SMELL_DAYS = 7
DEFAULT_FBH_RUIN_DAYS = 14

# Seasonal spoil times (if seasonal spoilage is ON)
SAISON_TIMES = {
    "Blattfrische": (4, 7),  # Spring
    "Blattfall": (4, 7),     # Fall
    "Blattleere": (7, 10),   # Winter
    "Blattgrüne": (2, 5),    # Summer
}

# FBH aliases for easier typing -> normalized to the Allgemein entries
ALIASES_FBH = {
    "maus": "Maus (allgemein)",
    "mäuse": "Maus (allgemein)",
    "mouse": "Maus (allgemein)",

    "vogel": "Vogel (allgemein)",
    "vögel": "Vogel (allgemein)",
    "bird": "Vogel (allgemein)",

    "raubvogel": "Raubvogel (allgemein)",
    "raubvögel": "Raubvogel (allgemein)",
    "greifvogel": "Raubvogel (allgemein)",
    "greifvögel": "Raubvogel (allgemein)",

    "fisch": "Fisch (allgemein)",
    "fish": "Fisch (allgemein)",

    "wasservogel": "Wasservogel (allgemein)",
    "wasservögel": "Wasservogel (allgemein)",

    "gans": "Gans (allgemein)",
    "gänse": "Gans (allgemein)",

    "ratte": "Ratte (allgemein)",
    "ratten": "Ratte (allgemein)",

    "kaninchen": "Kaninchen (allgemein)",
    "rabbit": "Kaninchen (allgemein)",

    "hase": "Hase (allgemein)",
    "hasen": "Hase (allgemein)",

    "schlange": "Schlange (allgemein)",
    "schlangen": "Schlange (allgemein)",
}

def now_ts() -> int:
    return int(time.time())

def norm_key(s: str) -> str:
    return " ".join(s.strip().split()).lower()

def apply_aliases_fbh(name: str) -> str:
    k = norm_key(name)
    return ALIASES_FBH.get(k, name.strip())

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_config()

ALLOWED_FBH_PREY = [x.strip() for x in CONFIG.get("allowed_fbh_prey", []) if x.strip()]
ALLOWED_KL_HERBS = [x.strip() for x in CONFIG.get("allowed_kl_herbs", []) if x.strip()]
KL_RECIPES: Dict[str, Dict[str, int]] = CONFIG.get("kl_rezepte", {}) or {}

ALLOWED_FBH_SET = {norm_key(x): x for x in ALLOWED_FBH_PREY}   # normalized -> canonical display
ALLOWED_KL_SET = {norm_key(x): x for x in ALLOWED_KL_HERBS}

def is_allowed(name: str, allowed_map: Dict[str, str]) -> bool:
    return norm_key(name) in allowed_map

def canonical(name: str, allowed_map: Dict[str, str]) -> str:
    return allowed_map.get(norm_key(name), name.strip())

# =========================
# Permissions helpers
# =========================

def is_mod_or_admin(member: discord.Member) -> bool:
    # Role-name based (as requested): Mod/Admin
    role_names = {r.name.lower() for r in member.roles}
    if "mod".lower() in role_names or "admin".lower() in role_names:
        return True
    # fallback: server permission
    perms = member.guild_permissions
    return perms.manage_guild or perms.administrator

# =========================
# Database layer
# =========================

class LagerDB:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def open(self):
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute("PRAGMA foreign_keys = ON;")
        await self._init_schema()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def _init_schema(self):
        assert self.conn
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS storages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                type TEXT NOT NULL,              -- FBH oder KL
                name TEXT NOT NULL,
                owner_role_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE (guild_id, type, name)
            );

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                saison TEXT NOT NULL DEFAULT 'Blattfrische',
                fbh_saison_verderb INTEGER NOT NULL DEFAULT 0,
                kl_trocknen INTEGER NOT NULL DEFAULT 0
            );

            -- FBH: we store batches so we know age of added prey
            CREATE TABLE IF NOT EXISTS fbh_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                storage_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                item_display TEXT NOT NULL,
                qty INTEGER NOT NULL,
                added_at INTEGER NOT NULL,
                added_by INTEGER,
                FOREIGN KEY(storage_id) REFERENCES storages(id) ON DELETE CASCADE
            );

            -- KL herbs as lots: fresh/dried by state
            CREATE TABLE IF NOT EXISTS kl_herb_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                storage_id INTEGER NOT NULL,
                herb_key TEXT NOT NULL,
                herb_display TEXT NOT NULL,
                qty INTEGER NOT NULL,
                state TEXT NOT NULL,      -- 'frisch' or 'getrocknet'
                added_at INTEGER NOT NULL,
                added_by INTEGER,
                FOREIGN KEY(storage_id) REFERENCES storages(id) ON DELETE CASCADE
            );

            -- KL pantry (prepared sets/mixes)
            CREATE TABLE IF NOT EXISTS kl_pantry (
                storage_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                item_display TEXT NOT NULL,
                qty INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                updated_by INTEGER,
                PRIMARY KEY (storage_id, item_key),
                FOREIGN KEY(storage_id) REFERENCES storages(id) ON DELETE CASCADE
            );
            """
        )
        await self.conn.commit()

    # ---------- settings ----------
    async def ensure_settings(self, guild_id: int):
        assert self.conn
        await self.conn.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
            (guild_id,),
        )
        await self.conn.commit()

    async def get_settings(self, guild_id: int) -> dict:
        assert self.conn
        await self.ensure_settings(guild_id)
        cur = await self.conn.execute(
            "SELECT saison, fbh_saison_verderb, kl_trocknen FROM guild_settings WHERE guild_id=?",
            (guild_id,),
        )
        row = await cur.fetchone()
        return {
            "saison": row[0],
            "fbh_saison_verderb": bool(row[1]),
            "kl_trocknen": bool(row[2]),
        }

    async def set_saison(self, guild_id: int, saison: str):
        assert self.conn
        await self.ensure_settings(guild_id)
        await self.conn.execute(
            "UPDATE guild_settings SET saison=? WHERE guild_id=?",
            (saison, guild_id),
        )
        await self.conn.commit()

    async def set_fbh_saison_verderb(self, guild_id: int, active: bool):
        assert self.conn
        await self.ensure_settings(guild_id)
        await self.conn.execute(
            "UPDATE guild_settings SET fbh_saison_verderb=? WHERE guild_id=?",
            (1 if active else 0, guild_id),
        )
        await self.conn.commit()

    async def set_kl_trocknen(self, guild_id: int, active: bool):
        assert self.conn
        await self.ensure_settings(guild_id)
        await self.conn.execute(
            "UPDATE guild_settings SET kl_trocknen=? WHERE guild_id=?",
            (1 if active else 0, guild_id),
        )
        await self.conn.commit()

    # ---------- storages ----------
    async def storage_create(self, guild_id: int, type_: str, name: str, owner_role_id: int) -> int:
        assert self.conn
        await self.conn.execute(
            "INSERT INTO storages (guild_id, type, name, owner_role_id, created_at) VALUES (?,?,?,?,?)",
            (guild_id, type_, name.strip(), owner_role_id, now_ts()),
        )
        await self.conn.commit()
        cur = await self.conn.execute("SELECT last_insert_rowid()")
        row = await cur.fetchone()
        return int(row[0])

    async def storage_list(self, guild_id: int) -> List[Tuple[str, str]]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT type, name FROM storages WHERE guild_id=? ORDER BY type, name COLLATE NOCASE",
            (guild_id,),
        )
        return await cur.fetchall()

    async def storage_get(self, guild_id: int, type_: str, name: str) -> Optional[Tuple[int, int]]:
        """returns (storage_id, owner_role_id)"""
        assert self.conn
        cur = await self.conn.execute(
            "SELECT id, owner_role_id FROM storages WHERE guild_id=? AND type=? AND name=?",
            (guild_id, type_, name.strip()),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return int(row[0]), int(row[1])

    async def storage_names_for_type(self, guild_id: int, type_: str) -> List[str]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT name FROM storages WHERE guild_id=? AND type=? ORDER BY name COLLATE NOCASE",
            (guild_id, type_),
        )
        return [r[0] for r in await cur.fetchall()]

    # ---------- FBH ----------
    async def fbh_add(self, storage_id: int, item_display: str, qty: int, user_id: int):
        assert self.conn
        k = norm_key(item_display)
        await self.conn.execute(
            "INSERT INTO fbh_batches (storage_id, item_key, item_display, qty, added_at, added_by) VALUES (?,?,?,?,?,?)",
            (storage_id, k, item_display.strip(), qty, now_ts(), user_id),
        )
        await self.conn.commit()

    async def fbh_total(self, storage_id: int) -> List[Tuple[str, int]]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT item_display, SUM(qty) FROM fbh_batches WHERE storage_id=? GROUP BY item_key ORDER BY item_display COLLATE NOCASE",
            (storage_id,),
        )
        rows = await cur.fetchall()
        return [(r[0], int(r[1] or 0)) for r in rows if int(r[1] or 0) > 0]

    async def fbh_take(self, storage_id: int, item_display: str, qty: int) -> int:
        """
        FIFO: remove from oldest batches first.
        Returns actually removed amount.
        """
        assert self.conn
        k = norm_key(item_display)
        cur = await self.conn.execute(
            "SELECT id, qty FROM fbh_batches WHERE storage_id=? AND item_key=? ORDER BY added_at ASC",
            (storage_id, k),
        )
        batches = await cur.fetchall()
        to_remove = qty
        removed = 0

        for batch_id, batch_qty in batches:
            if to_remove <= 0:
                break
            take = min(int(batch_qty), to_remove)
            new_qty = int(batch_qty) - take
            removed += take
            to_remove -= take

            if new_qty <= 0:
                await self.conn.execute("DELETE FROM fbh_batches WHERE id=?", (batch_id,))
            else:
                await self.conn.execute("UPDATE fbh_batches SET qty=? WHERE id=?", (new_qty, batch_id))

        await self.conn.commit()
        return removed

    async def fbh_batches_for_status(self, storage_id: int) -> List[Tuple[str, int, int]]:
        """
        returns list of (item_display, qty, added_at)
        """
        assert self.conn
        cur = await self.conn.execute(
            "SELECT item_display, qty, added_at FROM fbh_batches WHERE storage_id=? ORDER BY added_at ASC",
            (storage_id,),
        )
        rows = await cur.fetchall()
        return [(r[0], int(r[1]), int(r[2])) for r in rows]

    # ---------- KL drying ----------
    async def kl_mark_dried_older_than(self, storage_id: int, cutoff_ts: int):
        assert self.conn
        await self.conn.execute(
            "UPDATE kl_herb_lots SET state='getrocknet' WHERE storage_id=? AND state='frisch' AND added_at <= ?",
            (storage_id, cutoff_ts),
        )
        await self.conn.commit()

    # ---------- KL herbs ----------
    async def kl_add_herb(self, storage_id: int, herb_display: str, qty: int, user_id: int):
        assert self.conn
        k = norm_key(herb_display)
        await self.conn.execute(
            "INSERT INTO kl_herb_lots (storage_id, herb_key, herb_display, qty, state, added_at, added_by) VALUES (?,?,?,?,?,?,?)",
            (storage_id, k, herb_display.strip(), qty, "frisch", now_ts(), user_id),
        )
        await self.conn.commit()

    async def kl_totals_by_state(self, storage_id: int) -> Tuple[List[Tuple[str,int]], List[Tuple[str,int]]]:
        """
        returns (fresh_list, dried_list) as [(herb_display, total_qty)]
        """
        assert self.conn
        cur = await self.conn.execute(
            "SELECT herb_display, state, SUM(qty) FROM kl_herb_lots WHERE storage_id=? GROUP BY herb_key, state ORDER BY herb_display COLLATE NOCASE",
            (storage_id,),
        )
        rows = await cur.fetchall()
        fresh = []
        dried = []
        for herb_display, state, total in rows:
            total = int(total or 0)
            if total <= 0:
                continue
            if state == "frisch":
                fresh.append((herb_display, total))
            else:
                dried.append((herb_display, total))
        return fresh, dried

    async def kl_total_available(self, storage_id: int, herb_key: str) -> int:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT SUM(qty) FROM kl_herb_lots WHERE storage_id=? AND herb_key=?",
            (storage_id, herb_key),
        )
        row = await cur.fetchone()
        return int(row[0] or 0)

    async def kl_take_herb(self, storage_id: int, herb_display: str, qty: int) -> int:
        """
        Takes herbs FIFO. Prefers fresh first, then dried.
        Returns actually removed.
        """
        assert self.conn
        k = norm_key(herb_display)
        to_remove = qty
        removed = 0

        for state in ["frisch", "getrocknet"]:
            if to_remove <= 0:
                break
            cur = await self.conn.execute(
                "SELECT id, qty FROM kl_herb_lots WHERE storage_id=? AND herb_key=? AND state=? ORDER BY added_at ASC",
                (storage_id, k, state),
            )
            lots = await cur.fetchall()
            for lot_id, lot_qty in lots:
                if to_remove <= 0:
                    break
                take = min(int(lot_qty), to_remove)
                new_qty = int(lot_qty) - take
                removed += take
                to_remove -= take
                if new_qty <= 0:
                    await self.conn.execute("DELETE FROM kl_herb_lots WHERE id=?", (lot_id,))
                else:
                    await self.conn.execute("UPDATE kl_herb_lots SET qty=? WHERE id=?", (new_qty, lot_id))

        await self.conn.commit()
        return removed

    # ---------- KL pantry ----------
    async def pantry_add(self, storage_id: int, item_display: str, qty: int, user_id: int):
        assert self.conn
        k = norm_key(item_display)
        cur = await self.conn.execute(
            "SELECT qty FROM kl_pantry WHERE storage_id=? AND item_key=?",
            (storage_id, k),
        )
        row = await cur.fetchone()
        if row:
            new_qty = int(row[0]) + qty
            await self.conn.execute(
                "UPDATE kl_pantry SET qty=?, updated_at=?, updated_by=? WHERE storage_id=? AND item_key=?",
                (new_qty, now_ts(), user_id, storage_id, k),
            )
        else:
            await self.conn.execute(
                "INSERT INTO kl_pantry (storage_id, item_key, item_display, qty, updated_at, updated_by) VALUES (?,?,?,?,?,?)",
                (storage_id, k, item_display.strip(), qty, now_ts(), user_id),
            )
        await self.conn.commit()

    async def pantry_take(self, storage_id: int, item_display: str, qty: int) -> int:
        assert self.conn
        k = norm_key(item_display)
        cur = await self.conn.execute(
            "SELECT qty FROM kl_pantry WHERE storage_id=? AND item_key=?",
            (storage_id, k),
        )
        row = await cur.fetchone()
        if not row:
            return 0
        have = int(row[0])
        take = min(have, qty)
        new_qty = have - take
        if new_qty <= 0:
            await self.conn.execute("DELETE FROM kl_pantry WHERE storage_id=? AND item_key=?", (storage_id, k))
        else:
            await self.conn.execute(
                "UPDATE kl_pantry SET qty=? WHERE storage_id=? AND item_key=?",
                (new_qty, storage_id, k),
            )
        await self.conn.commit()
        return take

    async def pantry_list(self, storage_id: int) -> List[Tuple[str,int]]:
        assert self.conn
        cur = await self.conn.execute(
            "SELECT item_display, qty FROM kl_pantry WHERE storage_id=? ORDER BY item_display COLLATE NOCASE",
            (storage_id,),
        )
        rows = await cur.fetchall()
        return [(r[0], int(r[1])) for r in rows if int(r[1]) > 0]

# =========================
# Discord bot
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # to read roles

class WaCaBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db = LagerDB(DB_PATH)

    async def setup_hook(self):
        await self.db.open()
        # sync commands globally
        await self.tree.sync()

bot = WaCaBot()

# =========================
# Access checks
# =========================

async def resolve_storage(interaction: discord.Interaction, type_: str, lager_name: str) -> Tuple[int, int]:
    """returns (storage_id, owner_role_id) or raises ValueError"""
    if not interaction.guild:
        raise ValueError("Nur auf einem Server nutzbar.")
    got = await bot.db.storage_get(interaction.guild.id, type_, lager_name)
    if not got:
        raise ValueError("Lager nicht gefunden. Nutze /lager_auflisten.")
    return got

def member_has_role_id(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)

def assert_storage_access(member: discord.Member, owner_role_id: int):
    if member_has_role_id(member, owner_role_id):
        return
    if is_mod_or_admin(member):
        return
    raise PermissionError("Du hast keine Berechtigung für dieses Lager.")

async def kl_apply_drying_if_enabled(guild_id: int, storage_id: int):
    settings = await bot.db.get_settings(guild_id)
    if not settings["kl_trocknen"]:
        return
    cutoff = now_ts() - 7 * 24 * 60 * 60
    await bot.db.kl_mark_dried_older_than(storage_id, cutoff)

def fbh_status_for_age_days(age_days: int, saison: str, saison_enabled: bool) -> str:
    if saison_enabled:
        smell_days, ruin_days = SAISON_TIMES.get(saison, (DEFAULT_FBH_SMELL_DAYS, DEFAULT_FBH_RUIN_DAYS))
    else:
        smell_days, ruin_days = (DEFAULT_FBH_SMELL_DAYS, DEFAULT_FBH_RUIN_DAYS)

    if age_days >= ruin_days:
        return "verdorben"
    if age_days >= smell_days:
        return "müffelt"
    return "frisch"

# =========================
# Autocomplete
# =========================

async def ac_lager_fbh(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    names = await bot.db.storage_names_for_type(interaction.guild.id, TYPE_FBH)
    cur = current.lower()
    hits = [n for n in names if cur in n.lower()]
    return [app_commands.Choice(name=h, value=h) for h in hits[:25]]

async def ac_lager_kl(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    names = await bot.db.storage_names_for_type(interaction.guild.id, TYPE_KL)
    cur = current.lower()
    hits = [n for n in names if cur in n.lower()]
    return [app_commands.Choice(name=h, value=h) for h in hits[:25]]

async def ac_fbh_beute(interaction: discord.Interaction, current: str):
    cur = current.lower()
    hits = [x for x in ALLOWED_FBH_PREY if cur in x.lower()]
    return [app_commands.Choice(name=h, value=h) for h in hits[:25]]

async def ac_kl_kraut(interaction: discord.Interaction, current: str):
    cur = current.lower()
    hits = [x for x in ALLOWED_KL_HERBS if cur in x.lower()]
    return [app_commands.Choice(name=h, value=h) for h in hits[:25]]

async def ac_kl_rezept(interaction: discord.Interaction, current: str):
    cur = current.lower()
    names = sorted(KL_RECIPES.keys(), key=lambda s: s.lower())
    hits = [n for n in names if cur in n.lower()]
    return [app_commands.Choice(name=h, value=h) for h in hits[:25]]

# =========================
# Commands
# =========================

@bot.tree.command(name="lager_erstellen", description="Erstellt ein Lager (FBH oder KL) und bindet es an eine Rolle.")
@app_commands.describe(typ="FBH oder KL", name="Name des Lagers", rolle="Wer darf dieses Lager benutzen?")
async def lager_erstellen(interaction: discord.Interaction, typ: str, name: str, rolle: discord.Role):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return

    # optional: only mods/admin can create storages
    if not is_mod_or_admin(interaction.user):
        await interaction.response.send_message("Nur Mod/Admin dürfen Lager erstellen.", ephemeral=True)
        return

    typ = typ.strip().upper()
    if typ not in (TYPE_FBH, TYPE_KL):
        await interaction.response.send_message("Typ muss FBH oder KL sein.", ephemeral=True)
        return

    try:
        await bot.db.storage_create(interaction.guild.id, typ, name, rolle.id)
    except Exception:
        await interaction.response.send_message("Dieses Lager existiert schon (gleicher Typ + Name).", ephemeral=True)
        return

    await interaction.response.send_message(f"✅ Lager erstellt: **{typ}** — **{name}** (Rolle: {rolle.mention})")

@bot.tree.command(name="lager_auflisten", description="Listet alle Lager auf (FBH/KL).")
async def lager_auflisten(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    rows = await bot.db.storage_list(interaction.guild.id)
    if not rows:
        await interaction.response.send_message("Noch keine Lager. Nutze /lager_erstellen.", ephemeral=True)
        return
    lines = [f"• **{t}** — {n}" for t, n in rows]
    await interaction.response.send_message("📦 **Lager**\n" + "\n".join(lines), ephemeral=True)

# ---------- Admin toggles / season ----------

@bot.tree.command(name="saison_setzen", description="Setzt die Saison (Blattfrische/Blattgrüne/Blattfall/Blattleere).")
@app_commands.describe(saison="Blattfrische, Blattgrüne, Blattfall, Blattleere")
async def saison_setzen(interaction: discord.Interaction, saison: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    if not is_mod_or_admin(interaction.user):
        await interaction.response.send_message("Nur Mod/Admin dürfen die Saison setzen.", ephemeral=True)
        return

    saison = saison.strip()
    if saison not in SAISON_TIMES:
        await interaction.response.send_message("Ungültig. Erlaubt: Blattfrische, Blattgrüne, Blattfall, Blattleere.", ephemeral=True)
        return

    await bot.db.set_saison(interaction.guild.id, saison)
    await interaction.response.send_message(f"✅ Saison gesetzt auf **{saison}**.", ephemeral=True)

@bot.tree.command(name="fbh_saison_verderb", description="Schaltet saisonalen Verderb für FBH an/aus.")
@app_commands.describe(aktiv="true oder false")
async def fbh_saison_verderb(interaction: discord.Interaction, aktiv: bool):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    if not is_mod_or_admin(interaction.user):
        await interaction.response.send_message("Nur Mod/Admin dürfen das ändern.", ephemeral=True)
        return

    await bot.db.set_fbh_saison_verderb(interaction.guild.id, aktiv)
    await interaction.response.send_message(f"✅ Saison-Verderb für FBH ist jetzt **{'AN' if aktiv else 'AUS'}**.", ephemeral=True)

@bot.tree.command(name="kl_trocknen", description="Schaltet um, ob frische Kräuter nach 7 Tagen automatisch zu getrocknet werden.")
@app_commands.describe(aktiv="true oder false")
async def kl_trocknen(interaction: discord.Interaction, aktiv: bool):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    if not is_mod_or_admin(interaction.user):
        await interaction.response.send_message("Nur Mod/Admin dürfen das ändern.", ephemeral=True)
        return

    await bot.db.set_kl_trocknen(interaction.guild.id, aktiv)
    await interaction.response.send_message(f"✅ KL Trocknen ist jetzt **{'AN' if aktiv else 'AUS'}**.", ephemeral=True)

# ---------- Recipes / inventory ----------

@bot.tree.command(name="rezepte_anzeigen", description="Zeigt alle KL Rezepte (Sets/Mixes) mit Zutaten an.")
async def rezepte_anzeigen(interaction: discord.Interaction):
    if not KL_RECIPES:
        await interaction.response.send_message("Keine Rezepte in config.json gefunden.", ephemeral=True)
        return

    lines = []
    for rname in sorted(KL_RECIPES.keys(), key=lambda s: s.lower()):
        ing = KL_RECIPES[rname]
        parts = [f"{k}×{v}" for k, v in ing.items()]
        lines.append(f"**{rname}**: " + ", ".join(parts))

    text = "\n".join(lines)
    # Discord message limit safety
    if len(text) > 1800:
        text = text[:1800] + "\n… (zu lang, bitte Rezepte aufteilen)"
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="inventar_anzeigen", description="Zeigt komplettes Inventar eines Lagers (FBH oder KL).")
@app_commands.describe(typ="FBH oder KL", lager="Name des Lagers")
async def inventar_anzeigen(interaction: discord.Interaction, typ: str, lager: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return

    typ = typ.strip().upper()
    if typ not in (TYPE_FBH, TYPE_KL):
        await interaction.response.send_message("Typ muss FBH oder KL sein.", ephemeral=True)
        return

    try:
        storage_id, owner_role_id = await resolve_storage(interaction, typ, lager)
        assert_storage_access(interaction.user, owner_role_id)
    except (ValueError, PermissionError) as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    if typ == TYPE_FBH:
        settings = await bot.db.get_settings(interaction.guild.id)
        totals = await bot.db.fbh_total(storage_id)
        if not totals:
            await interaction.response.send_message(f"🪵 **{lager}** ist leer.", ephemeral=True)
            return

        batches = await bot.db.fbh_batches_for_status(storage_id)
        # compute status per batch and overall flags
        smell = 0
        ruin = 0
        saison = settings["saison"]
        enabled = settings["fbh_saison_verderb"]

        # Simple overall: if ANY batch is verdorben => pile is ruined
        ruined_any = False
        smelly_any = False
        for _, qty, added_at in batches:
            age_days = (now_ts() - added_at) // (24*60*60)
            st = fbh_status_for_age_days(int(age_days), saison, enabled)
            if st == "verdorben":
                ruined_any = True
            elif st == "müffelt":
                smelly_any = True

        header = f"🪵 **FBH Inventar: {lager}**\nSaison: **{saison}** | Saison-Verderb: **{'AN' if enabled else 'AUS'}**\n"
        if ruined_any:
            header += "⚠️ Status: **VERDORBEN** (ruiniert den ganzen Haufen)\n"
        elif smelly_any:
            header += "⚠️ Status: **MÜFFELT**\n"
        else:
            header += "✅ Status: **FRISCH**\n"

        lines = [f"• **{name}**: {amt}" for name, amt in totals]
        await interaction.response.send_message(header + "\n".join(lines), ephemeral=True)

    else:
        await kl_apply_drying_if_enabled(interaction.guild.id, storage_id)

        fresh, dried = await bot.db.kl_totals_by_state(storage_id)
        pantry = await bot.db.pantry_list(storage_id)

        msg = [f"🌿 **KL Inventar: {lager}**"]
        if fresh:
            msg.append("\n**Frisch:**")
            msg.extend([f"• {n}: {a}" for n, a in fresh])
        else:
            msg.append("\n**Frisch:** (leer)")

        if dried:
            msg.append("\n**Getrocknet:**")
            msg.extend([f"• {n}: {a}" for n, a in dried])
        else:
            msg.append("\n**Getrocknet:** (leer)")

        if pantry:
            msg.append("\n**Vorräte (Sets/Mixes):**")
            msg.extend([f"• {n}: {a}" for n, a in pantry])
        else:
            msg.append("\n**Vorräte (Sets/Mixes):** (leer)")

        text = "\n".join(msg)
        if len(text) > 1800:
            text = text[:1800] + "\n… (zu lang)"
        await interaction.response.send_message(text, ephemeral=True)

# ---------- FBH commands ----------

@bot.tree.command(name="fbh_hinzufuegen", description="Fügt Beute zu einem FBH Lager hinzu.")
@app_commands.describe(lager="Name des FBH Lagers", beute="Beute", menge="Menge")
@app_commands.autocomplete(lager=ac_lager_fbh, beute=ac_fbh_beute)
async def fbh_hinzufuegen(interaction: discord.Interaction, lager: str, beute: str, menge: int):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    if menge <= 0:
        await interaction.response.send_message("Menge muss > 0 sein.", ephemeral=True)
        return

    try:
        storage_id, owner_role_id = await resolve_storage(interaction, TYPE_FBH, lager)
        assert_storage_access(interaction.user, owner_role_id)
    except (ValueError, PermissionError) as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    beute = apply_aliases_fbh(beute)
    if not is_allowed(beute, ALLOWED_FBH_SET):
        await interaction.response.send_message("❌ Diese Beute ist nicht erlaubt (siehe config).", ephemeral=True)
        return
    beute = canonical(beute, ALLOWED_FBH_SET)

    await bot.db.fbh_add(storage_id, beute, menge, interaction.user.id)
    await interaction.response.send_message(f"✅ **{beute}** × **{menge}** zu **{lager}** hinzugefügt.", ephemeral=True)

@bot.tree.command(name="fbh_entnehmen", description="Entnimmt Beute aus einem FBH Lager.")
@app_commands.describe(lager="Name des FBH Lagers", beute="Beute", menge="Menge")
@app_commands.autocomplete(lager=ac_lager_fbh, beute=ac_fbh_beute)
async def fbh_entnehmen(interaction: discord.Interaction, lager: str, beute: str, menge: int):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    if menge <= 0:
        await interaction.response.send_message("Menge muss > 0 sein.", ephemeral=True)
        return

    try:
        storage_id, owner_role_id = await resolve_storage(interaction, TYPE_FBH, lager)
        assert_storage_access(interaction.user, owner_role_id)
    except (ValueError, PermissionError) as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    beute = apply_aliases_fbh(beute)
    if not is_allowed(beute, ALLOWED_FBH_SET):
        await interaction.response.send_message("❌ Diese Beute ist nicht erlaubt.", ephemeral=True)
        return
    beute = canonical(beute, ALLOWED_FBH_SET)

    removed = await bot.db.fbh_take(storage_id, beute, menge)
    await interaction.response.send_message(f"✅ Entnommen: **{beute}** × **{removed}** aus **{lager}**.", ephemeral=True)

# ---------- KL commands ----------

@bot.tree.command(name="kl_hinzufuegen", description="Fügt Kräuter zum KL hinzu (immer frisch).")
@app_commands.describe(lager="Name des KL Lagers", kraut="Kraut", menge="Menge")
@app_commands.autocomplete(lager=ac_lager_kl, kraut=ac_kl_kraut)
async def kl_hinzufuegen(interaction: discord.Interaction, lager: str, kraut: str, menge: int):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    if menge <= 0:
        await interaction.response.send_message("Menge muss > 0 sein.", ephemeral=True)
        return

    try:
        storage_id, owner_role_id = await resolve_storage(interaction, TYPE_KL, lager)
        assert_storage_access(interaction.user, owner_role_id)
    except (ValueError, PermissionError) as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    if not is_allowed(kraut, ALLOWED_KL_SET):
        await interaction.response.send_message("❌ Dieses Kraut ist nicht erlaubt (siehe config).", ephemeral=True)
        return
    kraut = canonical(kraut, ALLOWED_KL_SET)

    await kl_apply_drying_if_enabled(interaction.guild.id, storage_id)
    await bot.db.kl_add_herb(storage_id, kraut, menge, interaction.user.id)
    await interaction.response.send_message(f"✅ **{kraut}** × **{menge}** (frisch) zu **{lager}** hinzugefügt.", ephemeral=True)

@bot.tree.command(name="kl_entnehmen", description="Entnimmt Kräuter aus dem KL (nimmt frisch zuerst, dann getrocknet).")
@app_commands.describe(lager="Name des KL Lagers", kraut="Kraut", menge="Menge")
@app_commands.autocomplete(lager=ac_lager_kl, kraut=ac_kl_kraut)
async def kl_entnehmen(interaction: discord.Interaction, lager: str, kraut: str, menge: int):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    if menge <= 0:
        await interaction.response.send_message("Menge muss > 0 sein.", ephemeral=True)
        return

    try:
        storage_id, owner_role_id = await resolve_storage(interaction, TYPE_KL, lager)
        assert_storage_access(interaction.user, owner_role_id)
    except (ValueError, PermissionError) as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    if not is_allowed(kraut, ALLOWED_KL_SET):
        await interaction.response.send_message("❌ Dieses Kraut ist nicht erlaubt.", ephemeral=True)
        return
    kraut = canonical(kraut, ALLOWED_KL_SET)

    await kl_apply_drying_if_enabled(interaction.guild.id, storage_id)
    removed = await bot.db.kl_take_herb(storage_id, kraut, menge)
    await interaction.response.send_message(f"✅ Entnommen: **{kraut}** × **{removed}** aus **{lager}**.", ephemeral=True)

@bot.tree.command(name="kl_vorraete", description="Zeigt die Vorräte (Sets/Mixes) im KL an.")
@app_commands.describe(lager="Name des KL Lagers")
@app_commands.autocomplete(lager=ac_lager_kl)
async def kl_vorraete(interaction: discord.Interaction, lager: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return

    try:
        storage_id, owner_role_id = await resolve_storage(interaction, TYPE_KL, lager)
        assert_storage_access(interaction.user, owner_role_id)
    except (ValueError, PermissionError) as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    items = await bot.db.pantry_list(storage_id)
    if not items:
        await interaction.response.send_message("Vorräte sind leer.", ephemeral=True)
        return
    lines = [f"• **{n}**: {a}" for n, a in items]
    await interaction.response.send_message("📦 **Vorräte:**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="kl_herstellen", description="Stellt ein Rezept her: zieht Kräuter ab und legt das Ergebnis in Vorräte.")
@app_commands.describe(lager="Name des KL Lagers", rezept="Rezeptname", menge="Wie oft herstellen")
@app_commands.autocomplete(lager=ac_lager_kl, rezept=ac_kl_rezept)
async def kl_herstellen(interaction: discord.Interaction, lager: str, rezept: str, menge: int):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Nur auf einem Server nutzbar.", ephemeral=True)
        return
    if menge <= 0:
        await interaction.response.send_message("Menge muss > 0 sein.", ephemeral=True)
        return

    try:
        storage_id, owner_role_id = await resolve_storage(interaction, TYPE_KL, lager)
        assert_storage_access(interaction.user, owner_role_id)
    except (ValueError, PermissionError) as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    if rezept not in KL_RECIPES:
        await interaction.response.send_message("Rezept nicht gefunden. Nutze /rezepte_anzeigen.", ephemeral=True)
        return

    await kl_apply_drying_if_enabled(interaction.guild.id, storage_id)

    recipe = KL_RECIPES[rezept]
    # Check availability
    missing = []
    for herb_name, base_qty in recipe.items():
        if not is_allowed(herb_name, ALLOWED_KL_SET):
            missing.append(f"{herb_name} (nicht erlaubt in allowed_kl_herbs)")
            continue
        herb_key = norm_key(herb_name)
        need = int(base_qty) * menge
        have = await bot.db.kl_total_available(storage_id, herb_key)
        if have < need:
            missing.append(f"{herb_name}: fehlt {need - have} (braucht {need}, hat {have})")

    if missing:
        await interaction.response.send_message("❌ Nicht genug Zutaten:\n- " + "\n- ".join(missing), ephemeral=True)
        return

    # Subtract ingredients
    for herb_name, base_qty in recipe.items():
        herb_canon = canonical(herb_name, ALLOWED_KL_SET)
        need = int(base_qty) * menge
        removed = await bot.db.kl_take_herb(storage_id, herb_canon, need)
        if removed != need:
            await interaction.response.send_message("❌ Unerwarteter Fehler beim Abziehen der Zutaten.", ephemeral=True)
            return

    # Add to pantry
    await bot.db.pantry_add(storage_id, rezept, menge, interaction.user.id)
    await interaction.response.send_message(f"✅ **{rezept}** × **{menge}** hergestellt und zu Vorräten hinzugefügt.", ephemeral=True)

# =========================
# Events
# =========================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# =========================
# Run
# =========================

bot.run(TOKEN)
