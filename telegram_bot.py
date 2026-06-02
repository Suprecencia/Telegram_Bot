import os
import logging
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
from supabase import create_client, Client
from tavily import TavilyClient
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.enums import TA_LEFT
import tempfile
import base64
import pytz
import httpx
import json
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_KEY"]
TAVILY_API_KEY    = os.environ["TAVILY_API_KEY"]
APIFY_API_KEY     = os.environ["APIFY_API_KEY"]
SALES_GROUP_ID      = -1003726241799
MANAGEMENT_GROUP_ID = -1003787876162
WIB                 = pytz.timezone("Asia/Jakarta")

# Kompetitor yang dipantau
COMPETITOR_ACCOUNTS = [
    "sol.et.terre",
    "flawlessdiamonds.id",
    "ladinjewellery",
    "azurrdiamonds"
]

# Hashtag yang dipantau
TRACKED_HASHTAGS = [
    "labgrowndiamond",
    "perhiasancustom",
    "diamondIndonesia",
    "engagementring",
    "jewelry"
]

# ── Clients ───────────────────────────────────────────────────────────────────
claude  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
tavily  = TavilyClient(api_key=TAVILY_API_KEY)

# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a highly capable personal assistant for a devout Christian entrepreneur
who owns a custom jewelry and lab-grown diamond business — the first of its kind in Indonesia.

Your role covers 4 areas:
1. **Jewelry & Diamond Expert** — lab-grown diamonds, custom jewelry design,
   GIA certifications, 4Cs, pricing strategy, Indonesian luxury jewelry market.

2. **Christian Faith Perspective** — When asked about life, decisions, struggles,
   or biblical topics, provide thoughtful scripture-grounded answers with grace.

3. **Content & Copywriting** — Instagram captions, marketing copy, product
   descriptions, email drafts, business ideas with elegant premium brand voice.

4. **General Smart Assistant** — sharp, organized, practical for everything else.

Language: Auto-detect. Reply in Bahasa Indonesia if they write in Indonesian,
English if they write in English. Match their dominant language if mixed.

Tone: Warm, intelligent, professional — like a trusted advisor who genuinely cares.

