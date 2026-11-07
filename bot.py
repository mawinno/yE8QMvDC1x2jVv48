"""
Cipher Hub Discord bot — ใช้ฐานข้อมูล Postgres เดียวกับเว็บ (ตาราง kx_*)
คีย์เดิม (CIPHERHUB-XXXX-XXXX-XXXX) ใช้ต่อได้ทันที และ API เดิมบน Vercel ยังตรวจคีย์ตามปกติ
บอทเพิ่มแค่ตาราง kx_maps (เก็บสคริปต์ของแต่ละแมพ) โดยใช้ชื่อแมพ = label ของคีย์
"""
import io
import os
import re
import secrets
import time

import asyncpg
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
GUILD_ID = os.getenv("GUILD_ID")
BRAND = os.getenv("BRAND", "CIPHER HUB")


def _ids(name, default):
    return {int(x) for x in os.getenv(name, default).replace(" ", "").split(",") if x}


# ยศที่ใช้คำสั่งแอดมินได้ / ยศผู้ซื้อ (ใส่หลายยศคั่นด้วยคอมมาใน .env ได้)
DEV_ROLES = _ids("DEV_ROLE_IDS", "1565727026294427789")
BUYER_ROLES = _ids("BUYER_ROLE_IDS", "1565727001724198942")
# 1 = ต้องมียศ Buyer ก่อนถึงจะกด Redeem ได้ (ปกติ 0 เพราะคนซื้อใหม่ยังไม่มียศ บอทจะให้ยศหลัง Redeem สำเร็จ)
REDEEM_NEEDS_ROLE = os.getenv("REDEEM_NEEDS_ROLE", "0") == "1"


def has_role(member, roles):
    return any(r.id in roles for r in getattr(member, "roles", []))


def is_dev(member):
    return has_role(member, DEV_ROLES)


def is_buyer(member):
    return is_dev(member) or has_role(member, BUYER_ROLES)


async def deny_buyer(itx: discord.Interaction):
    await itx.response.send_message(
        "ฟีเจอร์นี้สำหรับผู้ที่ซื้อสคริปต์แล้วเท่านั้น (ต้องมียศ Buyer)\n"
        "ถ้าคุณซื้อแล้ว กด **Redeem Key** เพื่อผูกคีย์ แล้วบอทจะให้ยศให้", ephemeral=True)

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # เหมือนเว็บ (ตัด 0/O/1/I)
KEY_RE = re.compile(r"^[A-Z0-9]{2,12}(-[A-Z0-9]{4}){3}$")

pool: asyncpg.Pool = None


def norm_key(k: str) -> str:
    return (k or "").strip().upper()


def rand_group(n=4):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def clean_prefix(p, fallback="CIPHERHUB"):
    v = re.sub(r"[^A-Z0-9]", "", (p or "").upper())[:12]
    return v if len(v) >= 2 else fallback


async def log_event(con, key_id, key_text, action, actor, detail=""):
    await con.execute(
        "INSERT INTO kx_logs(key_id,key_text,action,actor,detail) VALUES($1,$2,$3,$4,$5)",
        key_id, key_text, action, actor, detail[:300])


async def get_settings():
    rows = await pool.fetch("SELECT key,value FROM kx_settings")
    return {r["key"]: r["value"] for r in rows}


async def get_policy():
    s = await get_settings()

    def n(k, d):
        try:
            x = int(s.get(k, d))
            return x if x >= 0 else d
        except ValueError:
            return d

    return {
        "enabled": s.get("user_reset_enabled", "1") != "0",
        "cooldown_h": n("reset_cooldown_hours", 24),
        "max": n("reset_max", 5),
        "prefix": clean_prefix(s.get("default_prefix", "CIPHERHUB")),
    }


