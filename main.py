import os
import logging
import threading
import requests
from bs4 import BeautifulSoup
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Logging አቀናጅቶ ለማየት
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# 🔑 የቦት ቶከን ከ Environment Variable ያነባል (በ Render ላይ የምናስገባው ነው)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# 🌐 የውጤት ማያ ድረ-ገጽ URL
RESULT_URL = "https://result.ethernet.edu.et" 

# Flask መተግበሪያ (Render እንዳይዘጋው ለመከላከል)
app = Flask('')

@app.route('/')
def home():
    return "ቦቱ በሰላም እየሰራ ነው!"

def run_flask():
    # Render በየሰዓቱ የሚሰጠውን Port በራስ-ሰር ያገኛል
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# 1. /start ሲባል የሚመጣ መልዕክት
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ሰላም! እንኳን ወደ Exit Exam ውጤት ማያ ቦት በሰላም መጣህ።\n\n"
        "እባክህ የፈተና መለያ ቁጥርህን (Registration Number) አስገባልኝ፦"
    )

# 2. ከድረ-ገጹ ላይ ውጤት የሚስበው ክፍል (Scraper)
def fetch_exit_result(reg_num):
    try:
        # ለድረ-ገጹ የሚላክ ፎርም ዳታ (የ input ፎርሙ ስም 'registration_number' ካልሆነ በኋላ ላይ ይቀየራል)
        payload = {
            'registration_number': reg_num
        }
        
        response = requests.post(RESULT_URL, data=payload, timeout=10)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 🔍 ከድረ-ገጹ HTML ላይ ውጤቱ ያለበትን ክፍል መፈለግ
            name_element = soup.find('span', id='student_name') or soup.find(class_='name')
            score_element = soup.find('div', class_='score') or soup.find(id='result')
            status_element = soup.find('div', class_='status')
            
            if score_element:
                name = name_element.text.strip() if name_element else "ያልታወቀ"
                score = score_element.text.strip()
                status = status_element.text.strip() if status_element else ""
                
                return f"📋 **የውጤት መግለጫ**\n\n👤 ስም: {name}\n🆔 መለያ ቁጥር: {reg_num}\n💯 ውጤት: {score}\n📌 ሁኔታ: {status}"
            else:
                return f"❌ ያስገቡት መለያ ቁጥር ({reg_num}) አልተገኘም። እባክዎ በትክክል መጻፍዎን ያረጋግጡ።"
        else:
            return "⚠️ የትምህርት ሚኒስቴር ድረ-ገጽ በአሁኑ ሰዓት በጣም ተጨናንቋል። እባክህ ጥቂት ደቂቃዎች ቆይተህ ድጋሚ ሞክር።"
            
    except Exception as e:
        logging.error(f"Error scraping data: {e}")
        return "❌ በትራፊክ መብዛት ወይም በኔትወርክ መቋረጥ ምክንያት መረጃውን ማምጣት አልተቻለም።"

# 3. ተማሪው ቁጥር ሲያስገባ መልስ የሚሰጠው ክፍል
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    waiting_message = await update.message.reply_text("🔄 በመፈለግ ላይ ነው... እባክህ ትንሽ ታገስ...")
    
    result_text = fetch_exit_result(user_input)
    await waiting_message.edit_text(result_text, parse_mode="Markdown")

def main():
    # Flaskን በሌላ Thread ውስጥ ማስነሳት
    threading.Thread(target=run_flask).start()
    
    # የቴሌግራም ቦቱን ማስነሳት
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("ቦቱ እና ዌብ ሰርቨሩ በተሳካ ሁኔታ ተነስተዋል...")
    bot_app.run_polling()

if __name__ == '__main__':
    main()
