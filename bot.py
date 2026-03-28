import os
import re
import httpx
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
from pyrogram import Client, filters, idle
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# --- Environment Variables (Set these in Render Environment) ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
MONGO_URI = os.environ.get("MONGO_URI")
EARNKARO_KEY = os.environ.get("EARNKARO_KEY") 
SESSION_STRING = os.environ.get("SESSION_STRING")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0)) 

# --- Configuration ---
MY_CHANNEL_ID = '@lootchannel596' # Make sure to change this to your channel!
SOURCE_CHANNELS = [
    'MoneySaving_Deals', 
    'btrickdeals', 
    'Loot_shoppingdeals123', 
    'looters_hub', 
    'Flipshope', 
    'me'  # Allows you to test the bot by sending links in "Saved Messages"
]

# --- Global State ---
BOT_ACTIVE = True 

# --- Database Setup ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.deal_bot
posted_deals = db.posted_deals

# --- Initialize Telegram Userbot ---
# Created globally so it binds to the main event loop automatically
app = Client(
    "my_interceptor",
    session_string=SESSION_STRING,
    api_id=API_ID,
    api_hash=API_HASH
)

# --- Core Functions ---

async def unshorten_url(short_url):
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            response = await client.head(short_url)
            return str(response.url)
    except Exception as e:
        return short_url

def extract_product_id(url):
    url_lower = url.lower()
    if "amazon" in url_lower:
        match = re.search(r'/([A-Z0-9]{10})(?:[/?]|$)', url_lower, re.IGNORECASE)
        if match: return f"AMZN_{match.group(1)}"
    elif "flipkart" in url_lower:
        match = re.search(r'pid=([A-Z0-9]{16})', url_lower, re.IGNORECASE)
        if match: return f"FKRT_{match.group(1)}"
    clean = re.sub(r'(&|\?)(tag|affid|cmpid|utm_[a-zA-Z0-9_]+)=[^&]*', '', url_lower)
    return clean

def extract_price(text):
    match = re.search(r'(?:₹|Rs\.?\s*)((?:\d+,?)+\d+)', text, re.IGNORECASE)
    if match:
        return int(match.group(1).replace(',', ''))
    return None

