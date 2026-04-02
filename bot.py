import os
import threading
import asyncio
import logging
import datetime as dt
from datetime import datetime, timedelta
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CONFIGURAZIONE ---
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
GROUP_ADMIN = int(os.getenv("GROUP_ADMIN", 0))
PORT = int(os.environ.get('PORT', 8080))

# Connessione al DB
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.monitor_bot
    users_col = db.users
    messages_col = db.messages
    messages_col.create_index("timestamp", expireAfterSeconds=7776000)
    client.server_info() 
    logger.info("✅ Connesso a MongoDB")
except Exception as e:
    logger.error(f"❌ Errore connessione MongoDB: {e}")

def get_now():
    return datetime.utcnow() + timedelta(hours=1)

# --- SERVER WEB (Keep-Alive) ---
webapp = Flask(__name__)

@webapp.route('/')
def home():
    return "Bot is active and healthy", 200

def run_flask():
    logger.info(f"📡 Avvio Flask sulla porta {PORT}")
    webapp.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# --- LOGICA STATUS ---
async def get_status_message():
    try:
        last_msg = messages_col.find_one(sort=[("timestamp", -1)])
        if last_msg:
            last_time = last_msg['timestamp']
            is_online = (get_now() - last_time) < timedelta(hours=2)
            status = "🟢 ONLINE" if is_online else "🔴 OFFLINE (Inattivo)"
            return f"{status}\n_Ultimo messaggio: {last_time.strftime('%H:%M:%S')}_"
        return "🔴 **Status Update**\nNessun dato nel DB."
    except Exception as e:
        return f"⚠️ **Errore Database**: {str(e)}"

# --- TASK PIANIFICATI ---
async def status_check_job(context: ContextTypes.DEFAULT_TYPE):
    msg = await get_status_message()
    await context.bot.send_message(chat_id=GROUP_ADMIN, text=msg, parse_mode="Markdown")

# --- HANDLERS BOT ---
async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_MONITOR:
        user = update.effective_user
        if not user or user.is_bot: return
        now = get_now()
        username = f"@{user.username}" if user.username else user.full_name
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {"username": username, "last_seen": now, "last_text": update.message.text[:100] if update.message.text else ""}}, 
            upsert=True
        )
        messages_col.insert_one({"username": username, "timestamp": now})

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_ADMIN:
        msg = await get_status_message()
        await update.message.reply_text(msg, parse_mode="Markdown")

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    status_msg = await update.message.reply_text("🔄 Sincronizzazione in corso...")
    all_users = list(users_col.find())
    gone_ids, gone_names = [], []

    for user in all_users:
        try:
            await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
        except Exception:
            gone_ids.append(user['user_id'])
            gone_names.append(user.get('username', 'Unknown'))
        await asyncio.sleep(0.05)

    if gone_ids:
        context.user_data['pending_delete'] = gone_ids
        elenco = "\n".join(f"- {u}" for u in gone_names[:10])
        keyboard = [[InlineKeyboardButton("🗑️ Elimina Usciti", callback_data="do_delete")]]
        await status_msg.edit_text(f"⚠️ **Trovati {len(gone_ids)} usciti:**\n{elenco}", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await status_msg.edit_text("✅ Database già pulito.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "do_delete":
        ids = context.user_data.get('pending_delete', [])
        if ids:
            users_col.delete_many({"user_id": {"$in": ids}})
            await query.edit_message_text(f"✅ Rimossi {len(ids)} record.")
        context.user_data['pending_delete'] = []

# --- MAIN ---
if __name__ == "__main__":
    # Avvio Flask in un thread separato
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Configurazione Bot
    application = Application.builder().token(TOKEN).build()
    
    # Pianificazione status check (08:00 e 20:00)
    times = [dt.time(hour=8, minute=0), dt.time(hour=20, minute=0)]
    for t in times:
        application.job_queue.run_daily(status_check_job, time=t)
    
    # Registrazione Handlers
    application.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("🚀 Bot in fase di avvio...")
    
    # Avvio del polling
    application.run_polling(drop_pending_updates=True)