Special commands the user can use:
- If user says "SEARCH:" at the start → you will receive web search results to analyze
- If user says "BUATKAN PDF:" or "MAKE PDF:" → create content and it will be saved as PDF
- If user sends a photo → analyze it professionally (jewelry quality, design, etc.)
- If user says "SOSMED:" → you will receive social media data to analyze
"""

# ── Supabase Memory ───────────────────────────────────────────────────────────
def load_history(user_id: int, limit: int = 20) -> list:
    try:
        result = supabase.table("conversations") \
            .select("role, content") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
        messages = [{"role": r["role"], "content": r["content"]} for r in reversed(result.data)]
        return messages
    except Exception as e:
        logging.error(f"Load history error: {e}")
        return []

def save_message(user_id: int, role: str, content: str):
    try:
        supabase.table("conversations").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        logging.error(f"Save message error: {e}")

def clear_history(user_id: int):
    try:
        supabase.table("conversations").delete().eq("user_id", user_id).execute()
    except Exception as e:
        logging.error(f"Clear history error: {e}")

# ── PDF Generator ─────────────────────────────────────────────────────────────
def create_pdf(content: str, title: str = "Dokumen") -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(tmp.name, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title", parent=styles["Heading1"], fontSize=16, spaceAfter=20)
    body_style  = ParagraphStyle("Body",  parent=styles["Normal"],   fontSize=11, leading=18)

    story = [
        Paragraph(title, title_style),
        Spacer(1, 0.5*cm),
    ]
    for line in content.split("\n"):
        line = line.strip()
        if line:
            story.append(Paragraph(line.replace("**", "<b>").replace("**", "</b>"), body_style))
            story.append(Spacer(1, 0.2*cm))

    doc.build(story)
    return tmp.name

# ── Claude call ───────────────────────────────────────────────────────────────
def ask_claude(messages: list, extra_system: str = "") -> str:
    system = SYSTEM_PROMPT + ("\n\n" + extra_system if extra_system else "")
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=system,
        messages=messages
    )
    return response.content[0].text

# ── Apify Social Media Scraper ────────────────────────────────────────────────
async def scrape_instagram_profile(username: str) -> dict:
    """Scrape Instagram profile data via Apify"""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Run the actor
            run_response = await client.post(
                f"https://api.apify.com/v2/acts/apify~instagram-profile-scraper/runs",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"},
                json={
                    "usernames": [username],
                    "resultsLimit": 5  # Ambil 5 post terbaru saja (hemat kuota)
                }
            )
            run_data = run_response.json()
            run_id = run_data.get("data", {}).get("id")
            if not run_id:
                return {}

            # Tunggu selesai (max 30 detik)
            for _ in range(10):
                await asyncio.sleep(3)
                status_resp = await client.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}",
                    headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
                )
                status = status_resp.json().get("data", {}).get("status")
                if status == "SUCCEEDED":
                    break
                elif status in ["FAILED", "ABORTED"]:
                    return {}

            # Ambil hasilnya
            dataset_id = status_resp.json().get("data", {}).get("defaultDatasetId")
            result_resp = await client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
            )
            items = result_resp.json()
            return items[0] if items else {}

    except Exception as e:
        logging.error(f"Apify Instagram error for {username}: {e}")
        return {}

async def scrape_hashtag_tiktok(hashtag: str) -> list:
    """Scrape TikTok hashtag data via Apify"""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            run_response = await client.post(
                f"https://api.apify.com/v2/acts/clockworks~tiktok-scraper/runs",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"},
                json={
                    "hashtags": [hashtag],
                    "resultsPerPage": 5  # 5 video per hashtag (hemat kuota)
                }
            )
            run_data = run_response.json()
            run_id = run_data.get("data", {}).get("id")
            if not run_id:
                return []

            for _ in range(10):
                await asyncio.sleep(3)
                status_resp = await client.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}",
                    headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
                )
                status = status_resp.json().get("data", {}).get("status")
                if status == "SUCCEEDED":
                    break
                elif status in ["FAILED", "ABORTED"]:
                    return []

            dataset_id = status_resp.json().get("data", {}).get("defaultDatasetId")
            result_resp = await client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
            )
            return result_resp.json()

    except Exception as e:
        logging.error(f"Apify TikTok error for {hashtag}: {e}")
        return []

async def get_social_media_intel() -> str:
    """Kumpulkan data sosmed dari kompetitor + hashtag tren"""
    intel = ""

    # Scrape 2 kompetitor saja per hari (hemat kuota free tier)
    today_idx = datetime.now(WIB).day % len(COMPETITOR_ACCOUNTS)
    accounts_today = COMPETITOR_ACCOUNTS[today_idx:today_idx+2]

    intel += "\n=== DATA INSTAGRAM KOMPETITOR ===\n"
    for username in accounts_today:
        data = await scrape_instagram_profile(username)
        if data:
            followers = data.get("followersCount", "N/A")
            posts = data.get("postsCount", "N/A")
            bio = data.get("biography", "")
            latest_posts = data.get("latestPosts", [])
            intel += f"\n@{username}:\n"
            intel += f"  Followers: {followers} | Posts: {posts}\n"
            intel += f"  Bio: {bio[:100]}\n"
            if latest_posts:
                for p in latest_posts[:3]:
                    likes = p.get("likesCount", 0)
                    caption = p.get("caption", "")[:80]
                    intel += f"  📸 [{likes} likes] {caption}\n"
        else:
            intel += f"\n@{username}: Data tidak tersedia\n"

    # Scrape 2 hashtag TikTok
    intel += "\n=== TREN TIKTOK ===\n"
    for hashtag in TRACKED_HASHTAGS[:2]:
        videos = await scrape_hashtag_tiktok(hashtag)
        if videos:
            intel += f"\n#{hashtag}:\n"
            for v in videos[:3]:
                views = v.get("playCount", 0)
                likes = v.get("diggCount", 0)
                desc = v.get("text", "")[:80]
                intel += f"  🎵 [{views:,} views | {likes:,} likes] {desc}\n"
        else:
            intel += f"\n#{hashtag}: Data tidak tersedia\n"

    return intel

# ── Daily Report ──────────────────────────────────────────────────────────────
async def send_daily_report(app):
    today = datetime.now(WIB).strftime("%A, %d %B %Y")

    await app.bot.send_message(
        chat_id=MANAGEMENT_GROUP_ID,
        text="⏳ Sedang menyiapkan laporan pagi... mohon tunggu sebentar."
    )

    # ── Web search ───────────────────────────────────────────────────────────
    topics = [
        "harga emas hari ini Indonesia",
        "lab grown diamond tren harga 2025",
        "tren perhiasan fashion Indonesia terbaru",
        "kompetitor perhiasan diamond Indonesia",
        "tren jewelry Instagram TikTok Indonesia"
    ]
    search_results = ""
    for topic in topics:
        try:
            results = tavily.search(query=topic, max_results=3)
            for r in results.get("results", []):
                search_results += f"\n[{topic}]\n{r['title']}: {r['content'][:200]}\n"
        except Exception as e:
            logging.error(f"Search error for {topic}: {e}")

    # ── Social media intel ───────────────────────────────────────────────────
    try:
        social_data = await get_social_media_intel()
    except Exception as e:
        logging.error(f"Social media intel error: {e}")
        social_data = "Data sosmed tidak tersedia hari ini."

    # ── Management report ────────────────────────────────────────────────────
    mgmt_prompt = f"""
Hari ini: {today}