async def monetize_link(final_url):
    """EarnKaro API Integration"""
    api_url = "https://ekaro-api.affiliaters.in/api/converter/public"
    payload = {
        "deal": final_url,
        "convert_option": "convert_only"
    }
    headers = {
        'Authorization': f'Bearer {EARNKARO_KEY}',
        'Content-Type': 'application/json'
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(api_url, headers=headers, json=payload)
            response_text = response.text
            
            urls = re.findall(r'(https?://[^\s"\'}]+)', response_text)
            if urls:
                return urls[0] 
            return final_url 
    except Exception as e:
        print(f"EarnKaro API failed: {e}")
        return final_url

async def wipe_database_at_3am():
    print("💀 3:00 AM IST: Running Smart Database Cleanup...")
    twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
    result = await posted_deals.delete_many({"posted_at": {"$lt": twenty_four_hours_ago}})
    print(f"Cleanup complete. Deleted {result.deleted_count} old deals.")

# --- ADMIN PANEL COMMANDS ---

@app.on_message((filters.me | filters.user(ADMIN_ID)) & filters.command("start"))
async def admin_panel(client, message):
    panel_text = (
        "🔥 **BOT IS ALIVE** 🔥\n\n"
        "**Admin Command Center:**\n"
        "🟢 `/on` - Turn Bot ON\n"
        "🔴 `/off` - Turn Bot OFF\n"
        "📊 `/stats` - View 24h Post Stats\n\n"
        f"**Current Status:** {'🟢 ONLINE' if BOT_ACTIVE else '🔴 OFFLINE'}"
    )
    await message.reply_text(panel_text)

@app.on_message((filters.me | filters.user(ADMIN_ID)) & filters.command("on"))
async def turn_bot_on(client, message):
    global BOT_ACTIVE
    if not BOT_ACTIVE:
        BOT_ACTIVE = True
        await message.reply_text("🟢 Bot is now ON and hunting for deals.")
        await client.send_message(MY_CHANNEL_ID, "Live🔥")
    else:
        await message.reply_text("Bot is already ON. 🟢")

@app.on_message((filters.me | filters.user(ADMIN_ID)) & filters.command("off"))
async def turn_bot_off(client, message):
    global BOT_ACTIVE
    BOT_ACTIVE = False
    await message.reply_text("🔴 Bot is now OFF. I am ignoring all deals.")

@app.on_message((filters.me | filters.user(ADMIN_ID)) & filters.command("stats"))
async def bot_stats(client, message):
    twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
    post_count = await posted_deals.count_documents({"posted_at": {"$gte": twenty_four_hours_ago}})
    await message.reply_text(f"📊 **Bot Stats**\n\nDeals posted in the last 24 hours: **{post_count}**")

# --- The Main Deal Handler ---
@app.on_message(filters.chat(SOURCE_CHANNELS) & filters.text)
async def deal_handler(client, message):
    global BOT_ACTIVE
    if not BOT_ACTIVE:
        return 
        
    text = message.text
    urls = re.findall(r'(https?://[^\s]+)', text)
    if not urls: return
    
    raw_url = urls[0]
    current_price = extract_price(text)
    final_url = await unshorten_url(raw_url)
    product_id = extract_product_id(final_url)
    
    existing_deal = await posted_deals.find_one({"product_id": product_id})
    current_time = datetime.utcnow()
    
    if existing_deal:
        stored_price = existing_deal.get("price")
        if current_price and stored_price and current_price < stored_price:
            await posted_deals.update_one(
                {"product_id": product_id}, 
                {"$set": {"price": current_price, "posted_at": current_time}}
            )
        else:
            return 
    else:
        await posted_deals.insert_one({
            "product_id": product_id, 
            "price": current_price, 
            "posted_at": current_time
        })
        
    monetized_link = await monetize_link(final_url)
    clean_text = re.sub(r'(https?://[^\s]+)', monetized_link, text)
    final_message = f"🚨 **NEW DEAL CAUGHT** 🚨\n\n{clean_text}"
    
    try:
        await client.send_message(MY_CHANNEL_ID, final_message)
    except Exception as e:
        print(f"Failed to send message: {e}")

# --- Render Health Check Server ---
async def health_check(request):
    return web.Response(text="💀 Bot is hunting deals on Render...")

async def start_web_server():
    app_web = web.Application()
    app_web.router.add_get('/', health_check)
    runner = web.AppRunner(app_web)
    await runner.setup()
    
    # Grabs Render's dynamic port assignment
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server running on port {port} for Render health check.")

# --- Background Task Loader ---
async def startup_tasks():
    # 1. Start Scheduler
    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Asia/Kolkata'))
    scheduler.add_job(wipe_database_at_3am, 'cron', hour=3, minute=0)
    scheduler.start()
    print("Scheduler armed for 3:00 AM IST wipes.")
    
    # 2. Start Web Server
    await start_web_server()
    
    # 3. Send "Live" test message
    try:
        # Give the bot a second to fully authenticate before sending
        await asyncio.sleep(2)
        await app.send_message(MY_CHANNEL_ID, "Live🔥")
        print("Startup message sent to channel.")
    except Exception as e:
        print(f"Could not send startup message: {e}")

# --- Master Boot Sequence ---
if __name__ == "__main__":
    print("Starting Telegram interceptor...")
    
    # Grab the main event loop
    loop = asyncio.get_event_loop()
    
    # Schedule our background tasks to run concurrently with Pyrogram
    loop.create_task(startup_tasks())
    
    # Let Pyrogram take over the main loop (Handles start, idle, and disconnect cleanly)
    app.run()