def status_of(r):
    if r["revoked"]:
        return "ถูกระงับ"
    if r["expires_at"] and r["expires_at"].timestamp() <= time.time():
        return "หมดอายุ"
    if not r["activated_at"]:
        return "ยังไม่ได้ใช้งาน"
    return "ตลอดชีพ" if r["seconds"] == 0 else "ใช้งานได้"


def ts(dt, style="R"):
    return f"<t:{int(dt.timestamp())}:{style}>"


# ------------------------------------------------------------- actions -----
async def claim_key(user_id: int, raw: str):
    key = norm_key(raw)
    if not KEY_RE.match(key):
        return "รูปแบบคีย์ไม่ถูกต้อง"
    uid = str(user_id)
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow("SELECT * FROM kx_keys WHERE key=$1 FOR UPDATE", key)
            if not row:
                return "ไม่พบคีย์นี้"
            if row["revoked"]:
                return "คีย์นี้ถูกระงับ"
            if row["discord_id"] and row["discord_id"] != uid:
                return "คีย์นี้ถูกผูกกับบัญชีอื่นแล้ว"
            if row["discord_id"] == uid:
                return "คุณผูกคีย์นี้ไปแล้ว"
            await con.execute("UPDATE kx_keys SET discord_id=$2 WHERE id=$1", row["id"], uid)
            await log_event(con, row["id"], key, "claim", "user:" + uid, "ผูกคีย์ผ่านบอท Discord")
    return "ผูกคีย์กับบัญชีของคุณเรียบร้อย"


async def reset_hwid(key: str, owner_id: int = None, force=False, actor="bot"):
    """คืนข้อความผลลัพธ์ — ใช้นโยบายเดียวกับเว็บ (เปิด/ปิด, คูลดาวน์, จำนวนครั้งสูงสุด)"""
    pol = await get_policy()
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow("SELECT * FROM kx_keys WHERE key=$1 FOR UPDATE", key)
            if not row or (owner_id and row["discord_id"] != str(owner_id)):
                return "ไม่พบคีย์ในบัญชีของคุณ"
            if row["revoked"]:
                return "คีย์ถูกระงับ"
            if not row["hwid"]:
                return "คีย์นี้ยังไม่ได้ผูกกับเครื่องใด"
            if not force:
                if not pol["enabled"]:
                    return "ผู้ดูแลปิดฟีเจอร์รีเซ็ต HWID ไว้"
                if pol["max"] > 0 and row["hwid_resets"] >= pol["max"]:
                    return f"ใช้สิทธิ์รีเซ็ตครบ {pol['max']} ครั้งแล้ว"
                last = row["last_reset_at"]
                if last and pol["cooldown_h"]:
                    nxt = last.timestamp() + pol["cooldown_h"] * 3600
                    if nxt > time.time():
                        return f"รีเซ็ตได้อีกครั้ง <t:{int(nxt)}:R>"
            await con.execute(
                """UPDATE kx_keys SET hwid=NULL, hwid_bound_at=NULL,
                          hwid_resets = CASE WHEN $2 THEN hwid_resets+1 ELSE hwid_resets END,
                          last_reset_at = CASE WHEN $2 THEN now() ELSE last_reset_at END
                    WHERE id=$1""", row["id"], not force)
            await log_event(con, row["id"], key, "hwid_reset", actor,
                            "รีเซ็ต HWID" if force else "ผู้ใช้รีเซ็ต HWID เองผ่านบอท")
    return "รีเซ็ต HWID เรียบร้อย เปิดสคริปต์บนเครื่องใหม่ได้เลย"


async def user_script_keys(uid: int):
    return await pool.fetch(
        """SELECT k.*, m.name AS map_name, m.script FROM kx_keys k
             JOIN kx_maps m ON m.label = k.label
            WHERE k.discord_id=$1 AND NOT k.revoked
              AND (k.expires_at IS NULL OR k.expires_at > now())""", str(uid))


async def user_keys(uid: int):
    return await pool.fetch(
        """SELECT * FROM kx_keys WHERE discord_id=$1 AND NOT revoked
              AND (expires_at IS NULL OR expires_at > now())""", str(uid))


