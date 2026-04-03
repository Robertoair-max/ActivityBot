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

# --- CONFIGURAZIONE E LOGGING ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
ADMIN_ENV = os.getenv("GROUP_ADMIN", "0")
GROUP_ADMINS = [int(i.strip()) for i in ADMIN_ENV.split(",") if i.strip()]
ITALY_TZ = pytz.timezone('Europe/Rome')

# --- CONNESSIONE MONGODB ---
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.monitor_bot
    users_col = db.users
    messages_col = db.messages
    messages_col.create_index("timestamp", expireAfterSeconds=7776000)
    logger.info("✅ MongoDB Connesso correttamente")
except Exception as e:
    logger.error(f"❌ Errore MongoDB: {e}")

# --- SERVER WEB ---
webapp = Flask(__name__)
@webapp.route('/')
def health(): return "OK", 200

def run_flask():
    try:
        port = int(os.environ.get('PORT', 10000))
        webapp.run(host='0.0.0.0', port=port, threaded=True)
    except Exception as e:
        logger.error(f"❌ Errore Flask: {e}")

threading.Thread(target=run_flask, daemon=True).start()

# --- LOGICA TRACKING ---
async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_chat.id == GROUP_MONITOR:
            user = update.effective_user
            if not user or user.is_bot: return
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            username = f"@{user.username}" if user.username else user.full_name
            users_col.update_one(
                {"user_id": user.id},
                {"$set": {"username": username, "last_seen": now_utc, "last_text": (update.message.text or "")[:100]}}, 
                upsert=True
            )
            messages_col.insert_one({"username": username, "timestamp": now_utc})
    except Exception as e:
        logger.error(f"Errore in track_activity: {e}")

