import os
import threading
import asyncio
import logging
import time
import datetime as dt
from datetime import datetime, timedelta
import pytz
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError

# --- CONFIGURAZIONE E LOGGING ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
ADMIN_ENV = os.getenv("GROUP_ADMIN", "0")
GROUP_ADMINS = [int(i.strip()) for i in ADMIN_ENV.split(",") if i.strip()]
ITALY_TZ = pytz.timezone('Europe/Rome')

# Connessione MongoDB
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.monitor_bot
    users_col = db.users
    messages_col = db.messages
    messages_col.create_index("timestamp", expireAfterSeconds=7776000)
except Exception as e:
    logger.error(f"MongoDB Error: {e}")

# --- SERVER WEB FLASK (Health Check anti-503) ---
webapp = Flask(__name__)
@webapp.route('/')
def health(): return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    webapp.run(host='0.0.0.0', port=port, threaded=True)

# --- FUNZIONI DI REPORT AUTOMATICO ---
async def perform_status_check(context: ContextTypes.DEFAULT_TYPE):
    try:
        last = messages_col.find_one(sort=[("timestamp", -1)])
        if last:
            diff = int((datetime.utcnow() - last['timestamp']).total_seconds() // 60)
            status = "🟢 Online" if diff < 120 else "⚠️ Offline"
            msg = f"📊 **Report Automatico**\n{status}\nUltimo msg: {last['username']} ({diff} min fa)"
        else:
            msg = "📊 **Report Automatico**\n❌ Database vuoto."
        for admin_id in GROUP_ADMINS:
            try: await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
            except: pass
    except Exception as e: logger.error(f"Job Error: {e}")

# --- HANDLERS BOT ---
async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_MONITOR:
        user = update.effective_user
        if not user or user.is_bot: return
        now_utc = datetime.utcnow()
        username = f"@{user.username}" if user.username else user.full_name
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {"username": username, "last_seen": now_utc, "last_text": update.message.text[:100] if update.message.text else "No text"}}, 
            upsert=True
        )
        messages_col.insert_one({"username": username, "timestamp": now_utc})

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    status_msg = await update.message.reply_text("🔄 Sincronizzazione in corso...")
    all_users = list(users_col.find())
    gone_ids, gone_names = [], []
    for index, user in enumerate(all_users):
        try:
            member = await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
            if member.status in ['left', 'kicked']:
                gone_ids.append(user['user_id']); gone_names.append(user['username'])
        except: gone_ids.append(user['user_id']); gone_names.append(user['username'])
        if (index + 1) % 10 == 0 or (index + 1) == len(all_users):
            try: await status_msg.edit_text(f"⏳ Verificati: {index+1}/{len(all_users)}\nUsciti: {len(gone_ids)}")
            except: pass
        await asyncio.sleep(0.2)
    if gone_ids:
        context.user_data['pending_delete'] = gone_ids
        elenco = "\n".join(f"- {u}" for u in gone_names[:15])
        keyboard = [[InlineKeyboardButton("🗑️ Conferma Eliminazione", callback_data="do_delete")]]
        await status_msg.edit_text(f"⚠️ **Trovati {len(gone_ids)} usciti:**\n\n{elenco}", reply_markup=InlineKeyboardMarkup(keyboard))
    else: await status_msg.edit_text("✅ Database già sincronizzato.")

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days, target = int(context.args[0]), context.args[1]
        limit = datetime.utcnow() - timedelta(days=days)
        count = messages_col.count_documents({"username": target, "timestamp": {"$gte": limit}})
        await update.message.reply_text(f"📊 {target}: **{count}** msg negli ultimi {days}gg.")
    except: await update.message.reply_text("❌ Uso: `/count 7 @username`")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days = int(context.args[0])
        limit = datetime.utcnow() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit}}).limit(30)
        res = f"⚠️ **Inattivi da {days}gg:**\n" + "\n".join([f"- {u['username']} ({u['last_seen'].strftime('%d/%m')})" for u in inactive])
        await update.message.reply_text(res if "last_seen" in res else "✅ Tutti attivi!")
    except: await update.message.reply_text("❌ Uso: `/list 5`")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}]
    results = list(messages_col.aggregate(pipeline))
    res = "📊 **Classifica Totale:**\n" + "\n".join([f"- {i['_id']}: {i['total']}" for i in results])
    await update.message.reply_text(res[:4000])

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        u = users_col.find_one({"username": context.args[0]})
        if u: await update.message.reply_text(f"👤 {u['username']}\nVisto: {u['last_seen'].strftime('%d/%m %H:%M')}\nUltimo msg: `{u['last_text']}`", parse_mode="Markdown")
        else: await update.message.reply_text("❌ Non trovato.")
    except: await update.message.reply_text("❌ Uso: `/user @username`")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        target = context.args[0]
        r1 = users_col.delete_one({"username": target})
        r2 = messages_col.delete_many({"username": target})
        await update.message.reply_text(f"🗑️ {target} eliminato (Profilo: {r1.deleted_count}, Msg: {r2.deleted_count})")
    except: await update.message.reply_text("❌ Uso: `/clean @username`")

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    last = messages_col.find_one(sort=[("timestamp", -1)])
    if last:
        diff = int((datetime.utcnow() - last['timestamp']).total_seconds() // 60)
        status = "🟢 Online" if diff < 120 else "⚠️ Offline"
        await update.message.reply_text(f"📊 **Test Stato**\n{status}\nUltimo msg: {last['username']} ({diff} min fa)")
    else: await update.message.reply_text("❌ Database vuoto.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id not in GROUP_ADMINS: return
    await query.answer()
    if query.data == "do_delete":
        ids = context.user_data.get('pending_delete', [])
        if ids: users_col.delete_many({"user_id": {"$in": ids}})
        await query.edit_message_text(f"✅ Rimossi {len(ids)} record.")
        context.user_data['pending_delete'] = []

# --- AVVIO ---
def main():
    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(2)
    app = Application.builder().token(TOKEN).build()
    
    # Pianificazione Report (08:00 e 21:30 ITA)
    app.job_queue.run_daily(perform_status_check, time=dt.time(hour=8, minute=0, tzinfo=ITALY_TZ))
    app.job_queue.run_daily(perform_status_check, time=dt.time(hour=22, minute=40, tzinfo=ITALY_TZ))

    # Handlers
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("count", count_messages))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("total", total_messages))
    app.add_handler(CommandHandler("user", get_user))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info(f"🚀 Bot avviato per {len(GROUP_ADMINS)} gruppi admin.")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