Berdasarkan data riset berikut, buatkan laporan pagi lengkap untuk tim MANAJEMEN Dikara 
(bisnis perhiasan custom & lab-grown diamond pertama di Indonesia).

Data riset web:
{search_results}

Data sosial media kompetitor & tren:
{social_data}

Format laporan (gunakan emoji, bahasa Indonesia, tone profesional):
🌅 SELAMAT PAGI - LAPORAN HARIAN DIKARA
📅 Tanggal

💰 UPDATE HARGA
- Harga emas hari ini
- Estimasi harga lab-grown diamond

📊 TREN PASAR
- Tren jewelry & diamond terkini
- Tren fashion yang relevan

📱 INTEL SOSMED
- Update kompetitor Instagram hari ini
- Tren TikTok yang relevan

🏆 KOMPETITOR
- Update singkat kompetitor

💡 REKOMENDASI STRATEGI
- 2-3 rekomendasi actionable untuk Dikara hari ini

✝️ FIRMAN PAGI
- Ayat Alkitab yang relevan + refleksi singkat
"""

    mgmt_report = ask_claude([{"role": "user", "content": mgmt_prompt}])

    # ── Sales report ─────────────────────────────────────────────────────────
    sales_prompt = f"""
Hari ini: {today}

Berdasarkan data riset berikut, buatkan pesan pagi singkat & motivatif untuk tim SALES Dikara.
Fokus pada insight yang langsung bisa dipakai untuk penawaran ke customer hari ini.

Data riset:
{search_results}

Data sosmed tren:
{social_data}

Format (bahasa Indonesia, singkat, semangat):
💎 GOOD MORNING DIKARA SALES TEAM!
📅 Tanggal

🔥 TREN HARI INI
- 2-3 tren yang relevan untuk customer

📱 SOSMED INSIGHT
- 1-2 konten/tren yang lagi viral, bisa dijadikan bahan ngobrol dengan customer

💡 IDE PENAWARAN HARI INI
- 2-3 ide konkret untuk ditawarkan ke customer