# --- REPORT STATO ---
def create_status_report():
    try:
        last = messages_col.find_one(sort=[("timestamp", -1)])
        if last:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            diff_min = int((now_utc - last['timestamp']).total_seconds() // 60)
            status = "🟢 Online" if diff_min < 120 else "⚠️ Offline"
            last_utc = last['timestamp'].replace(tzinfo=pytz.UTC)
            ora_it = last_utc.astimezone(ITALY_TZ).strftime('%H:%M:%S')
            return (f"📊 **Report Stato Bot**\n{status}\n\n"
                    f"👤 Ultimo: {last['username']}\n"
                    f"⏰ Ora ITA: {ora_it}\n"
                    f"⏳ Ritardo: {max(0, diff_min)} min fa")
        return "📊 **Report Stato Bot**\n❌ Nessun messaggio nel database."
    except Exception as e:
        logger.error(f"Errore report: {e}")
        return "❌ Errore report."

async def perform_status_check(context: ContextTypes.DEFAULT_TYPE):
    msg = create_status_report()
    for admin_id in GROUP_ADMINS:
        try: await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
        except: pass

# --- HANDLERS COMANDI ---

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days = int(context.args[0])
        target = context.args[1]
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        count = messages_col.count_documents({"username": target, "timestamp": {"$gte": limit}})
        await update.message.reply_text(f"📊 {target}: **{count}** msg negli ultimi {days}gg.")
    except Exception as e:
        await update.message.reply_text("❌ Uso: `/count 7 @username`")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    
    # 1. Feedback immediato all'utente
    status_msg = await update.message.reply_text("⏳ Calcolo classifica in corso...")
    
    try:
        # 2. Definiamo l'operazione DB
        def get_data():
            pipeline = [
                {"$group": {"_id": "$username", "total": {"$sum": 1}}},
                {"$sort": {"total": -1}},
            ]
            # Usiamo list() per consumare il cursore
            return list(messages_col.aggregate(pipeline))

        # 3. Eseguiamo l'operazione in un thread separato per non bloccare il bot
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, get_data)

        if not results:
            await status_msg.edit_text("📊 Il database dei messaggi è vuoto.")
            return

        # 4. Costruiamo la stringa con gestione dei null
        lines = []
        for i in results:
            username = i.get('_id') or "Utente Sconosciuto"
            count = i.get('total', 0)
            lines.append(f"- {username}: **{count}**")
        
        res = "📊 **Classifica Messaggi:**\n" + "\n".join(lines)
        
        # 5. Modifichiamo il messaggio di attesa invece di inviarne uno nuovo
        await status_msg.edit_text(res[:4000], parse_mode="Markdown")

    except Exception as e:
        logger.error(f"❌ Errore critico in total_messages: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Errore tecnico: {str(e)}")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        target = context.args[0]
        users_col.delete_one({"username": target})
        messages_col.delete_many({"username": target})
        await update.message.reply_text(f"🗑️ Dati di {target} rimossi.")
    except Exception as e:
        await update.message.reply_text("❌ Uso: `/clean @username`")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days = int(context.args[0])
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        inactive = list(users_col.find({"last_seen": {"$lt": limit}}).limit(30))
        lines = [f"- {u['username']} ({u['last_seen'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%d/%m %H:%M')})" for u in inactive]
        await update.message.reply_text(f"⚠️ **Inattivi da {days}gg:**\n" + "\n".join(lines) if lines else "✅ Tutti attivi!")
    except:
        await update.message.reply_text("❌ Uso: `/list 5`")

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        target = context.args[0]
        u = users_col.find_one({"username": target})
        if u:
            ora_it = u['last_seen'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%d/%m %H:%M')
            await update.message.reply_text(f"👤 {u['username']}\nVisto ITA: {ora_it}\nUltimo msg: `{u['last_text']}`", parse_mode="Markdown")
        else: await update.message.reply_text("❌ Utente non trovato.")
    except: await update.message.reply_text("❌ Uso: `/user @username`")

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        status_msg = await update.message.reply_text("🔄 Sincronizzazione...")
        all_users = list(users_col.find())
        total_db = len(all_users)
        gone_ids, gone_names = [], []
        for i, u in enumerate(all_users):
            try:
                mem = await context.bot.get_chat_member(GROUP_MONITOR, u['user_id'])
                if mem.status in ['left', 'kicked']: 
                    gone_ids.append(u['user_id'])
                    gone_names.append(u['username'])
            except:
                gone_ids.append(u['user_id'])
                gone_names.append(u['username'])
            if (i+1) % 10 == 0 or (i+1) == total_db:
                try: await status_msg.edit_text(f"⏳ Verificati: {i+1}/{total_db}")
                except: pass
            await asyncio.sleep(0.2)
        if gone_ids:
            context.user_data['pending_delete'] = gone_ids
            elenco = "\n".join([f"- {n}" for n in gone_names[:20]])
            kb = [[InlineKeyboardButton("🗑️ ELIMINA", callback_data="do_del")], [InlineKeyboardButton("❌ ANNULLA", callback_data="cancel_del")]]
            await status_msg.edit_text(f"⚠️ **Trovati {len(gone_ids)} usciti:**\n{elenco}", reply_markup=InlineKeyboardMarkup(kb))
        else: await status_msg.edit_text("✅ Tutto sincronizzato.")
    except Exception as e: logger.error(f"Errore refresh: {e}")

async def test_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        await update.message.reply_text(create_status_report(), parse_mode="Markdown")
    except: pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        if query.message.chat.id not in GROUP_ADMINS: return
        await query.answer()
        if query.data == "do_del":
            ids = context.user_data.get('pending_delete', [])
            if ids: users_col.delete_many({"user_id": {"$in": ids}})
            await query.edit_message_text(f"✅ Rimossi {len(ids)} record.")
        elif query.data == "cancel_del":
            await query.edit_message_text("❌ Annullato.")
        context.user_data['pending_delete'] = []
    except: pass

def main():
    try:
        app = Application.builder().token(TOKEN).build()
        app.job_queue.run_daily(perform_status_check, time=dt.time(hour=8, minute=0, tzinfo=ITALY_TZ))
        app.job_queue.run_daily(perform_status_check, time=dt.time(hour=14, minute=0, tzinfo=ITALY_TZ))
        app.job_queue.run_daily(perform_status_check, time=dt.time(hour=20, minute=0, tzinfo=ITALY_TZ))
        
        app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
        app.add_handler(CommandHandler("refresh", refresh))
        app.add_handler(CommandHandler("count", count_messages))
        app.add_handler(CommandHandler("list", list_inactive))
        app.add_handler(CommandHandler("total", total_messages))
        app.add_handler(CommandHandler("user", get_user))
        app.add_handler(CommandHandler("clean", clean_user))
        app.add_handler(CommandHandler("test", test_manual))
        app.add_handler(CallbackQueryHandler(button_handler))
        
        logger.info("🚀 Bot in ascolto...")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Errore fatale: {e}")

if __name__ == '__main__':
    main()