# -------------------------------------------------------------- UI ---------
async def send_script(itx: discord.Interaction, key: str):
    row = await pool.fetchrow(
        """SELECT k.*, m.name AS map_name, m.script FROM kx_keys k
             JOIN kx_maps m ON m.label=k.label
            WHERE k.key=$1 AND k.discord_id=$2 AND NOT k.revoked
              AND (k.expires_at IS NULL OR k.expires_at > now())""", key, str(itx.user.id))
    if not row:
        return await itx.followup.send("คีย์ใช้งานไม่ได้", ephemeral=True)
    head = f"**{row['map_name']}**\n"
    text = row["script"]
    if len(text) < 1700:
        await itx.followup.send(f"{head}```lua\n{text}\n```", ephemeral=True)
    else:
        f = discord.File(io.BytesIO(text.encode()), filename="script.lua")
        await itx.followup.send(head, file=f, ephemeral=True)


async def do_reset(itx: discord.Interaction, key: str):
    msg = await reset_hwid(key, owner_id=itx.user.id, actor="user:" + str(itx.user.id))
    await itx.followup.send(msg, ephemeral=True)


class PickKey(discord.ui.View):
    def __init__(self, rows, action):
        super().__init__(timeout=120)
        self.action = action
        opts = []
        for r in rows[:25]:
            name = (r["label"] or "คีย์")[:100]
            opts.append(discord.SelectOption(label=name, description=r["key"][:14] + "…", value=r["key"]))
        self.sel = discord.ui.Select(placeholder="เลือกคีย์/แมพ", options=opts)
        self.sel.callback = self.on_pick
        self.add_item(self.sel)

    async def on_pick(self, itx: discord.Interaction):
        await itx.response.defer(ephemeral=True)
        await self.action(itx, self.sel.values[0])


async def pick_or_run(itx: discord.Interaction, action, fetcher, empty_msg):
    await itx.response.defer(ephemeral=True)
    rows = await fetcher(itx.user.id)
    if not rows:
        return await itx.followup.send(empty_msg, ephemeral=True)
    if len(rows) == 1:
        return await action(itx, rows[0]["key"])
    await itx.followup.send("เลือกคีย์ที่ต้องการ", view=PickKey(rows, action), ephemeral=True)


class RedeemModal(discord.ui.Modal, title="ผูกคีย์กับบัญชี Discord"):
    key = discord.ui.TextInput(label="คีย์", placeholder="CIPHERHUB-XXXX-XXXX-XXXX", max_length=64)

    async def on_submit(self, itx: discord.Interaction):
        await itx.response.defer(ephemeral=True)
        msg = await claim_key(itx.user.id, self.key.value)
        if msg.startswith("ผูกคีย์กับบัญชี") or msg.startswith("คุณผูกคีย์นี้ไปแล้ว"):
            role = itx.guild.get_role(next(iter(BUYER_ROLES))) if itx.guild and BUYER_ROLES else None
            if role and role not in getattr(itx.user, "roles", []):
                try:
                    await itx.user.add_roles(role, reason="Redeem key สำเร็จ")
                    msg += f"\nคุณได้รับยศ {role.mention} แล้ว"
                except discord.HTTPException:
                    msg += "\n(บอทให้ยศอัตโนมัติไม่ได้ กรุณาแจ้งแอดมิน)"
        await itx.followup.send(msg, ephemeral=True)