✝️ SEMANGAT PAGI
- Ayat singkat + kalimat motivasi
"""

    sales_report = ask_claude([{"role": "user", "content": sales_prompt}])

    # ── Send reports ─────────────────────────────────────────────────────────
    try:
        await app.bot.send_message(chat_id=MANAGEMENT_GROUP_ID, text=mgmt_report)
        await app.bot.send_message(chat_id=SALES_GROUP_ID, text=sales_report)
        logging.info("✅ Daily reports sent successfully")
    except Exception as e:
        logging.error(f"Send report error: {e}")

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Halo! Saya asisten pribadi kamu yang sudah di-upgrade!\n\n"
        "Kemampuan saya sekarang:\n"
        "💎 Jewelry & lab-grown diamond expert\n"
        "✝️ Christian faith & biblical perspective\n"
        "✍️ Content & copywriting\n"
        "🔍 Web search — ketik: SEARCH: [pertanyaan]\n"
        "📱 Sosmed intel — ketik: SOSMED: kompetitor/tren\n"
        "🖼️ Analisa foto — kirim foto langsung!\n"
        "📄 Buat PDF — ketik: BUATKAN PDF: [instruksi]\n"
        "💾 Memory permanen — saya ingat semua percakapan kita!\n\n"
        "Ketik /clear untuk reset memory.\nAda yang bisa saya bantu?"
    )

# ── /myid ─────────────────────────────────────────────────────────────────────
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    await update.message.reply_text(
        f"📋 Info ID:\n"
        f"👤 User ID kamu: `{user_id}`\n"
        f"💬 Chat ID ini: `{chat_id}`\n"
        f"📌 Tipe chat: {chat_type}",
        parse_mode="Markdown"
    )

# ── /testreport ───────────────────────────────────────────────────────────────
async def test_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != int(os.environ.get("OWNER_ID", 0)):
        await update.message.reply_text("⛔ Hanya owner yang bisa menjalankan ini.")
        return
    await update.message.reply_text("🔄 Mengirim test report ke kedua grup...")
    await send_daily_report(context.application)
    await update.message.reply_text("✅ Test report selesai!")

# ── /testsosmed ───────────────────────────────────────────────────────────────
async def test_sosmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != int(os.environ.get("OWNER_ID", 0)):
        await update.message.reply_text("⛔ Hanya owner yang bisa menjalankan ini.")
        return
    await update.message.reply_text("📱 Mengambil data sosmed... tunggu sekitar 1 menit ya.")
    try:
        data = await get_social_media_intel()
        await update.message.reply_text(f"✅ Data sosmed berhasil:\n\n{data[:3000]}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ── /clear ────────────────────────────────────────────────────────────────────
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_history(user_id)
    await update.message.reply_text("🧹 Memory dihapus! Kita mulai percakapan baru.")

# ── Photo handler ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    caption = update.message.caption or "Tolong analisa gambar ini."

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        path  = tempfile.mktemp(suffix=".jpg")
        await file.download_to_drive(path)

        with open(path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")

        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}},
                    {"type": "text", "text": caption}
                ]
            }]
        )
        reply = response.content[0].text
        save_message(user_id, "user", f"[Mengirim foto] {caption}")
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)

    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text("⚠️ Gagal menganalisa foto. Coba lagi ya.")

# ── Text handler ──────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    user_text = update.message.text.strip()
    chat_type = update.effective_chat.type

    if chat_type in ["group", "supergroup"]:
        is_mention = False
        is_reply_to_bot = False

        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "mention":
                    mentioned = user_text[entity.offset:entity.offset + entity.length]
                    if "Myhumbleservant_Vic_bot" in mentioned or "Dikara_asisstant_bot" in mentioned:
                        is_mention = True

        if update.message.reply_to_message:
            if update.message.reply_to_message.from_user.is_bot:
                is_reply_to_bot = True

        try:
            team = supabase.table("team_members").select("user_id").execute()
            team_ids = [str(m["user_id"]) for m in team.data]
        except:
            team_ids = []

        is_authorized = str(user_id) == os.environ.get("OWNER_ID") or str(user_id) in team_ids

        if not (is_mention or is_reply_to_bot) and not is_authorized:
            return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        # ── PDF request ───────────────────────────────────────────────────────
        if user_text.upper().startswith("BUATKAN PDF:") or user_text.upper().startswith("MAKE PDF:"):
            instruction = user_text.split(":", 1)[1].strip()
            history = load_history(user_id)
            history.append({"role": "user", "content": f"Buatkan konten lengkap untuk PDF tentang: {instruction}. Tulis dalam format yang rapi dengan judul dan paragraf."})
            content = ask_claude(history)

            title = instruction[:50]
            pdf_path = create_pdf(content, title)

            save_message(user_id, "user", user_text)
            save_message(user_id, "assistant", f"[PDF dibuat] {title}")

            await update.message.reply_text(f"📄 Membuat PDF: *{title}*...", parse_mode="Markdown")
            with open(pdf_path, "rb") as pdf_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=pdf_file,
                    filename=f"{title}.pdf"
                )
            return

        # ── Web search request ────────────────────────────────────────────────
        if user_text.upper().startswith("SEARCH:"):
            query = user_text.split(":", 1)[1].strip()
            await update.message.reply_text(f"🔍 Mencari: *{query}*...", parse_mode="Markdown")

            results = tavily.search(query=query, max_results=5)
            search_summary = "\n\n".join([
                f"**{r['title']}**\n{r['content'][:300]}..."
                for r in results.get("results", [])
            ])

            history = load_history(user_id)
            history.append({"role": "user", "content": f"Berdasarkan hasil pencarian web berikut, jawab pertanyaan: {query}\n\nHasil pencarian:\n{search_summary}"})
            reply = ask_claude(history, extra_system="You have been given real-time web search results. Analyze and summarize them clearly.")

            save_message(user_id, "user", user_text)
            save_message(user_id, "assistant", reply)
            await update.message.reply_text(reply)
            return

        # ── Social media request ──────────────────────────────────────────────
        if user_text.upper().startswith("SOSMED:"):
            query = user_text.split(":", 1)[1].strip()
            await update.message.reply_text("📱 Mengambil data sosmed... tunggu sekitar 1 menit ya.")

            social_data = await get_social_media_intel()

            history = load_history(user_id)
            history.append({"role": "user", "content": f"Berdasarkan data sosial media berikut, analisa dan jawab: {query}\n\nData sosmed:\n{social_data}"})
            reply = ask_claude(history, extra_system="You have been given real social media data. Analyze competitor activity, engagement rates, and trends for Dikara jewelry business.")

            save_message(user_id, "user", user_text)
            save_message(user_id, "assistant", reply)
            await update.message.reply_text(reply)
            return

        # ── Normal conversation ───────────────────────────────────────────────
        history = load_history(user_id)

        chat_id = update.effective_chat.id
        extra = ""
        if chat_id == SALES_GROUP_ID:
            extra = "PENTING: Selalu jawab dalam Bahasa Indonesia, apapun bahasa yang digunakan."

        history.append({"role": "user", "content": user_text})
        reply = ask_claude(history, extra_system=extra)

        save_message(user_id, "user", user_text)
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)

    except Exception as e:
        logging.error(f"Message error: {e}")
        await update.message.reply_text("⚠️ Ada error. Coba lagi ya!")

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    async def post_init(application):
        scheduler = AsyncIOScheduler(timezone=WIB)
        scheduler.add_job(
            send_daily_report,
import os
import logging
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
from supabase import create_client, Client
from tavily import TavilyClient
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.enums import TA_LEFT
import tempfile
import base64
import pytz
import httpx
import json
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_KEY"]
TAVILY_API_KEY    = os.environ["TAVILY_API_KEY"]
APIFY_API_KEY     = os.environ["APIFY_API_KEY"]
SALES_GROUP_ID      = -1003726241799
MANAGEMENT_GROUP_ID = -1003787876162
WIB                 = pytz.timezone("Asia/Jakarta")

# Kompetitor yang dipantau
COMPETITOR_ACCOUNTS = [
    "sol.et.terre",
    "flawlessdiamonds.id",
    "ladinjewellery",
    "azurrdiamonds"
]

# Hashtag yang dipantau
TRACKED_HASHTAGS = [
    "labgrowndiamond",
    "perhiasancustom",
    "diamondIndonesia",
    "engagementring",
    "jewelry"
]

# ── Clients ───────────────────────────────────────────────────────────────────
claude  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
tavily  = TavilyClient(api_key=TAVILY_API_KEY)

# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a highly capable personal assistant for a devout Christian entrepreneur
who owns a custom jewelry and lab-grown diamond business — the first of its kind in Indonesia.

Your role covers 4 areas:
1. **Jewelry & Diamond Expert** — lab-grown diamonds, custom jewelry design,
   GIA certifications, 4Cs, pricing strategy, Indonesian luxury jewelry market.

2. **Christian Faith Perspective** — When asked about life, decisions, struggles,
   or biblical topics, provide thoughtful scripture-grounded answers with grace.

3. **Content & Copywriting** — Instagram captions, marketing copy, product
   descriptions, email drafts, business ideas with elegant premium brand voice.

4. **General Smart Assistant** — sharp, organized, practical for everything else.

Language: Auto-detect. Reply in Bahasa Indonesia if they write in Indonesian,
English if they write in English. Match their dominant language if mixed.

Tone: Warm, intelligent, professional — like a trusted advisor who genuinely cares.

Special commands the user can use:
- If user says "SEARCH:" at the start → you will receive web search results to analyze
- If user says "BUATKAN PDF:" or "MAKE PDF:" → create content and it will be saved as PDF
- If user sends a photo → analyze it professionally (jewelry quality, design, etc.)
- If user says "SOSMED:" → you will receive social media data to analyze
"""

