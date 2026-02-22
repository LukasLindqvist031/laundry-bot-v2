"""
Aptus Laundry Discord Bot
=========================
Slash commands:
  /bookings  — show your current upcoming bookings
  /slots     — show available slots
  /book <n>  — book slot number N from /slots list
  /cancel <n>— cancel booking number N from /bookings list
  /run       — trigger the auto-logic immediately

The bot also runs the auto-logic every 30 minutes in the background
and DMs you when it makes a change.
"""

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta

import discord

# Force stdout to flush so Railway captures all logs immediately
sys.stdout.reconfigure(line_buffering=True)
from discord import app_commands
from playwright.async_api import async_playwright

# ── Config (set these as environment variables) ────────────────────────────────

APTUS_URL     = os.environ.get("APTUS_URL", "https://aptus.studentvagen.se/AptusPortalStyra")
APTUS_USER    = os.environ["APTUS_USERNAME"]
APTUS_PASS    = os.environ["APTUS_PASSWORD"]
CATEGORY_ID   = os.environ.get("CATEGORY_ID", "23")
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
OWNER_ID      = int(os.environ["DISCORD_OWNER_ID"])
GUILD_ID      = int(os.environ["DISCORD_GUILD_ID"])

MIN_HOUR       = 9    # never book before 09:00
CHECK_INTERVAL = 30   # minutes between auto-runs

# ── Swedish month lookup ───────────────────────────────────────────────────────

MONTHS_SV = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4,
    "maj": 5, "juni": 6, "juli": 7, "augusti": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

# ── Parsing helpers ────────────────────────────────────────────────────────────

def parse_card_datetime(time_label: str, date_label: str) -> datetime | None:
    m_time = re.match(r"(\d{2}):(\d{2})", time_label)
    if not m_time:
        return None
    hour, minute = int(m_time.group(1)), int(m_time.group(2))
    m_date = re.search(r"(\d{1,2})\s+(\w+)", date_label)
    if not m_date:
        return None
    day_num  = int(m_date.group(1))
    month    = MONTHS_SV.get(m_date.group(2).lower())
    if not month:
        return None
    now = datetime.now()
    try:
        dt = datetime(now.year, month, day_num, hour, minute)
    except ValueError:
        return None
    if dt < now - timedelta(days=60):
        dt = dt.replace(year=now.year + 1)
    return dt


def parse_slot_datetime(aria_label: str) -> datetime | None:
    m = re.search(
        r"den\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s+(\d{2}):(\d{2})",
        aria_label, re.IGNORECASE
    )
    if not m:
        return None
    month = MONTHS_SV.get(m.group(2).lower())
    if not month:
        return None
    return datetime(int(m.group(3)), month, int(m.group(1)), int(m.group(4)), int(m.group(5)))


def fmt(dt: datetime) -> str:
    """Human-friendly datetime string."""
    return dt.strftime("%a %d %b %Y  %H:%M")


# ── Aptus browser helpers ──────────────────────────────────────────────────────

async def make_page(pw):
    browser = await pw.chromium.launch(headless=True)
    page    = await browser.new_page()
    return browser, page


async def login(page) -> bool:
    try:
        print("[login] Navigating to login page...")
        await page.context.clear_cookies()
        await page.goto(f"{APTUS_URL}/Account/Login", wait_until="commit", timeout=20000)
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        print(f"[login] Page loaded: {page.url}")
        await page.fill('input[name="UserName"]', APTUS_USER)
        await page.fill('input[name="Password"]', APTUS_PASS)
        print("[login] Submitting...")
        await page.click('input[type="submit"], button[type="submit"]')
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)
        print(f"[login] Final URL: {page.url}")
        return "Login" not in page.url
    except Exception as e:
        print(f"[login] FAILED: {e}")
        return False


