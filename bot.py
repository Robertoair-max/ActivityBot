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
from telegram.error import BadRequest, TelegramError

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

# Connessione al DB
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.monitor_bot
    users_col = db.users
    messages_col = db.messages
    messages_col.create_index("timestamp", expireAfterSeconds=7776000)
    client.server_info() 
except Exception as e:
    logger.error(f"❌ Errore connessione MongoDB: {e}")

def get_now():
    return datetime.utcnow() + timedelta(hours=1)

# --- SERVER WEB (Keep-Alive) ---
webapp = Flask(__name__)

@webapp.route('/')
def home():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"📡 Avvio Flask sulla porta {port}")
    webapp.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# --- LOGICA STATUS (ONLINE/OFFLINE) ---
async def get_status_message():
    try:
        last_msg = messages_col.find_one(sort=[("timestamp", -1)])
        
        if last_msg:
            last_time = last_msg['timestamp']
            diff = get_now() - last_time
            
            is_online = diff < timedelta(hours=2)
            status_icon = "🟢" if is_online else "🔴"
            status_text = "ONLINE" if is_online else "OFFLINE (Inattivo)"
            
            return f"{status_icon} **Status Update**\nIl bot è {status_text}.\n_Ultimo messaggio: {last_time.strftime('%H:%M:%S')}_"
        else:
            return "🔴 **Status Update**\nIl bot è OFFLINE (Nessun dato nel DB)."
    except Exception as e:
        return f"⚠️ **Errore Database**: {str(e)}"

# --- TASK PIANIFICATI ---
async def status_check_job(context: ContextTypes.DEFAULT_TYPE):
    msg = await get_status_message()
    await context.bot.send_message(chat_id=GROUP_ADMIN, text=msg, parse_mode="Markdown")

# --- HANDLERS BOT ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"⚠️ Errore Telegram: {context.error}")

async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_MONITOR:
        user = update.effective_user
        if not user or user.is_bot: return
        
        now = get_now()
        username = f"@{user.username}" if user.username else user.full_name
        
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "username": username, 
                "last_seen": now, 
                "last_text": update.message.text[:100] if update.message.text else "No text"
            }}, 
            upsert=True
        )
        messages_col.insert_one({"username": username, "timestamp": now})

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    msg = await get_status_message()
    await update.message.reply_text(msg, parse_mode="Markdown")

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    users_col.delete_many({"$or": [{"username": {"$exists": False}}, {"user_id": {"$exists": False}}]})
    status_msg = await update.message.reply_text("🔄 Sincronizzazione...")
    all_users = list(users_col.find())
    gone_ids, gone_names = [], []

    for user in all_users:
        try:
            member = await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
            if member.status in ['left', 'kicked']:
                gone_ids.append(user['user_id']); gone_names.append(user['username'])
        except:
            gone_ids.append(user['user_id']); gone_names.append(user['username'])
        await asyncio.sleep(0.05)

    if gone_ids:
        context.user_data['pending_delete'] = gone_ids
        elenco = "\n".join(f"- {u}" for u in gone_names[:15])
        keyboard = [[InlineKeyboardButton("🗑️ Elimina Usciti", callback_data="confirm_delete")]]
        await status_msg.edit_text(f"⚠️ **Trovati {len(gone_ids)} usciti:**\n{elenco}", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await status_msg.edit_text("✅ Database già pulito.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_delete":
        keyboard = [[InlineKeyboardButton("✅ PROCEDI", callback_data="do_delete")], [InlineKeyboardButton("❌ ANNULLA", callback_data="cancel_delete")]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "do_delete":
        ids = context.user_data.get('pending_delete', [])
        if ids:
            users_col.delete_many({"user_id": {"$in": ids}})
            await query.edit_message_text(f"✅ Rimossi {len(ids)} record.")
        context.user_data['pending_delete'] = []
    elif query.data == "cancel_delete":
        await query.edit_message_text("❌ Operazione annullata.")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(context.args[0])
        limit_date = get_now() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(40)
        lines = [f"- {u['username']} ({u['last_seen'].strftime('%d/%m')})" for u in inactive]
        res = f"⚠️ **Inattivi da {days}gg:**\n" + "\n".join(lines) if lines else f"✅ Tutti attivi negli ultimi {days}gg."
        await update.message.reply_text(res)
    except: 
        await update.message.reply_text("Uso: `/list 7`")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}, {"$limit": 400}]
    results = list(messages_col.aggregate(pipeline))
    res = "📊 **Classifica Messaggi:**\n" + "\n".join([f"- {i['_id']}: {i['total']}" for i in results]) if results else "Nessun dato."
    await update.message.reply_text(res[:4000])

# --- MAIN ---
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    
    application = Application.builder().token(TOKEN).build()
    jq = application.job_queue
    
    # Status check alle 08:00 e 20:00
    times = [dt.time(hour=8, minute=0), dt.time(hour=20, minute=0)]
    for t in times:
        jq.run_daily(status_check_job, time=t)
    
    # Handlers
    application.add_error_handler(error_handler)
    application.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(CommandHandler("list", list_inactive))
    application.add_handler(CommandHandler("total", total_messages))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("🚀 Sistema avviato.")
    application.run_polling(drop_pending_updates=True)
