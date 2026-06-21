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
import re
import time
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
def _markdown_bold_to_reportlab(text: str) -> str:
    """
    Convert **bold** markdown into ReportLab's <b>...</b> mini-XML correctly.
    Previous version did .replace('**','<b>').replace('**','</b>') which is a
    no-op bug: the first replace already consumes every '**', so the second
    replace finds nothing and every bold tag is left unclosed.
    This also escapes stray <, >, & so ReportLab's parser doesn't choke on
    characters Claude may emit (e.g. "harga < 5jt").
    """
    # Escape XML-special characters first (but not the ** markers themselves)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Alternate each pair of ** into <b>...</b>
    parts = text.split("**")
    rebuilt = ""
    for i, part in enumerate(parts):
        if i % 2 == 1:
            rebuilt += f"<b>{part}</b>"
        else:
            rebuilt += part
    return rebuilt

def create_pdf(content: str, title: str = "Dokumen") -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(tmp.name, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title", parent=styles["Heading1"], fontSize=16, spaceAfter=20)
    body_style  = ParagraphStyle("Body",  parent=styles["Normal"],   fontSize=11, leading=18)

    story = [
        Paragraph(_markdown_bold_to_reportlab(title), title_style),
        Spacer(1, 0.5*cm),
    ]
    for line in content.split("\n"):
        line = line.strip()
        if line:
            try:
                story.append(Paragraph(_markdown_bold_to_reportlab(line), body_style))
            except Exception as e:
                # Fallback: if a line still breaks ReportLab's parser, strip
                # all markup and send as plain text rather than crashing the
                # whole PDF generation.
                logging.warning(f"PDF line parse fallback: {e}")
                plain = re.sub(r"[<>&*]", "", line)
                story.append(Paragraph(plain, body_style))
            story.append(Spacer(1, 0.2*cm))

    doc.build(story)
    return tmp.name

# ── Claude call (with native tool-calling) ───────────────────────────────────
MAX_TOOL_ROUNDS = 3  # batas anti infinite-loop / cost blowup

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Cari informasi terkini di internet. Gunakan ini kalau butuh data yang "
            "mungkin sudah berubah sejak training (harga emas/diamond hari ini, "
            "berita terbaru, tren fashion/jewelry terkini, info kompetitor, dll). "
            "Jangan gunakan untuk pertanyaan umum yang sudah kamu tahu jawabannya "
            "(misal definisi 4Cs diamond, sejarah, konsep dasar)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Query pencarian, singkat dan spesifik (Bahasa Indonesia atau Inggris)."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_social_media_intel",
        "description": (
            "Ambil data terbaru dari Instagram kompetitor (followers, post, engagement) "
            "dan tren hashtag TikTok yang relevan dengan industri jewelry/diamond. "
            "Gunakan ini kalau user tanya soal kompetitor, tren sosial media, atau "
            "konten yang lagi viral di industri ini. Proses ini bisa makan waktu "
            "hingga 1 menit kalau data belum ada di cache."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        }
    }
]

async def _execute_tool(tool_name: str, tool_input: dict) -> str:
    """Eksekusi tool yang diminta Claude, return hasil sebagai string."""
    if tool_name == "web_search":
        query = tool_input.get("query", "")
        try:
            results = tavily.search(query=query, max_results=5)
            summary = "\n\n".join([
                f"**{r['title']}**\n{r['content'][:300]}"
                for r in results.get("results", [])
            ])
            return summary or "Tidak ada hasil pencarian ditemukan."
        except Exception as e:
            logging.error(f"Tool web_search error: {e}")
            return f"Gagal melakukan pencarian: {e}"

    elif tool_name == "get_social_media_intel":
        try:
            return await get_social_media_intel()
        except Exception as e:
            logging.error(f"Tool get_social_media_intel error: {e}")
            return f"Gagal mengambil data sosmed: {e}"

    return f"Tool tidak dikenal: {tool_name}"