# ── Supabase Memory ───────────────────────────────────────────────────────────
def load_history(user_id: int, limit: int = 20) -> list:
    try:
        result = supabase.table("conversations") \
            .select("role, content") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
        messages = [{"role": r["role"], "content": r["content"]} for r in reversed(result.data)]
        return messages
    except Exception as e:
        logging.error(f"Load history error: {e}")
        return []

def save_message(user_id: int, role: str, content: str):
    try:
        supabase.table("conversations").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        logging.error(f"Save message error: {e}")

def clear_history(user_id: int):
    try:
        supabase.table("conversations").delete().eq("user_id", user_id).execute()
    except Exception as e:
        logging.error(f"Clear history error: {e}")

# ── PDF Generator ─────────────────────────────────────────────────────────────
def create_pdf(content: str, title: str = "Dokumen") -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(tmp.name, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title", parent=styles["Heading1"], fontSize=16, spaceAfter=20)
    body_style  = ParagraphStyle("Body",  parent=styles["Normal"],   fontSize=11, leading=18)

    story = [
        Paragraph(title, title_style),
        Spacer(1, 0.5*cm),
    ]
    for line in content.split("\n"):
        line = line.strip()
        if line:
            story.append(Paragraph(line.replace("**", "<b>").replace("**", "</b>"), body_style))
            story.append(Spacer(1, 0.2*cm))

    doc.build(story)
    return tmp.name

# ── Claude call ───────────────────────────────────────────────────────────────
def ask_claude(messages: list, extra_system: str = "") -> str:
    system = SYSTEM_PROMPT + ("\n\n" + extra_system if extra_system else "")
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=system,
        messages=messages
    )
    return response.content[0].text

# ── Apify Social Media Scraper ────────────────────────────────────────────────
async def scrape_instagram_profile(username: str) -> dict:
    """Scrape Instagram profile data via Apify"""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Run the actor
            run_response = await client.post(
                f"https://api.apify.com/v2/acts/apify~instagram-profile-scraper/runs",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"},
                json={
                    "usernames": [username],
                    "resultsLimit": 5  # Ambil 5 post terbaru saja (hemat kuota)
                }
            )
            run_data = run_response.json()
            run_id = run_data.get("data", {}).get("id")
            if not run_id:
                return {}

            # Tunggu selesai (max 30 detik)
            for _ in range(10):
                await asyncio.sleep(3)
                status_resp = await client.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}",
                    headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
                )
                status = status_resp.json().get("data", {}).get("status")
                if status == "SUCCEEDED":
                    break
                elif status in ["FAILED", "ABORTED"]:
                    return {}

            # Ambil hasilnya
            dataset_id = status_resp.json().get("data", {}).get("defaultDatasetId")
            result_resp = await client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
            )
            items = result_resp.json()
            return items[0] if items else {}

    except Exception as e:
        logging.error(f"Apify Instagram error for {username}: {e}")
        return {}

