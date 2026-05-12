import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ── Config (set these as environment variables) ───────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ── Claude client ─────────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── System Prompt (your assistant's personality) ──────────────────────────────
SYSTEM_PROMPT = """
You are a highly capable personal assistant for a devout Christian entrepreneur 
who owns a custom jewelry and lab-grown diamond business — the first of its kind in Indonesia.

Your role covers 4 areas:
1. **Jewelry & Diamond Expert** — You know about lab-grown diamonds, custom jewelry design, 
   GIA certifications, 4Cs (cut, color, clarity, carat), pricing strategy, and the Indonesian 
   luxury jewelry market.

2. **Christian Faith Perspective** — When asked about life, decisions, struggles, or 
   biblical topics, you provide thoughtful, scripture-grounded answers with grace and wisdom.

3. **Content & Copywriting** — You help craft Instagram captions, marketing copy, product 
   descriptions, email drafts, and business ideas that reflect an elegant, premium brand voice.

4. **General Smart Assistant** — For everything else, you are sharp, organized, and practical.

Language: Detect the language the user writes in. If they write in Bahasa Indonesia, 
reply in Bahasa Indonesia. If in English, reply in English. If mixed, match their dominant language.

Tone: Warm, intelligent, professional — like a trusted advisor who genuinely cares.
Never be robotic or generic. Always be specific and useful.
"""

# ── Conversation memory (per user) ───────────────────────────────────────────
conversation_history: dict[int, list] = {}

# ── /start command ─────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Halo! I'm your personal assistant.\n\n"
        "I can help you with:\n"
        "💎 Jewelry & lab-grown diamond knowledge\n"
        "✝️ Christian faith & biblical perspective\n"
        "✍️ Content & copywriting for your brand\n"
        "🧠 General smart assistance\n\n"
        "Just type anything — in English or Bahasa Indonesia!"
    )

# ── /clear command — reset conversation memory ────────────────────────────────
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_history[user_id] = []
    await update.message.reply_text("🧹 Memory cleared! Let's start fresh.")

# ── Main message handler ──────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    # Initialize history for new users
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    # Add user message to history
    conversation_history[user_id].append({
        "role": "user",
        "content": user_text
    })

    # Keep last 20 messages to avoid token overflow
    if len(conversation_history[user_id]) > 20:
        conversation_history[user_id] = conversation_history[user_id][-20:]

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing"
    )

    try:
        # Call Claude API
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=conversation_history[user_id]
        )

        assistant_reply = response.content[0].text

        # Save assistant reply to history
        conversation_history[user_id].append({
            "role": "assistant",
            "content": assistant_reply
        })

        await update.message.reply_text(assistant_reply)

    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(
            "⚠️ Sorry, something went wrong. Please try again in a moment."
        )

# ── Run the bot ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Bot is running...")
    app.run_polling()
