import os
import threading
import asyncio
import logging
import time
import datetime as dt
from datetime import datetime, timedelta, time as datetime_time
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError

# --- CONFIGURAZIONE E LOGGING ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
GROUP_ADMIN = int(os.getenv("GROUP_ADMIN", 0))

# Connessione MongoDB con test immediato
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logger.info("✅ Connessione a MongoDB riuscita!")
except Exception as e:
    logger.error(f"❌ Errore connessione MongoDB: {e}")

db = client.monitor_bot
users_col = db.users
messages_col = db.messages
messages_col.create_index("timestamp", expireAfterSeconds=7776000)

def get_now():
    return datetime.utcnow() + timedelta(hours=1)

# --- SERVER WEB (Keep-Alive per Render) ---
webapp = Flask(__name__)
@webapp.route('/')
def home(): return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    webapp.run(host='0.0.0.0', port=port)

# --- FUNZIONE LOGICA TEST (RIUTILIZZABILE) ---
async def perform_status_check(context: ContextTypes.DEFAULT_TYPE):
    last = messages_col.find_one(sort=[("timestamp", -1)])
    if last:
        diff = int((get_now() - last['timestamp']).total_seconds() // 60)
        status = "🟢 Online" if diff < 120 else "⚠️ Offline (>2h)"
        msg = f"📊 **Report Automatico Stato**\n{status}\nUltimo msg: {last['username']}\nRitardo: {diff} min fa"
    else:
        msg = "❌ Database vuoto, nessun messaggio rilevato."
    
    await context.bot.send_message(chat_id=GROUP_ADMIN, text=msg, parse_mode="Markdown")

# --- HANDLERS ---
async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_MONITOR:
        user = update.effective_user
        if not user or user.is_bot: return
        now = get_now()
        username = f"@{user.username}" if user.username else user.full_name
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {"username": username, "last_seen": now, "last_text": update.message.text[:100] if update.message.text else "No text"}}, 
            upsert=True
        )
        messages_col.insert_one({"username": username, "timestamp": now})

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    # Riutilizza la logica del test automatico
    await perform_status_check(context)

# [Qui inserisci le altre funzioni: refresh, count_messages, list_inactive, total_messages, get_user, clean_user, button_handler del codice precedente]

# --- AVVIO ---
def main():
    # Avvio Flask
    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(2)
    
    # Costruzione Applicazione (JobQueue inclusa se installata correttamente)
    app = Application.builder().token(TOKEN).build()
    jq = app.job_queue

    # --- PIANIFICAZIONE TEST AUTOMATICI ---
    # Nota: Gli orari sono riferiti all'ora del server (solitamente UTC). 
    # Se il server è in UTC, sottrai 1 ora per l'Italia. Qui usiamo timedelta per sicurezza.
    jq.run_daily(perform_status_check, time=datetime_time(hour=8, minute=0))
    jq.run_daily(perform_status_check, time=datetime_time(hour=21, minute=30))

    # Handlers
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("test", test_command))
    # ... aggiungi tutti gli altri add_handler qui ...
    
    logger.info("🚀 Bot avviato con JobQueue attiva (08:00 e 21:30)")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