async def read_booking_page(page) -> dict:
    await page.goto(f"{APTUS_URL}/CustomerBooking", wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    cards    = await page.query_selector_all(".bookingCard")
    upcoming = []
    used_dts = []
    for card in cards:
        disabled = await card.get_attribute("data-disabled")
        divs     = await card.query_selector_all(":scope > div")
        texts    = [t for t in [(await d.inner_text()).strip() for d in divs] if t]
        if len(texts) < 2:
            continue
        dt = parse_card_datetime(texts[0], texts[1])
        if disabled == "disabled":
            if dt:
                used_dts.append(dt)
        else:
            btn = await card.query_selector(".unbookButton")
            if btn and dt:
                upcoming.append({
                    "time_label": texts[0],
                    "date_label": texts[1],
                    "datetime":   dt,
                    "unbook_id":  await btn.get_attribute("id"),
                })
    upcoming.sort(key=lambda b: b["datetime"])
    return {"upcoming": upcoming, "last_used_dt": max(used_dts) if used_dts else None}


async def get_available_slots(page) -> list[dict]:
    url = f"{APTUS_URL}/CustomerBooking/FirstAvailable?categoryId={CATEGORY_ID}&firstX=10"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    buttons = await page.query_selector_all("button.bookButton, button.bookButtonFirstAvailable")
    slots, seen = [], set()
    for btn in buttons:
        aria    = await btn.get_attribute("aria-label") or ""
        onclick = await btn.get_attribute("onclick") or ""
        dt      = parse_slot_datetime(aria)
        if dt and dt.hour >= MIN_HOUR and dt not in seen:
            seen.add(dt)
            slots.append({"datetime": dt, "aria_label": aria, "onclick": onclick})
    slots.sort(key=lambda s: s["datetime"])
    return slots


async def book_slot(page, slot: dict) -> bool:
    m = re.search(r"DoBooking\('([^']+)'", slot["onclick"])
    if not m:
        return False
    await page.goto(f"https://aptus.studentvagen.se{m.group(1)}", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    return True


async def cancel_booking(page, booking: dict) -> bool:
    await page.goto(f"{APTUS_URL}/CustomerBooking/Unbook/{booking['unbook_id']}", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    for sel in ["button:has-text('Avboka')", "button:has-text('Ja')", "input[value='Avboka']"]:
        btn = await page.query_selector(sel)
        if btn:
            await btn.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)
            break
    return True


# ── Auto-logic (same rules as booking_logic.py) ───────────────────────────────

async def run_auto_logic() -> list[str]:
    """
    Runs the full booking logic headlessly.
    Returns a list of action strings describing what was done (empty = nothing changed).
    """
    actions = []
    async with async_playwright() as pw:
        browser, page = await make_page(pw)
        try:
            if not await login(page):
                return ["❌ Login failed during auto-run"]

            state        = await read_booking_page(page)
            upcoming     = state["upcoming"]
            last_used_dt = state["last_used_dt"] or datetime.now()
            slots        = await get_available_slots(page)

            # ── Ensure 2 bookings ───────────────────────────────────────────
            if len(upcoming) < 2:
                needed       = 2 - len(upcoming)
                reference_dt = upcoming[-1]["datetime"] if upcoming else last_used_dt
                booked       = 0
                for slot in slots:
                    if booked >= needed:
                        break
                    if (slot["datetime"] - reference_dt).days < 7:
                        continue
                    if await book_slot(page, slot):
                        actions.append(f"📌 Booked new slot: **{fmt(slot['datetime'])}**")
                        booked      += 1
                        reference_dt = slot["datetime"]

                # Re-read after booking
                state    = await read_booking_page(page)
                upcoming = state["upcoming"]

            # ── Try to move first booking earlier ───────────────────────────
            if upcoming:
                first = upcoming[0]
                for slot in slots:
                    if slot["datetime"] >= first["datetime"]:
                        break
                    if (slot["datetime"] - last_used_dt).days < 7:
                        continue
                    # Earlier valid slot found
                    old_label = f"{first['time_label']} {first['date_label']}"
                    if await cancel_booking(page, first):
                        if await book_slot(page, slot):
                            actions.append(
                                f"🔄 Moved first booking earlier:\n"
                                f"  ~~{old_label}~~ → **{fmt(slot['datetime'])}**"
                            )
                    break
        finally:
            await browser.close()
    return actions


# ── Discord bot ───────────────────────────────────────────────────────────────

intents         = discord.Intents.default()
client          = discord.Client(intents=intents)
tree            = app_commands.CommandTree(client)

# Temporary cache so /book N and /cancel N work without re-scraping
_slots_cache:    list[dict] = []
_bookings_cache: list[dict] = []


async def dm_owner(message: str):
    """Send a DM to the bot owner."""
    user = await client.fetch_user(OWNER_ID)
    await user.send(message)


# ── /bookings ─────────────────────────────────────────────────────────────────

@tree.command(name="bookings", description="Show your current upcoming laundry bookings")
async def cmd_bookings(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("⛔ Not authorised.", ephemeral=True)
        return
    print(f"[/bookings] Received from {interaction.user.id}, deferring...")
    await interaction.response.defer(ephemeral=True)
    print("[/bookings] Deferred, launching browser...")

    async with async_playwright() as pw:
        browser, page = await make_page(pw)
        try:
            print("[/bookings] Logging in...")
            if not await login(page):
                print("[/bookings] Login failed")
                await interaction.followup.send("❌ Login failed.", ephemeral=True)
                return
            print("[/bookings] Logged in, reading page...")
            state = await read_booking_page(page)
            print("[/bookings] Done reading page")
        finally:
            await browser.close()

    global _bookings_cache
    _bookings_cache = state["upcoming"]
    upcoming        = state["upcoming"]
    last_used_dt    = state["last_used_dt"]

    if not upcoming:
        msg = "📭 **No upcoming bookings.**"
    else:
        lines = ["📅 **Upcoming bookings:**"]
        for i, b in enumerate(upcoming, 1):
            lines.append(f"  `{i}.` {b['time_label']}  {b['date_label']}")
        msg = "\n".join(lines)

    if last_used_dt:
        msg += f"\n\n🧺 Last laundry done: {fmt(last_used_dt)}"

    await interaction.followup.send(msg, ephemeral=True)


# ── /slots ────────────────────────────────────────────────────────────────────

@tree.command(name="slots", description="Show available laundry slots")
async def cmd_slots(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("⛔ Not authorised.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    async with async_playwright() as pw:
        browser, page = await make_page(pw)
        try:
            if not await login(page):
                await interaction.followup.send("❌ Login failed.", ephemeral=True)
                return
            slots = await get_available_slots(page)
        finally:
            await browser.close()

    global _slots_cache
    _slots_cache = slots

    if not slots:
        await interaction.followup.send("😔 No available slots found.", ephemeral=True)
        return

    lines = ["🗓️ **Available slots** (use `/book <number>` to book one):"]
    for i, s in enumerate(slots, 1):
        lines.append(f"  `{i}.` {fmt(s['datetime'])}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ── /book ─────────────────────────────────────────────────────────────────────

@tree.command(name="book", description="Book a slot by number (run /slots first)")
@app_commands.describe(number="Slot number from the /slots list")
async def cmd_book(interaction: discord.Interaction, number: int):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("⛔ Not authorised.", ephemeral=True)
        return

    if not _slots_cache:
        await interaction.response.send_message("Run `/slots` first to see available slots.", ephemeral=True)
        return
    if number < 1 or number > len(_slots_cache):
        await interaction.response.send_message(f"Invalid number. Choose between 1 and {len(_slots_cache)}.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    slot = _slots_cache[number - 1]

    async with async_playwright() as pw:
        browser, page = await make_page(pw)
        try:
            if not await login(page):
                await interaction.followup.send("❌ Login failed.", ephemeral=True)
                return
            success = await book_slot(page, slot)
        finally:
            await browser.close()

    if success:
        await interaction.followup.send(f"✅ Booked: **{fmt(slot['datetime'])}**", ephemeral=True)
    else:
        await interaction.followup.send("❌ Booking failed.", ephemeral=True)


# ── /cancel ───────────────────────────────────────────────────────────────────

@tree.command(name="cancel", description="Cancel a booking by number (run /bookings first)")
@app_commands.describe(number="Booking number from the /bookings list")
async def cmd_cancel(interaction: discord.Interaction, number: int):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("⛔ Not authorised.", ephemeral=True)
        return

    if not _bookings_cache:
        await interaction.response.send_message("Run `/bookings` first to see your bookings.", ephemeral=True)
        return
    if number < 1 or number > len(_bookings_cache):
        await interaction.response.send_message(f"Invalid number. Choose between 1 and {len(_bookings_cache)}.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    booking = _bookings_cache[number - 1]

    async with async_playwright() as pw:
        browser, page = await make_page(pw)
        try:
            if not await login(page):
                await interaction.followup.send("❌ Login failed.", ephemeral=True)
                return
            success = await cancel_booking(page, booking)
        finally:
            await browser.close()

    if success:
        await interaction.followup.send(
            f"✅ Cancelled: **{booking['time_label']}  {booking['date_label']}**", ephemeral=True
        )
    else:
        await interaction.followup.send("❌ Cancellation failed.", ephemeral=True)


# ── /run ──────────────────────────────────────────────────────────────────────

@tree.command(name="run", description="Trigger the auto-booking logic now")
async def cmd_run(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("⛔ Not authorised.", ephemeral=True)
        return
    await interaction.response.send_message("⚙️ Running auto-logic…", ephemeral=True)
    actions = await run_auto_logic()
    if actions:
        await dm_owner("⚙️ **Auto-run result:**\n" + "\n".join(actions))
    else:
        await dm_owner("⚙️ **Auto-run:** nothing to change, all good!")


# ── Background scheduler ──────────────────────────────────────────────────────

async def scheduler():
    """Runs the auto-logic every CHECK_INTERVAL minutes and DMs on changes."""
    await client.wait_until_ready()
    print(f"[Scheduler] Started — running every {CHECK_INTERVAL} min")
    while not client.is_closed():
        await asyncio.sleep(CHECK_INTERVAL * 60)
        print("[Scheduler] Running auto-logic...")
        try:
            actions = await run_auto_logic()
            if actions:
                await dm_owner("🤖 **Auto-booking update:**\n" + "\n".join(actions))
        except Exception as e:
            print(f"[Scheduler] Error: {e}")


# ── Bot startup ───────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"[Bot] Logged in as {client.user} — slash commands synced to guild {GUILD_ID}")
    await dm_owner(
        "🤖 **Laundry bot is online!**\n"
        "Commands: `/bookings` `/slots` `/book <n>` `/cancel <n>` `/run`"
    )


async def main():
    async with client:
        client.loop.create_task(scheduler())
        await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