async def ask_claude(messages: list, extra_system: str = "", use_tools: bool = True) -> str:
    """
    Panggil Claude dengan native tool-calling. Claude bisa minta web_search atau
    get_social_media_intel secara otonom berdasarkan isi percakapan, tanpa user
    perlu ketik prefix manual. Dibatasi MAX_TOOL_ROUNDS untuk cegah loop tak
    berkesudahan / biaya membengkak.

    use_tools=False dipakai untuk jalur prefix manual (SEARCH:/SOSMED:/PDF) yang
    sudah punya data siap pakai di prompt — di situ tool-calling cuma nambah
    latency tanpa guna, karena datanya sudah ditempel ke pesan.
    """
    system = SYSTEM_PROMPT + ("\n\n" + extra_system if extra_system else "")
    working_messages = list(messages)  # jangan mutate list asli pemanggil

    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1500,
        "system": system,
        "messages": working_messages,
    }
    if use_tools:
        kwargs["tools"] = TOOLS

    for round_num in range(MAX_TOOL_ROUNDS):
        response = claude.messages.create(**kwargs)

        if response.stop_reason != "tool_use":
            # Claude selesai — ambil semua text block (biasanya cuma 1)
            text_parts = [block.text for block in response.content if block.type == "text"]
            return "\n".join(text_parts) if text_parts else "(Tidak ada respons teks dari Claude)"

        # Claude minta tool — eksekusi semua tool_use block di response ini
        working_messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                logging.info(f"Tool call: {block.name}({block.input})")
                result_text = await _execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text[:4000]  # cap biar tidak membengkak context
                })

        working_messages.append({"role": "user", "content": tool_results})
        kwargs["messages"] = working_messages

    # Lewat batas round — paksa jawaban final tanpa tool lagi
    kwargs.pop("tools", None)
    response = claude.messages.create(**kwargs)
    text_parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(text_parts) if text_parts else "Maaf, butuh waktu lebih lama dari biasanya. Coba tanya lagi dengan lebih spesifik ya."

# ── Apify Social Media Scraper ────────────────────────────────────────────────
async def scrape_instagram_profile(username: str) -> dict:
    """Scrape Instagram profile data via Apify"""
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            run_response = await client.post(
                f"https://api.apify.com/v2/acts/apify~instagram-profile-scraper/runs",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"},
                json={
                    "usernames": [username],
                    "resultsLimit": 5
                }
            )
            run_data = run_response.json()
            run_id = run_data.get("data", {}).get("id")
            if not run_id:
                logging.warning(f"Apify: no run_id for {username}")
                return {}

            # Tunggu selesai (max 60 detik = 20x poll tiap 3 detik)
            status = None
            status_resp = None
            for _ in range(20):
                await asyncio.sleep(3)
                try:
                    status_resp = await client.get(
                        f"https://api.apify.com/v2/actor-runs/{run_id}",
                        headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
                    )
                    status = status_resp.json().get("data", {}).get("status")
                    if status == "SUCCEEDED":
                        break
                    elif status in ["FAILED", "ABORTED", "TIMED-OUT"]:
                        logging.warning(f"Apify run {status} for {username}")
                        return {}
                except Exception as poll_err:
                    logging.warning(f"Poll error for {username}: {poll_err}")
                    continue

            if status != "SUCCEEDED" or status_resp is None:
                logging.warning(f"Apify did not succeed for {username}, status={status}")
                return {}

            dataset_id = status_resp.json().get("data", {}).get("defaultDatasetId")
            if not dataset_id:
                return {}
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
        async with httpx.AsyncClient(timeout=90) as client:
            run_response = await client.post(
                f"https://api.apify.com/v2/acts/clockworks~tiktok-scraper/runs",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"},
                json={
                    "hashtags": [hashtag],
                    "resultsPerPage": 5
                }
            )
            run_data = run_response.json()
            run_id = run_data.get("data", {}).get("id")
            if not run_id:
                logging.warning(f"Apify TikTok: no run_id for {hashtag}")
                return []

            status = None
            status_resp = None
            for _ in range(20):
                await asyncio.sleep(3)
                try:
                    status_resp = await client.get(
                        f"https://api.apify.com/v2/actor-runs/{run_id}",
                        headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
                    )
                    status = status_resp.json().get("data", {}).get("status")
                    if status == "SUCCEEDED":
                        break
                    elif status in ["FAILED", "ABORTED", "TIMED-OUT"]:
                        logging.warning(f"Apify TikTok run {status} for {hashtag}")
                        return []
                except Exception as poll_err:
                    logging.warning(f"TikTok poll error for {hashtag}: {poll_err}")
                    continue

            if status != "SUCCEEDED" or status_resp is None:
                logging.warning(f"Apify TikTok did not succeed for {hashtag}, status={status}")
                return []

            dataset_id = status_resp.json().get("data", {}).get("defaultDatasetId")
            if not dataset_id:
                return []
            result_resp = await client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                headers={"Authorization": f"Bearer {APIFY_API_KEY}"}
            )
            return result_resp.json()

    except Exception as e:
        logging.error(f"Apify TikTok error for {hashtag}: {e}")
        return []

async def _scrape_social_media_intel() -> str:
    """Kumpulkan data sosmed dari kompetitor + hashtag tren (scrape langsung, tanpa cache)"""
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

async def get_social_media_intel(force_refresh: bool = False) -> str:
    """
    Entry point untuk ambil data sosmed. Cek cache Supabase dulu (TTL 6 jam)
    sebelum scrape Apify lagi — hemat kuota free tier kalau dipanggil berkali-kali
    dalam rentang waktu pendek (misal SOSMED: dipanggil 2x dalam 1 jam yang sama).
    """
    if not force_refresh:
        cached = get_cached_social_data()
        if cached is not None:
            return cached

    fresh_data = await _scrape_social_media_intel()
    save_social_cache(fresh_data)
    return fresh_data