class Panel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Redeem Key", style=discord.ButtonStyle.success, custom_id="panel:redeem")
    async def redeem(self, itx: discord.Interaction, _):
        if REDEEM_NEEDS_ROLE and not is_buyer(itx.user):
            return await deny_buyer(itx)
        await itx.response.send_modal(RedeemModal())

    @discord.ui.button(label="Get Script", style=discord.ButtonStyle.primary, custom_id="panel:script")
    async def script(self, itx: discord.Interaction, _):
        if not is_buyer(itx.user):
            return await deny_buyer(itx)
        await pick_or_run(itx, send_script, user_script_keys,
                          "ไม่พบคีย์ที่ใช้งานได้พร้อมสคริปต์ กด Redeem Key ก่อน หรือแอดมินยังไม่ได้ตั้งสคริปต์ให้แมพของคีย์คุณ")

    @discord.ui.button(label="Reset HWID", style=discord.ButtonStyle.danger, custom_id="panel:reset")
    async def reset(self, itx: discord.Interaction, _):
        if not is_buyer(itx.user):
            return await deny_buyer(itx)
        await pick_or_run(itx, do_reset, user_keys, "คุณยังไม่มีคีย์ที่ใช้งานได้ กด Redeem Key ก่อนนะ")


# -------------------------------------------------------------- BOT --------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global pool
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
        await pool.execute(
            """CREATE TABLE IF NOT EXISTS kx_maps(
                 label TEXT PRIMARY KEY,
                 name TEXT NOT NULL,
                 script TEXT NOT NULL)""")
        self.add_view(Panel())
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"บอทออนไลน์: {self.user}")


bot = Bot()
admin = app_commands.check(lambda itx: is_dev(itx.user))  # เฉพาะยศ DEV