async def scrape_hashtag_tiktok(hashtag: str) -> list:
    """Scrape TikTok hashtag data via Apify"""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            run_response = await client.post(
                f"https://api.apify.com/v2/acts/clockworks~tiktok-scraper/runs",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"},
                json={
                    "hashtags": [hashtag],
                    "resultsPerPage": 5  # 5 video per hashtag (hemat kuota)
                }
            )
            run_data = run_response.json()
            run_id = run_data.get("data", {}).get("id")
            if not run_id:
                return []

            for _ in range(10):
                await asyncio.sleep(3)
                status_resp = await client.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}",
                    headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
                )
                status = status_resp.json().get("data", {}).get("status")
                if status == "SUCCEEDED":
                    break
                elif status in ["FAILED", "ABORTED"]:
                    return []

            dataset_id = status_resp.json().get("data", {}).get("defaultDatasetId")
            result_resp = await client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
            )
            return result_resp.json()

    except Exception as e:
        logging.error(f"Apify TikTok error for {hashtag}: {e}")
        return []

async def get_social_media_intel() -> str:
    """Kumpulkan data sosmed dari kompetitor + hashtag tren"""
    intel = ""

    # Scrape 2 kompetitor saja per hari (hemat kuota free tier)
    today_idx = datetime.now(WIB).day % len(COMPETITOR_ACCOUNTS)
    accounts_today = COMPETITOR_ACCOUNTS[today_idx:today_idx+2]

    intel += "\n=== DATA INSTAGRAM KOMPETITOR ===\n"
    for username in accounts_today:
        data = await scrape_instagram_profile(username)
        if data:
            followers = data.get("followersCount", "N/A")
            posts = data.get("postsCount", "N/A")
            bio = data.get("biography", "")
            latest_posts = data.get("latestPosts", [])
            intel += f"\n@{username}:\n"
            intel += f"  Followers: {followers} | Posts: {posts}\n"
            intel += f"  Bio: {bio[:100]}\n"
            if latest_posts:
                for p in latest_posts[:3]:
                    likes = p.get("likesCount", 0)
                    caption = p.get("caption", "")[:80]
                    intel += f"  📸 [{likes} likes] {caption}\n"
        else:
            intel += f"\n@{username}: Data tidak tersedia\n"

    # Scrape 2 hashtag TikTok
    intel += "\n=== TREN TIKTOK ===\n"
    for hashtag in TRACKED_HASHTAGS[:2]:
        videos = await scrape_hashtag_tiktok(hashtag)
        if videos:
            intel += f"\n#{hashtag}:\n"
            for v in videos[:3]:
                views = v.get("playCount", 0)
                likes = v.get("diggCount", 0)
                desc = v.get("text", "")[:80]
                intel += f"  🎵 [{views:,} views | {likes:,} likes] {desc}\n"
        else:
            intel += f"\n#{hashtag}: Data tidak tersedia\n"

    return intel