# ── Social Media Cache (Supabase) ────────────────────────────────────────────
SOCIAL_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 jam — Instagram/TikTok data tidak berubah cepat

def get_cached_social_data() -> str | None:
    """Ambil cache sosmed dari Supabase kalau masih fresh (< TTL). None kalau expired/tidak ada."""
    try:
        result = supabase.table("social_cache") \
            .select("data, created_at") \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        if not result.data:
            return None
        row = result.data[0]
        created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        age_seconds = (datetime.now(pytz.UTC) - created_at).total_seconds()
        if age_seconds < SOCIAL_CACHE_TTL_SECONDS:
            logging.info(f"Social cache HIT (umur {int(age_seconds/60)} menit)")
            return row["data"]
        logging.info(f"Social cache EXPIRED (umur {int(age_seconds/60)} menit)")
        return None
    except Exception as e:
        logging.error(f"Get social cache error: {e}")
        return None

def save_social_cache(data: str):
    try:
        supabase.table("social_cache").insert({"data": data}).execute()
    except Exception as e:
        logging.error(f"Save social cache error: {e}")

# ── Rate Limiting (in-memory, per-process) ───────────────────────────────────
# Sengaja in-memory (bukan Supabase) karena reset per restart itu acceptable,
# dan menghindari 1 DB call tambahan di setiap message.
_rate_limit_log: dict[str, list[float]] = {}

def check_rate_limit(user_id: int, action: str, max_calls: int, window_seconds: int) -> bool:
    """
    Return True kalau masih dalam limit (boleh lanjut), False kalau sudah kena limit.
    """
    key = f"{user_id}:{action}"
    now = time.time()
    timestamps = _rate_limit_log.get(key, [])
    # buang timestamp yang sudah di luar window
    timestamps = [t for t in timestamps if now - t < window_seconds]

    if len(timestamps) >= max_calls:
        _rate_limit_log[key] = timestamps
        return False

    timestamps.append(now)
    _rate_limit_log[key] = timestamps
    return True

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
        social_data = await get_social_media_intel(force_refresh=True)
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

    mgmt_report = await ask_claude([{"role": "user", "content": mgmt_prompt}], use_tools=False)

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

    sales_report = await ask_claude([{"role": "user", "content": sales_prompt}], use_tools=False)

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

    force = bool(context.args) and context.args[0].lower() in ("force", "fresh", "refresh")
    label = "🔄 force refresh (scrape baru)" if force else "📦 cek cache dulu"
    await update.message.reply_text(f"📱 Mengambil data sosmed ({label})... tunggu sekitar 1 menit ya.")
    try:
        data = await get_social_media_intel(force_refresh=force)
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
            model="claude-sonnet-4-6",
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
            if not check_rate_limit(user_id, "pdf", max_calls=5, window_seconds=3600):
                await update.message.reply_text("⏳ Sudah banyak PDF dibuat sejam terakhir. Coba lagi nanti ya (max 5x/jam).")
                return

            instruction = user_text.split(":", 1)[1].strip()
            history = load_history(user_id)
            history.append({"role": "user", "content": f"Buatkan konten lengkap untuk PDF tentang: {instruction}. Tulis dalam format yang rapi dengan judul dan paragraf."})
            content = await ask_claude(history, use_tools=False)

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
            reply = await ask_claude(history, extra_system="You have been given real-time web search results. Analyze and summarize them clearly.", use_tools=False)

            save_message(user_id, "user", user_text)
            save_message(user_id, "assistant", reply)
            await update.message.reply_text(reply)
            return

        # ── Social media request ──────────────────────────────────────────────
        if user_text.upper().startswith("SOSMED:"):
            if not check_rate_limit(user_id, "sosmed", max_calls=5, window_seconds=3600):
                await update.message.reply_text("⏳ Sudah 5x cek sosmed dalam 1 jam terakhir. Coba lagi nanti ya (data tidak berubah secepat itu kok 😊).")
                return

            query = user_text.split(":", 1)[1].strip()
            await update.message.reply_text("📱 Mengambil data sosmed... tunggu sekitar 1 menit ya.")

            social_data = await get_social_media_intel()

            history = load_history(user_id)
            history.append({"role": "user", "content": f"Berdasarkan data sosial media berikut, analisa dan jawab: {query}\n\nData sosmed:\n{social_data}"})
            reply = await ask_claude(history, extra_system="You have been given real social media data. Analyze competitor activity, engagement rates, and trends for Dikara jewelry business.", use_tools=False)

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
        reply = await ask_claude(history, extra_system=extra)

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
