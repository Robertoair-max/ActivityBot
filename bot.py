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

# Gestione Multi-Admin: converte "ID1,ID2" in una lista di interi [ID1, ID2]
ADMIN_ENV = os.getenv("GROUP_ADMIN", "0")
GROUP_ADMINS = [int(i.strip()) for i in ADMIN_ENV.split(",") if i.strip()]

# Connessione MongoDB con Test Automatico
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

# --- SERVER WEB (Keep-Alive Render porta 10000) ---
webapp = Flask(__name__)
@webapp.route('/')
def home(): return "Bot is Alive!", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"📡 Flask avviato sulla porta {port}")
    webapp.run(host='0.0.0.0', port=port)

# --- FUNZIONE LOGICA TEST (MULTI-ADMIN) ---
async def perform_status_check(context: ContextTypes.DEFAULT_TYPE):
    last = messages_col.find_one(sort=[("timestamp", -1)])
    if last:
        diff = int((get_now() - last['timestamp']).total_seconds() // 60)
        status = "🟢 Online" if diff < 120 else "⚠️ Offline (Nessun msg da >2h)"
        msg = f"📊 **Report Stato Bot**\n{status}\n\n👤 Ultimo: {last['username']}\n⏰ Ora: {last['timestamp'].strftime('%H:%M:%S')}\n⏳ Ritardo: {diff} min fa"
    else:
        msg = "❌ Database messaggi vuoto."
    
    for admin_id in GROUP_ADMINS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Errore invio report a {admin_id}: {e}")

# --- HANDLERS LOGICA ---

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

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    users_col.delete_many({"$or": [{"username": {"$exists": False}}, {"user_id": {"$exists": False}}]})
    all_users = list(users_col.find())
    total = len(all_users)
    if total == 0: return await update.message.reply_text("Database vuoto.")

    status_msg = await update.message.reply_text(f"🔄 Sincronizzazione di {total} utenti...")
    gone_ids, gone_names = [], []

    for index, user in enumerate(all_users):
        curr = index + 1
        try:
            member = await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
            if member.status in ['left', 'kicked']:
                gone_ids.append(user['user_id']); gone_names.append(user['username'])
        except:
            gone_ids.append(user['user_id']); gone_names.append(user['username'])
        
        if curr % 10 == 0 or curr == total:
            try: await status_msg.edit_text(f"⏳ **Sincronizzazione...**\nVerificati: `{curr}` / `{total}`\nUsciti: `{len(gone_ids)}`", parse_mode="Markdown")
            except: pass
        await asyncio.sleep(0.2)

    if gone_ids:
        context.user_data['pending_delete'] = gone_ids
        elenco = "\n".join(f"- {u}" for u in gone_names[:15])
        if len(gone_names) > 15: elenco += f"\n...e altri {len(gone_names)-15}"
        keyboard = [[InlineKeyboardButton("🗑️ Conferma Eliminazione", callback_data="confirm_delete")]]
        await status_msg.edit_text(f"⚠️ **Trovati {len(gone_ids)} usciti:**\n\n{elenco}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await status_msg.edit_text(f"✅ Tutti i {total} utenti sono nel gruppo.")

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days, target = int(context.args[0]), context.args[1]
        limit_date = get_now() - timedelta(days=min(days, 90))
        count = messages_col.count_documents({"username": target, "timestamp": {"$gte": limit_date}})
        await update.message.reply_text(f"📊 {target}: **{count}** msg negli ultimi {days}gg.")
    except: await update.message.reply_text("❌ Uso: `/count 7 @username`")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days = int(context.args[0])
        limit_date = get_now() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(30)
        lines = [f"- {u['username']} ({u['last_seen'].strftime('%d/%m')})" for u in inactive]
        res = f"⚠️ **Inattivi da {days}gg:**\n" + "\n".join(lines) if lines else "✅ Tutti attivi!"
        await update.message.reply_text(res)
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
        target = context.args[0]
        u = users_col.find_one({"username": target})
        if u: await update.message.reply_text(f"👤 {u['username']}\nVisto: {u['last_seen'].strftime('%d/%m %H:%M')}\nMsg: `{u['last_text']}`", parse_mode="Markdown")
        else: await update.message.reply_text("❌ Non trovato.")
    except: await update.message.reply_text("❌ Uso: `/user @username`")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        target = " ".join(context.args)
        r1 = users_col.delete_one({"username": target})
        r2 = messages_col.delete_many({"username": target})
        await update.message.reply_text(f"🗑️ {target} rimosso (Record: {r1.deleted_count}, Msg: {r2.deleted_count})")
    except: await update.message.reply_text("❌ Uso: `/clean @username`")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id not in GROUP_ADMINS:
        return await query.answer("Non autorizzato.", show_alert=True)
    
    await query.answer()
    if query.data == "confirm_delete":
        keyboard = [[InlineKeyboardButton("✅ PROCEDI", callback_data="do_delete")], [InlineKeyboardButton("❌ ANNULLA", callback_data="cancel_delete")]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "do_delete":
        ids = context.user_data.get('pending_delete', [])
        if ids: users_col.delete_many({"user_id": {"$in": ids}})
        await query.edit_message_text(f"✅ Rimossi {len(ids)} record dal database.")
        context.user_data['pending_delete'] = []
    elif query.data == "cancel_delete": await query.edit_message_text("❌ Operazione annullata.")

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(2)
    app = Application.builder().token(TOKEN).build()
    jq = app.job_queue

    # Pianificazione (08:00 e 21:30 ITA -> 07:00 e 20:30 UTC)
    jq.run_daily(perform_status_check, time=datetime_time(hour=7, minute=0))
    jq.run_daily(perform_status_check, time=datetime_time(hour=20, minute=30))

    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("count", count_messages))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("total", total_messages))
    app.add_handler(CommandHandler("user", get_user))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CommandHandler("test", lambda u, c: perform_status_check(c)))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info(f"🚀 Bot avviato su Render per {len(GROUP_ADMINS)} gruppi admin.")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