# ── Daily Report ──────────────────────────────────────────────────────────────
async def send_daily_report(app):
    today = datetime.now(WIB).strftime("%A, %d %B %Y")

    await app.bot.send_message(
        chat_id=MANAGEMENT_GROUP_ID,
        text="⏳ Sedang menyiapkan laporan pagi... mohon tunggu sebentar."
    )

    # ── Web search ───────────────────────────────────────────────────────────
    topics = [
        "harga emas hari ini Indonesia",
        "lab grown diamond tren harga 2025",
        "tren perhiasan fashion Indonesia terbaru",
        "kompetitor perhiasan diamond Indonesia",
        "tren jewelry Instagram TikTok Indonesia"
    ]
    search_results = ""
    for topic in topics:
        try:
            results = tavily.search(query=topic, max_results=3)
            for r in results.get("results", []):
                search_results += f"\n[{topic}]\n{r['title']}: {r['content'][:200]}\n"
        except Exception as e:
            logging.error(f"Search error for {topic}: {e}")

    # ── Social media intel ───────────────────────────────────────────────────
    try:
        social_data = await get_social_media_intel()
    except Exception as e:
        logging.error(f"Social media intel error: {e}")
        social_data = "Data sosmed tidak tersedia hari ini."

    # ── Management report ────────────────────────────────────────────────────
    mgmt_prompt = f"""
Hari ini: {today}

Berdasarkan data riset berikut, buatkan laporan pagi lengkap untuk tim MANAJEMEN Dikara 
(bisnis perhiasan custom & lab-grown diamond pertama di Indonesia).

Data riset web:
{search_results}

Data sosial media kompetitor & tren:
{social_data}

Format laporan (gunakan emoji, bahasa Indonesia, tone profesional):
🌅 SELAMAT PAGI - LAPORAN HARIAN DIKARA
📅 Tanggal

💰 UPDATE HARGA
- Harga emas hari ini
- Estimasi harga lab-grown diamond

📊 TREN PASAR
- Tren jewelry & diamond terkini
- Tren fashion yang relevan

📱 INTEL SOSMED
- Update kompetitor Instagram hari ini
- Tren TikTok yang relevan

🏆 KOMPETITOR
- Update singkat kompetitor

💡 REKOMENDASI STRATEGI
- 2-3 rekomendasi actionable untuk Dikara hari ini

✝️ FIRMAN PAGI
- Ayat Alkitab yang relevan + refleksi singkat
"""

    mgmt_report = ask_claude([{"role": "user", "content": mgmt_prompt}])

    # ── Sales report ─────────────────────────────────────────────────────────
    sales_prompt = f"""
Hari ini: {today}

Berdasarkan data riset berikut, buatkan pesan pagi singkat & motivatif untuk tim SALES Dikara.
Fokus pada tren dan insight yang langsung bisa dipakai untuk ngobrol dengan customer hari ini.

PENTING: Jangan sebutkan angka harga apapun (harga emas, harga diamond, estimasi harga, range harga, dll).
Fokus HANYA pada tren desain, gaya, dan insight pasar.

Data riset:
{search_results}

Data sosmed tren:
{social_data}

Format (bahasa Indonesia, singkat, semangat):
💎 GOOD MORNING DIKARA SALES TEAM!
📅 Tanggal

🔥 TREN HARI INI
- 2-3 tren desain/gaya yang relevan untuk customer

📱 SOSMED INSIGHT
- 1-2 konten/tren yang lagi viral, bisa dijadikan bahan ngobrol dengan customer

💡 IDE PENAWARAN HARI INI
- 2-3 ide konkret untuk ditawarkan ke customer (fokus pada gaya & tren, bukan harga)

✝️ SEMANGAT PAGI
- Ayat singkat + kalimat motivasi
"""

    sales_report = ask_claude([{"role": "user", "content": sales_prompt}])

    # ── Send reports ─────────────────────────────────────────────────────────
    try:
        await app.bot.send_message(chat_id=MANAGEMENT_GROUP_ID, text=mgmt_report)
        await app.bot.send_message(chat_id=SALES_GROUP_ID, text=sales_report)
        logging.info("✅ Daily reports sent successfully")
    except Exception as e:
        logging.error(f"Send report error: {e}")

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Halo! Saya asisten pribadi kamu yang sudah di-upgrade!\n\n"
        "Kemampuan saya sekarang:\n"
        "💎 Jewelry & lab-grown diamond expert\n"
        "✝️ Christian faith & biblical perspective\n"
        "✍️ Content & copywriting\n"
        "🔍 Web search — ketik: SEARCH: [pertanyaan]\n"
        "📱 Sosmed intel — ketik: SOSMED: kompetitor/tren\n"
        "🖼️ Analisa foto — kirim foto langsung!\n"
        "📄 Buat PDF — ketik: BUATKAN PDF: [instruksi]\n"
        "💾 Memory permanen — saya ingat semua percakapan kita!\n\n"
        "Ketik /clear untuk reset memory.\nAda yang bisa saya bantu?"
    )

# ── /myid ─────────────────────────────────────────────────────────────────────
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    await update.message.reply_text(
        f"📋 Info ID:\n"
        f"👤 User ID kamu: `{user_id}`\n"
        f"💬 Chat ID ini: `{chat_id}`\n"
        f"📌 Tipe chat: {chat_type}",
        parse_mode="Markdown"
    )

# ── /testreport ───────────────────────────────────────────────────────────────
async def test_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != int(os.environ.get("OWNER_ID", 0)):
        await update.message.reply_text("⛔ Hanya owner yang bisa menjalankan ini.")
        return
    await update.message.reply_text("🔄 Mengirim test report ke kedua grup...")
    await send_daily_report(context.application)
    await update.message.reply_text("✅ Test report selesai!")

# ── /testsosmed ───────────────────────────────────────────────────────────────
async def test_sosmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != int(os.environ.get("OWNER_ID", 0)):
        await update.message.reply_text("⛔ Hanya owner yang bisa menjalankan ini.")
        return
    await update.message.reply_text("📱 Mengambil data sosmed... tunggu sekitar 1 menit ya.")
    try:
        data = await get_social_media_intel()
        await update.message.reply_text(f"✅ Data sosmed berhasil:\n\n{data[:3000]}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ── /clear ────────────────────────────────────────────────────────────────────
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_history(user_id)
    await update.message.reply_text("🧹 Memory dihapus! Kita mulai percakapan baru.")

# ── Photo handler ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    caption = update.message.caption or "Tolong analisa gambar ini."

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        path  = tempfile.mktemp(suffix=".jpg")
        await file.download_to_drive(path)

        with open(path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")

        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}},
                    {"type": "text", "text": caption}
                ]
            }]
        )
        reply = response.content[0].text
        save_message(user_id, "user", f"[Mengirim foto] {caption}")
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)

    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text("⚠️ Gagal menganalisa foto. Coba lagi ya.")

