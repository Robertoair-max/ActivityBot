import os
import threading
import asyncio
import logging
import time
import datetime as dt
from datetime import datetime, timedelta, timezone
import pytz
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes

# --- CONFIGURAZIONE ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
ADMIN_ENV = os.getenv("GROUP_ADMIN", "0")
GROUP_ADMINS = [int(i.strip()) for i in ADMIN_ENV.split(",") if i.strip()]
ITALY_TZ = pytz.timezone('Europe/Rome')

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.monitor_bot
    users_col = db.users
    messages_col = db.messages
    messages_col.create_index("timestamp", expireAfterSeconds=7776000)
    logger.info("✅ MongoDB Connesso")
except Exception as e:
    logger.error(f"❌ Errore MongoDB: {e}")

# --- SERVER WEB ---
webapp = Flask(__name__)
@webapp.route('/')
def health(): return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    webapp.run(host='0.0.0.0', port=port, threaded=True)

threading.Thread(target=run_flask, daemon=True).start()

# --- TRACKING (SALVATAGGIO IN UTC PURO) ---
async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_MONITOR:
        user = update.effective_user
        if not user or user.is_bot: return
        
        # Salviamo l'oggetto datetime UTC "nudo" (senza offset manuali)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        username = f"@{user.username}" if user.username else user.full_name
        
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {"username": username, "last_seen": now_utc, "last_text": (update.message.text or "")[:100]}}, 
            upsert=True
        )
        messages_col.insert_one({"username": username, "timestamp": now_utc})

# --- LOGICA REPORT (CALCOLO IN UTC E VISUALIZZAZIONE IN ITA) ---
async def perform_status_check(context: ContextTypes.DEFAULT_TYPE):
    try:
        last = messages_col.find_one(sort=[("timestamp", -1)])
        if last:
            # Calcolo in UTC puro
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            diff_min = int((now_utc - last['timestamp']).total_seconds() // 60)
            
            status = "🟢 Online" if diff_min < 120 else "⚠️ Offline"
            
            # Conversione in Ora Italiana per il messaggio (UTC -> ITA)
            last_utc = last['timestamp'].replace(tzinfo=pytz.UTC)
            ora_it = last_utc.astimezone(ITALY_TZ).strftime('%H:%M:%S')
            
            msg = (f"📊 **Report Stato**\n{status}\n\n"
                   f"👤 Ultimo: {last['username']}\n"
                   f"⏰ Ora ITA: {ora_it}\n"
                   f"⏳ Ritardo: {max(0, diff_min)} min fa")
        else:
            msg = "📊 **Report**\n❌ Nessun dato trovato."
        
        for admin_id in GROUP_ADMINS:
            try: await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
            except: pass
    except Exception as e: logger.error(f"Errore Job: {e}")

# --- HANDLERS COMANDI ---
async def test_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    last = messages_col.find_one(sort=[("timestamp", -1)])
    if last:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        diff_min = int((now_utc - last['timestamp']).total_seconds() // 60)
        status = "🟢 Online" if diff_min < 120 else "⚠️ Offline"
        last_utc = last['timestamp'].replace(tzinfo=pytz.UTC)
        ora_it = last_utc.astimezone(ITALY_TZ).strftime('%H:%M:%S')
        await update.message.reply_text(f"📊 **Test Manuale**\n{status}\nOra ITA: {ora_it}\nRitardo: {max(0, diff_min)} min fa")
    else: await update.message.reply_text("❌ DB Vuoto.")

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    m = await update.message.reply_text("🔄 Sincronizzazione...")
    users = list(users_col.find())
    gone_ids, gone_names = [], []
    for i, u in enumerate(users):
        try:
            mem = await context.bot.get_chat_member(GROUP_MONITOR, u['user_id'])
            if mem.status in ['left', 'kicked']: gone_ids.append(u['user_id']); gone_names.append(u['username'])
        except: gone_ids.append(u['user_id']); gone_names.append(u['username'])
        if (i+1) % 10 == 0: await m.edit_text(f"⏳ Verificati: {i+1}/{len(users)}")
        await asyncio.sleep(0.2)
    if gone_ids:
        context.user_data['pending_delete'] = gone_ids
        kb = [[InlineKeyboardButton("🗑️ Elimina", callback_data="do_del")]]
        await m.edit_text(f"⚠️ Trovati {len(gone_ids)} usciti.", reply_markup=InlineKeyboardMarkup(kb))
    else: await m.edit_text("✅ Tutto sincronizzato.")

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days, target = int(context.args[0]), context.args[1]
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        count = messages_col.count_documents({"username": target, "timestamp": {"$gte": limit}})
        await update.message.reply_text(f"📊 {target}: **{count}** msg negli ultimi {days}gg.")
    except: await update.message.reply_text("Uso: `/count 7 @username`")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days = int(context.args[0])
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit}}).limit(30)
        lines = [f"- {u['username']} ({u['last_seen'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%d/%m %H:%M')})" for u in inactive]
        await update.message.reply_text(f"⚠️ Inattivi:\n" + "\n".join(lines) if lines else "✅ Tutti attivi!")
    except: await update.message.reply_text("Uso: `/list 5`")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}]
    res = "📊 Classifica:\n" + "\n".join([f"- {i['_id']}: {i['total']}" for i in messages_col.aggregate(pipeline)])
    await update.message.reply_text(res[:4000])

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        target = context.args[0]
        users_col.delete_one({"username": target})
        messages_col.delete_many({"username": target})
        await update.message.reply_text(f"🗑️ {target} rimosso.")
    except: await update.message.reply_text("Uso: `/clean @username`")

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        u = users_col.find_one({"username": context.args[0]})
        if u:
            ora_it = u['last_seen'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%d/%m %H:%M')
            await update.message.reply_text(f"👤 {u['username']}\nVisto ITA: {ora_it}\nMsg: `{u['last_text']}`", parse_mode="Markdown")
        else: await update.message.reply_text("❌ Non trovato.")
    except: await update.message.reply_text("Uso: `/user @username`")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "do_del" and q.message.chat.id in GROUP_ADMINS:
        ids = context.user_data.get('pending_delete', [])
        if ids: users_col.delete_many({"user_id": {"$in": ids}})
        await q.edit_message_text(f"✅ Rimossi {len(ids)} record.")

def main():
    app = Application.builder().token(TOKEN).build()
    
    # Orari Report ITA (08:00 e 23:05)
    app.job_queue.run_daily(perform_status_check, time=dt.time(hour=8, minute=0, tzinfo=ITALY_TZ))
    app.job_queue.run_daily(perform_status_check, time=dt.time(hour=23, minute=30, tzinfo=ITALY_TZ))

    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("test", test_manual))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("count", count_messages))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("total", total_messages))
    app.add_handler(CommandHandler("user", get_user))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