@bot.tree.error
async def on_app_error(itx: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        msg = "คำสั่งนี้สำหรับทีม DEV เท่านั้น"
    else:
        print("command error:", repr(error))
        msg = "เกิดข้อผิดพลาด ลองใหม่อีกครั้ง"
    if itx.response.is_done():
        await itx.followup.send(msg, ephemeral=True)
    else:
        await itx.response.send_message(msg, ephemeral=True)


@bot.tree.command(description="โพสต์แผงปุ่ม Redeem / Get Script / Reset HWID")
@admin
async def panel(itx: discord.Interaction):
    embed = discord.Embed(
        title=BRAND,
        description="**Redeem Key** ผูกคีย์ที่ซื้อมากับบัญชีนี้\n**Get Script** รับสคริปต์ของแมพที่คุณมีคีย์\n"
                    "**Reset HWID** ย้ายเครื่อง (ตามนโยบายที่ตั้งไว้ในหลังบ้าน)",
        color=0x3B82F6)
    await itx.channel.send(embed=embed, view=Panel())
    await itx.response.send_message("โพสต์แล้ว", ephemeral=True)


@bot.tree.command(description="เพิ่ม/แก้ไขสคริปต์ของแมพ (label ต้องตรงกับ label ของคีย์)")
@admin
@app_commands.describe(label="ชื่อแมพ = label ที่ใส่ตอนสร้างคีย์ (เช่น Sailueatarchiwa)",
                       name="ชื่อที่แสดงให้ผู้ใช้เห็น", script="โค้ดหรือ loadstring ของแมพนี้")
async def addmap(itx: discord.Interaction, label: str, name: str, script: str):
    await pool.execute(
        "INSERT INTO kx_maps VALUES($1,$2,$3) ON CONFLICT(label) DO UPDATE SET name=$2, script=$3",
        label.strip(), name, script)
    await itx.response.send_message(f"บันทึกแมพ `{label}` แล้ว", ephemeral=True)


@bot.tree.command(description="ลบแมพ")
@admin
async def delmap(itx: discord.Interaction, label: str):
    await pool.execute("DELETE FROM kx_maps WHERE label=$1", label)
    await itx.response.send_message("ลบแล้ว", ephemeral=True)


@bot.tree.command(description="ดูรายชื่อแมพ และจำนวนคีย์ของแต่ละแมพ")
@admin
async def maps(itx: discord.Interaction):
    rows = await pool.fetch(
        """SELECT m.label, m.name, count(k.id)::int AS n FROM kx_maps m
             LEFT JOIN kx_keys k ON k.label=m.label GROUP BY m.label, m.name ORDER BY m.label""")
    text = "\n".join(f"`{r['label']}` {r['name']} ({r['n']} คีย์)" for r in rows) or "ยังไม่มีแมพ"
    await itx.response.send_message(text, ephemeral=True)


@bot.tree.command(description="สร้างคีย์ใหม่ (เก็บลงฐานข้อมูลเดียวกับเว็บ)")
@admin
@app_commands.describe(label="ชื่อแมพ (ต้องตรงกับที่ตั้งใน /addmap)", days="จำนวนวัน (0 = ตลอดชีพ)",
                       qty="จำนวนคีย์ (สูงสุด 50)", prefix="คำนำหน้า (ไม่ใส่ = ใช้ค่าเริ่มต้นของเว็บ)",
                       start_on_issue="เริ่มนับเวลาตั้งแต่ออกคีย์ (ปกติเริ่มนับตอนใช้ครั้งแรก)")
async def genkey(itx: discord.Interaction, label: str, days: int = 30, qty: int = 1,
                 prefix: str = "", start_on_issue: bool = False):
    pol = await get_policy()
    pre = clean_prefix(prefix, pol["prefix"]) if prefix else pol["prefix"]
    qty = max(1, min(qty, 50))
    secs = max(0, days) * 86400
    actor = "bot:" + str(itx.user.id)
    out = []
    async with pool.acquire() as con:
        async with con.transaction():
            for _ in range(qty):
                for attempt in range(8):
                    k = f"{pre}-{rand_group()}-{rand_group()}-{rand_group()}"
                    r = await con.fetchrow(
                        """INSERT INTO kx_keys(key,label,seconds,start_mode,hwid_lock,created_by,activated_at,expires_at)
                           VALUES($1,$2,$3,$4,TRUE,$5,
                                  CASE WHEN $6 THEN now() END,
                                  CASE WHEN $6 AND $3::bigint > 0 THEN now() + make_interval(secs => $3::double precision) END)
                           ON CONFLICT (key) DO NOTHING RETURNING id,key""",
                        k, label.strip(), secs, "issue" if start_on_issue else "activate", actor, start_on_issue)
                    if r:
                        out.append(r["key"])
                        await log_event(con, r["id"], r["key"], "created", actor,
                                        f"{label} · {days} วัน" if days else f"{label} · ตลอดชีพ")
                        break
    has_map = await pool.fetchval("SELECT 1 FROM kx_maps WHERE label=$1", label.strip())
    warn = "" if has_map else f"\n⚠️ ยังไม่มีสคริปต์ของแมพ `{label}` ใช้ /addmap ก่อน ไม่งั้นปุ่ม Get Script จะไม่ส่งอะไร"
    await itx.response.send_message("```\n" + "\n".join(out) + "\n```" + warn, ephemeral=True)


@bot.tree.command(description="ดูข้อมูลคีย์")
@admin
async def keyinfo(itx: discord.Interaction, key: str):
    r = await pool.fetchrow("SELECT * FROM kx_keys WHERE key=$1", norm_key(key))
    if not r:
        return await itx.response.send_message("ไม่พบคีย์", ephemeral=True)
    owner = f"<@{r['discord_id']}>" if r["discord_id"] else "ยังไม่ผูกบัญชี"
    exp = ts(r["expires_at"]) if r["expires_at"] else ("ตลอดชีพ" if r["seconds"] == 0 else "ยังไม่เริ่มนับ")
    hw = (r["hwid"][:6] + "…" + r["hwid"][-4:]) if r["hwid"] and len(r["hwid"]) > 10 else (r["hwid"] or "-")
    rb = f"{r['roblox_name']} ({r['roblox_id']})" if r["roblox_id"] else "-"
    await itx.response.send_message(
        f"แมพ/label `{r['label'] or '-'}`\nสถานะ {status_of(r)}\nเจ้าของ {owner}\nหมดอายุ {exp}\n"
        f"HWID `{hw}`\nรีเซ็ตแล้ว {r['hwid_resets']} ครั้ง\nRoblox {rb}\nใช้งาน {r['uses']} ครั้ง",
        ephemeral=True)


@bot.tree.command(description="ดูคีย์ทั้งหมดของผู้ใช้")
@admin
async def userkeys(itx: discord.Interaction, user: discord.User):
    rows = await pool.fetch("SELECT * FROM kx_keys WHERE discord_id=$1 ORDER BY id DESC LIMIT 20", str(user.id))
    if not rows:
        return await itx.response.send_message("ผู้ใช้นี้ไม่มีคีย์", ephemeral=True)
    text = "\n".join(f"`{r['key']}` · {r['label'] or '-'} · {status_of(r)}" for r in rows)
    await itx.response.send_message(text, ephemeral=True)


@bot.tree.command(description="รีเซ็ต HWID ให้ผู้ใช้ (ข้ามนโยบาย ไม่นับโควต้า)")
@admin
async def resetuser(itx: discord.Interaction, key: str):
    msg = await reset_hwid(norm_key(key), force=True, actor="bot:" + str(itx.user.id))
    await itx.response.send_message(msg, ephemeral=True)


@bot.tree.command(description="ต่อ/ลดเวลาคีย์ (วัน ติดลบได้)")
@admin
async def extend(itx: discord.Interaction, key: str, days: int):
    k = norm_key(key)
    secs = days * 86400
    if secs == 0 or abs(secs) > 10 * 365 * 86400:
        return await itx.response.send_message("จำนวนวันไม่ถูกต้อง", ephemeral=True)
    async with pool.acquire() as con:
        async with con.transaction():
            r = await con.fetchrow("SELECT * FROM kx_keys WHERE key=$1 FOR UPDATE", k)
            if not r:
                return await itx.response.send_message("ไม่พบคีย์", ephemeral=True)
            if r["seconds"] == 0:
                return await itx.response.send_message("คีย์ตลอดชีพไม่ต้องต่อเวลา", ephemeral=True)
            if r["expires_at"]:
                await con.execute("UPDATE kx_keys SET expires_at = expires_at + make_interval(secs => $2::double precision) WHERE id=$1",
                                  r["id"], float(secs))
            else:
                await con.execute("UPDATE kx_keys SET seconds = GREATEST(60, seconds + $2) WHERE id=$1", r["id"], secs)
            await log_event(con, r["id"], k, "extend", "bot:" + str(itx.user.id), f"{days:+d} วัน")
    await itx.response.send_message("ปรับเวลาแล้ว", ephemeral=True)


@bot.tree.command(description="ระงับ/ปลดระงับคีย์ (ผู้เล่นที่เล่นอยู่จะโดนเตะจาก heartbeat)")
@admin
async def revoke(itx: discord.Interaction, key: str, revoked: bool = True):
    k = norm_key(key)
    async with pool.acquire() as con:
        async with con.transaction():
            r = await con.fetchrow("UPDATE kx_keys SET revoked=$2 WHERE key=$1 RETURNING id", k, revoked)
            if not r:
                return await itx.response.send_message("ไม่พบคีย์", ephemeral=True)
            await log_event(con, r["id"], k, "revoke" if revoked else "unrevoke", "bot:" + str(itx.user.id))
    await itx.response.send_message("อัปเดตแล้ว", ephemeral=True)


@bot.tree.command(description="ลบคีย์ทิ้งถาวร")
@admin
async def delkey(itx: discord.Interaction, key: str):
    r = await pool.execute("DELETE FROM kx_keys WHERE key=$1", norm_key(key))
    await itx.response.send_message("ลบแล้ว" if r.endswith("1") else "ไม่พบคีย์", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN or not DATABASE_URL:
        raise SystemExit("ตั้งค่า DISCORD_TOKEN และ DATABASE_URL ใน .env ก่อน")
    bot.run(TOKEN)