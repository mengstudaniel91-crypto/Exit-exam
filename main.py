import os
import logging
import requests
from bs4 import BeautifulSoup
from flask import Flask, request as flask_request, abort
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import asyncio

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN")
RENDER_URL  = os.getenv("RENDER_URL")   # e.g. https://your-app-name.onrender.com
PORT        = int(os.environ.get("PORT", 8080))

RESULT_BASE = "https://result.ethernet.edu.et"

# These are the known backend API patterns for the MoE React app.
# The app calls its own backend — we replicate those calls.
API_CANDIDATES = [
    "https://result.ethernet.edu.et/api/student/result",
    "https://result.ethernet.edu.et/api/result",
    "https://result.ethernet.edu.et/api/results",
    "https://result.ethernet.edu.et/result",
]

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         RESULT_BASE + "/",
    "Origin":          RESULT_BASE,
}

# ─── Telegram Application (built once, reused across requests) ─────────────────
application = ApplicationBuilder().token(BOT_TOKEN).build()

# ─── Flask app ─────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def home():
    return "Exit Exam Bot is live ✅", 200

@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Receive updates from Telegram and dispatch them."""
    if flask_request.method != "POST":
        abort(403)

    json_data = flask_request.get_json(force=True)
    update = Update.de_json(json_data, application.bot)

    # Run the async handler in the event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(application.process_update(update))
    finally:
        loop.close()

    return "ok", 200

# ─── Result fetching ───────────────────────────────────────────────────────────

def try_json_api(reg: str) -> dict | None:
    """Try all known JSON API endpoints with GET and POST."""
    session = requests.Session()
    session.headers.update(SCRAPE_HEADERS)

    payloads = [
        {"reg": reg},
        {"regno": reg},
        {"registrationNumber": reg},
        {"registration_number": reg},
        {"id": reg},
    ]

    for endpoint in API_CANDIDATES:
        for payload in payloads:
            # GET
            try:
                r = session.get(endpoint, params=payload, timeout=12)
                if r.status_code == 200:
                    data = r.json()
                    if data:
                        return data if isinstance(data, dict) else (data[0] if data else None)
            except Exception:
                pass

            # POST JSON
            try:
                r = session.post(endpoint, json=payload, timeout=12)
                if r.status_code == 200:
                    data = r.json()
                    if data:
                        return data if isinstance(data, dict) else (data[0] if data else None)
            except Exception:
                pass

            # POST form
            try:
                r = session.post(endpoint, data=payload, timeout=12)
                if r.status_code == 200:
                    try:
                        data = r.json()
                        if data:
                            return data if isinstance(data, dict) else (data[0] if data else None)
                    except Exception:
                        pass
            except Exception:
                pass

    return None


def try_html_scrape(reg: str) -> dict | None:
    """Last resort: POST to the base URL and scrape any HTML returned."""
    session = requests.Session()
    session.headers.update(SCRAPE_HEADERS)

    for payload in [{"reg": reg}, {"regno": reg}, {"registration_number": reg}]:
        try:
            r = session.post(RESULT_BASE, data=payload, timeout=12)
            if r.status_code != 200:
                continue
            if "enable JavaScript" in r.text:
                continue  # Pure SPA, no server-rendered result

            soup = BeautifulSoup(r.text, "lxml")
            result = {}

            for row in soup.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    k = cells[0].get_text(strip=True).lower()
                    v = cells[1].get_text(strip=True)
                    if "name" in k:            result["name"] = v
                    elif "reg" in k:           result["reg"] = v
                    elif any(x in k for x in ["score", "mark", "grade"]): result["score"] = v
                    elif any(x in k for x in ["status", "pass", "fail"]): result["status"] = v
                    elif any(x in k for x in ["program", "field", "dept"]): result["program"] = v
                    elif any(x in k for x in ["university", "institution"]): result["institution"] = v
                    elif "year" in k:          result["exam_year"] = v

            if result:
                return result
        except Exception as e:
            logger.warning(f"HTML scrape failed: {e}")

    return None


def normalize(raw: dict) -> dict:
    aliases = {
        "name":        ["name", "student_name", "full_name", "studentName", "fullName", "Name"],
        "reg":         ["reg", "regno", "registration", "reg_no", "registrationNumber"],
        "score":       ["score", "total_score", "totalScore", "mark", "totalMark", "result", "Score"],
        "status":      ["status", "pass_fail", "passFail", "exam_status", "Status"],
        "program":     ["program", "field", "department", "stream", "Program"],
        "institution": ["university", "institution", "college", "school"],
        "exam_year":   ["year", "exam_year", "examYear", "academic_year"],
    }
    out = {}
    lower = {k.lower(): v for k, v in raw.items()}
    for field, keys in aliases.items():
        for k in keys:
            if k in raw:
                out[field] = str(raw[k])
                break
            if k.lower() in lower:
                out[field] = str(lower[k.lower()])
                break
    return out


def format_result(reg: str, r: dict) -> str:
    status_raw = r.get("status", "").lower()
    if any(w in status_raw for w in ["pass", "success"]):
        s_emoji = "✅"
    elif any(w in status_raw for w in ["fail", "not"]):
        s_emoji = "❌"
    else:
        s_emoji = "📋"

    lines = ["🎓 *Exit Exam Result*", "━━━━━━━━━━━━━━━━━━━━━━"]
    if r.get("name"):        lines.append(f"👤 *Name:* {r['name']}")
    lines.append(             f"🪪 *Reg No:* `{r.get('reg', reg)}`")
    if r.get("program"):     lines.append(f"📚 *Program:* {r['program']}")
    if r.get("institution"): lines.append(f"🏫 *Institution:* {r['institution']}")
    if r.get("exam_year"):   lines.append(f"📅 *Year:* {r['exam_year']}")
    if r.get("score"):       lines.append(f"📊 *Score:* {r['score']}")
    if r.get("status"):      lines.append(f"{s_emoji} *Status:* {r['status']}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🔗 [Official site]({RESULT_BASE})")
    return "\n".join(lines)


def get_result(reg: str) -> str:
    raw = try_json_api(reg)
    if raw:
        result = normalize(raw)
        if result:
            return format_result(reg, result)

    raw = try_html_scrape(reg)
    if raw:
        return format_result(reg, raw)

    return (
        "❌ *Result not found.*\n\n"
        "Please check:\n"
        "• Your registration number is correct\n"
        "• Results for your batch have been published\n"
        "• The MoE website is currently accessible\n\n"
        f"🔗 Check directly: {RESULT_BASE}"
    )

# ─── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to the Exit Exam Result Bot!*\n\n"
        "Send me your *registration number* and I'll look up your result "
        "from the Ministry of Education portal.\n\n"
        "Just type your registration number and send it 👇",
        parse_mode="Markdown",
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *How to use:*\n\n"
        "1. Type your registration number (e.g. `ETH/1234/2023`)\n"
        "2. The bot queries the MoE portal\n"
        "3. Your result is shown here\n\n"
        "If you get an error, verify your reg number or try again later.",
        parse_mode="Markdown",
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg = update.message.text.strip()
    if len(reg) < 3:
        await update.message.reply_text("⚠️ That doesn't look like a valid registration number. Please try again.")
        return

    await update.message.reply_text("🔍 Searching for your result, please wait...")
    try:
        text = get_result(reg)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error for {reg}: {e}")
        await update.message.reply_text(
            f"⚠️ Something went wrong. Try again or check directly: {RESULT_BASE}"
        )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Telegram error: {context.error}")

# ─── Register handlers ─────────────────────────────────────────────────────────
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.add_error_handler(error_handler)

# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set.")
    if not RENDER_URL:
        raise ValueError("RENDER_URL is not set. Example: https://your-app.onrender.com")

    # Register the webhook with Telegram
    async def set_webhook():
        await application.initialize()
        webhook_url = f"{RENDER_URL.rstrip('/')}/{BOT_TOKEN}"
        await application.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to: {webhook_url}")

    asyncio.run(set_webhook())

    # Start Flask (this is the ONLY server — it receives webhook POSTs from Telegram)
    logger.info(f"Starting Flask on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)