# ── Text handler ──────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    user_text = update.message.text.strip()
    chat_type = update.effective_chat.type

    if chat_type in ["group", "supergroup"]:
        is_mention = False
        is_reply_to_bot = False

        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "mention":
                    mentioned = user_text[entity.offset:entity.offset + entity.length]
                    if "Myhumbleservant_Vic_bot" in mentioned or "Dikara_asisstant_bot" in mentioned:
                        is_mention = True

        if update.message.reply_to_message:
            if update.message.reply_to_message.from_user.is_bot:
            is_reply_to_bot = True

        try:
            team = supabase.table("team_members").select("user_id").execute()
            team_ids = [str(m["user_id"]) for m in team.data]
        except:
            team_ids = []

        is_authorized = str(user_id) == os.environ.get("OWNER_ID") or str(user_id) in team_ids

        if not (is_mention or is_reply_to_bot) and not is_authorized:
            return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        # ── PDF request ───────────────────────────────────────────────────────
        if user_text.upper().startswith("BUATKAN PDF:") or user_text.upper().startswith("MAKE PDF:"):
            instruction = user_text.split(":", 1)[1].strip()
            history = load_history(user_id)
            history.append({"role": "user", "content": f"Buatkan konten lengkap untuk PDF tentang: {instruction}. Tulis dalam format yang rapi dengan judul dan paragraf."})
            content = ask_claude(history)

            title = instruction[:50]
            pdf_path = create_pdf(content, title)

            save_message(user_id, "user", user_text)
            save_message(user_id, "assistant", f"[PDF dibuat] {title}")

            await update.message.reply_text(f"📄 Membuat PDF: *{title}*...", parse_mode="Markdown")
            with open(pdf_path, "rb") as pdf_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=pdf_file,
                    filename=f"{title}.pdf"
                )
            return

        # ── Web search request ────────────────────────────────────────────────
        if user_text.upper().startswith("SEARCH:"):
            query = user_text.split(":", 1)[1].strip()
            await update.message.reply_text(f"🔍 Mencari: *{query}*...", parse_mode="Markdown")

            results = tavily.search(query=query, max_results=5)
            search_summary = "\n\n".join([
                f"**{r['title']}**\n{r['content'][:300]}..."
                for r in results.get("results", [])
            ])

            history = load_history(user_id)
            history.append({"role": "user", "content": f"Berdasarkan hasil pencarian web berikut, jawab pertanyaan: {query}\n\nHasil pencarian:\n{search_summary}"})
            reply = ask_claude(history, extra_system="You have been given real-time web search results. Analyze and summarize them clearly.")

            save_message(user_id, "user", user_text)
            save_message(user_id, "assistant", reply)
            await update.message.reply_text(reply)
            return

        # ── Social media request ──────────────────────────────────────────────
        if user_text.upper().startswith("SOSMED:"):
            query = user_text.split(":", 1)[1].strip()
            await update.message.reply_text("📱 Mengambil data sosmed... tunggu sekitar 1 menit ya.")

            social_data = await get_social_media_intel()

            history = load_history(user_id)
            history.append({"role": "user", "content": f"Berdasarkan data sosial media berikut, analisa dan jawab: {query}\n\nData sosmed:\n{social_data}"})
            reply = ask_claude(history, extra_system="You have been given real social media data. Analyze competitor activity, engagement rates, and trends for Dikara jewelry business.")

            save_message(user_id, "user", user_text)
            save_message(user_id, "assistant", reply)
            await update.message.reply_text(reply)
            return

        # ── Normal conversation ───────────────────────────────────────────────
        history = load_history(user_id)

        chat_id = update.effective_chat.id
        extra = ""
        if chat_id == SALES_GROUP_ID:
            extra = "PENTING: Selalu jawab dalam Bahasa Indonesia, apapun bahasa yang digunakan."

        history.append({"role": "user", "content": user_text})
        reply = ask_claude(history, extra_system=extra)

        save_message(user_id, "user", user_text)
        save_message(user_id, "assistant", reply)
        await update.message.reply_text(reply)

    except Exception as e:
        logging.error(f"Message error: {e}")
        await update.message.reply_text("⚠️ Ada error. Coba lagi ya!")

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    async def post_init(application):
        scheduler = AsyncIOScheduler(timezone=WIB)
        scheduler.add_job(
            send_daily_report,
            trigger="cron",
            hour=9,
            minute=0,
            args=[application]
        )
        scheduler.start()

    app.post_init = post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("testreport", test_report))
    app.add_handler(CommandHandler("testsosmed", test_sosmed))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot is running...")
    app.run_polling()
