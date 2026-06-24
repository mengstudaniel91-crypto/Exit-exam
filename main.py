import os
import logging
import threading
import requests
from bs4 import BeautifulSoup
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))

RESULT_URL = "https://result.ethernet.edu.et"
# The React app calls a JSON API — we target it directly.
# Pattern observed: POST /api/result  or  GET /api/result?reg=<REG>
# We try both approaches so the bot degrades gracefully if one changes.
API_ENDPOINTS = [
    "https://result.ethernet.edu.et/api/result",
    "https://result.ethernet.edu.et/result",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": RESULT_URL,
    "Origin": RESULT_URL,
}

# ─── Flask keep-alive server ───────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Exit Exam Bot is running ✅", 200

@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ─── Scraping helpers ──────────────────────────────────────────────────────────

def fetch_result_json(reg_number: str) -> dict | None:
    """Try JSON API endpoints (GET and POST)."""
    session = requests.Session()
    session.headers.update(HEADERS)

    for endpoint in API_ENDPOINTS:
        # Try GET
        try:
            resp = session.get(endpoint, params={"reg": reg_number, "regno": reg_number}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    return data
        except Exception:
            pass

        # Try POST
        try:
            resp = session.post(
                endpoint,
                json={"reg": reg_number, "regno": reg_number},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    return data
        except Exception:
            pass

        # Try POST form-encoded
        try:
            resp = session.post(
                endpoint,
                data={"reg": reg_number, "regno": reg_number},
                timeout=15,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data:
                        return data
                except Exception:
                    pass
        except Exception:
            pass

    return None


def fetch_result_html(reg_number: str) -> dict | None:
    """
    Fallback: POST the registration number to the main page and
    scrape whatever HTML the server returns.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    payloads = [
        {"reg": reg_number},
        {"regno": reg_number},
        {"registration_number": reg_number},
        {"id": reg_number},
    ]

    for payload in payloads:
        try:
            resp = session.post(RESULT_URL, data=payload, timeout=15)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Generic table scrape — works for many MoE-style result pages
            result = {}
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if len(cells) >= 2:
                        key = cells[0].get_text(strip=True).lower()
                        value = cells[1].get_text(strip=True)
                        if "name" in key:
                            result["name"] = value
                        elif "reg" in key or "id" in key:
                            result["reg"] = value
                        elif "score" in key or "mark" in key or "result" in key or "grade" in key:
                            result["score"] = value
                        elif "status" in key or "pass" in key or "fail" in key:
                            result["status"] = value
                        elif "program" in key or "field" in key or "department" in key:
                            result["program"] = value
                        elif "university" in key or "institution" in key:
                            result["institution"] = value
                        elif "year" in key or "exam" in key:
                            result["exam_year"] = value

            # Also check definition lists and labeled spans
            for dt in soup.find_all(["dt", "label", "strong", "b"]):
                text = dt.get_text(strip=True).lower()
                sibling = dt.find_next_sibling()
                if not sibling:
                    sibling = dt.parent.find_next_sibling()
                if sibling:
                    val = sibling.get_text(strip=True)
                    if "name" in text and "name" not in result:
                        result["name"] = val
                    elif ("score" in text or "mark" in text) and "score" not in result:
                        result["score"] = val
                    elif "status" in text and "status" not in result:
                        result["status"] = val

            if result:
                return result

        except Exception as e:
            logger.warning(f"HTML scrape attempt failed: {e}")

    return None


def parse_json_result(data: dict | list) -> dict:
    """Normalize a JSON API response into a flat result dict."""
    if isinstance(data, list):
        data = data[0] if data else {}

    # Common key aliases used by MoE APIs
    aliases = {
        "name":        ["name", "student_name", "full_name", "studentName", "fullName"],
        "reg":         ["reg", "regno", "registration", "reg_no", "registrationNumber"],
        "score":       ["score", "total_score", "result", "mark", "totalScore", "totalMark"],
        "status":      ["status", "pass_fail", "passFail", "exam_status"],
        "program":     ["program", "field", "department", "stream"],
        "institution": ["university", "institution", "college", "school"],
        "exam_year":   ["year", "exam_year", "examYear", "academic_year"],
    }

    result = {}
    lower_data = {k.lower(): v for k, v in data.items()}
    for field, keys in aliases.items():
        for key in keys:
            if key in data:
                result[field] = str(data[key])
                break
            if key.lower() in lower_data:
                result[field] = str(lower_data[key.lower()])
                break

    return result


def get_student_result(reg_number: str) -> str:
    """Master function: try JSON API first, fall back to HTML scraping."""
    reg_number = reg_number.strip()

    # 1. Try JSON API
    json_data = fetch_result_json(reg_number)
    if json_data:
        result = parse_json_result(json_data)
        if result:
            return format_result(reg_number, result)

    # 2. Try HTML scraping
    html_result = fetch_result_html(reg_number)
    if html_result:
        return format_result(reg_number, html_result)

    # 3. Not found or site unreachable
    return (
        "❌ *Result not found.*\n\n"
        "Possible reasons:\n"
        "• The registration number is incorrect\n"
        "• Results for your exam have not been published yet\n"
        "• The MoE website is temporarily down\n\n"
        f"You can also check directly: {RESULT_URL}"
    )


def format_result(reg_number: str, result: dict) -> str:
    """Build a clean Markdown message from a result dict."""
    # Determine status emoji
    status_raw = result.get("status", "").lower()
    if any(w in status_raw for w in ["pass", "passed", "success"]):
        status_emoji = "✅"
    elif any(w in status_raw for w in ["fail", "failed", "not"]):
        status_emoji = "❌"
    else:
        status_emoji = "📋"

    lines = ["🎓 *Exit Exam Result*", "─────────────────────"]

    if result.get("name"):
        lines.append(f"👤 *Name:* {result['name']}")

    lines.append(f"🪪 *Reg. No:* `{result.get('reg', reg_number)}`")

    if result.get("program"):
        lines.append(f"📚 *Program:* {result['program']}")

    if result.get("institution"):
        lines.append(f"🏫 *Institution:* {result['institution']}")

    if result.get("exam_year"):
        lines.append(f"📅 *Exam Year:* {result['exam_year']}")

    if result.get("score"):
        lines.append(f"📊 *Score:* {result['score']}")

    if result.get("status"):
        lines.append(f"{status_emoji} *Status:* {result['status']}")

    lines.append("─────────────────────")
    lines.append(f"🔗 [View on official site]({RESULT_URL})")

    return "\n".join(lines)


# ─── Telegram handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to the Exit Exam Result Bot!*\n\n"
        "Send me your *registration number* and I'll fetch your result from the "
        "Ministry of Education portal.\n\n"
        "Example: `ETH/XXXX/XXXX`\n\n"
        "Just type or paste your registration number below 👇",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *How to use this bot:*\n\n"
        "1. Type your registration number (e.g. `ETH/1234/2023`)\n"
        "2. The bot will query the official MoE result portal\n"
        "3. Your result will be displayed here\n\n"
        "If you get an error, double-check your registration number "
        "or try again later — the MoE site may be temporarily busy.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg_number = update.message.text.strip()

    if not reg_number:
        await update.message.reply_text("Please send a valid registration number.")
        return

    # Basic sanity check — skip obvious non-reg inputs
    if len(reg_number) < 3:
        await update.message.reply_text(
            "⚠️ That looks too short to be a registration number. Please try again."
        )
        return

    await update.message.reply_text("🔍 Fetching your result, please wait...")

    try:
        result_text = get_student_result(reg_number)
        await update.message.reply_text(result_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Unhandled error for {reg_number}: {e}")
        await update.message.reply_text(
            "⚠️ An unexpected error occurred. Please try again in a moment.\n\n"
            f"You can also check directly: {RESULT_URL}"
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Telegram error: {context.error}")


# ─── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set.")

    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask keep-alive server started on port {PORT}")

    # Build and run the Telegram bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Telegram bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
