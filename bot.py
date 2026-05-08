import os
import threading
import asyncio
import logging
import datetime as dt
from datetime import datetime, timedelta, timezone
import pytz
from flask import Flask
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode

# --- CONFIGURAZIONE E LOGGING ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

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

# --- SERVER WEB (FLASK) ---
webapp = Flask(__name__)

@webapp.route('/')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    try:
        webapp.run(host='0.0.0.0', port=port, threaded=True, debug=False, use_reloader=False)
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
        logger.error(f"Errore tracking: {e}")

# --- REPORT STATO ---
def create_status_report():
    try:
        last = messages_col.find_one(sort=[("timestamp", -1)])
        if last:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            diff_min = int((now_utc - last['timestamp']).total_seconds() // 60)
            status = "🟢 Online" if diff_min < 120 else "⚠️ Offline"
            ora_it = last['timestamp'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%H:%M:%S')
            return (f"<b>📊 Report Stato Bot</b>\n{status}\n\n"
                    f"👤 Ultimo: {last['username']}\n"
                    f"⏰ Ora ITA: {ora_it}\n"
                    f"⏳ Ritardo: {max(0, diff_min)} min fa")
        return "<b>📊 Report Stato Bot</b>\n❌ Nessun messaggio nel database."
    except Exception as e:
        logger.error(f"Errore generazione report: {e}")
        return "❌ Errore durante il report."

async def perform_status_check(context: ContextTypes.DEFAULT_TYPE):
    msg = create_status_report()
    for admin_id in GROUP_ADMINS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode=ParseMode.HTML)
        except:
            pass

# --- HANDLERS COMANDI E CALLBACK ---

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    msg = await update.message.reply_text("🔄 Sincronizzazione profonda in corso...")
    try:
        all_u = list(users_col.find())
        gone_ids, gone_names = [], []
        for i, u in enumerate(all_u):
            try:
                m = await context.bot.get_chat_member(GROUP_MONITOR, u['user_id'])
                if m.status in ['left', 'kicked']: 
                    gone_ids.append(u['user_id'])
                    gone_names.append(u['username'])
            except:
                gone_ids.append(u['user_id'])
                gone_names.append(u['username'])
            if (i+1) % 10 == 0: 
                await msg.edit_text(f"⏳ Verifica membri: {i+1}/{len(all_u)}")
            await asyncio.sleep(0.1)

        current_valid_names = [u['username'] for u in all_u if u['user_id'] not in gone_ids]
        orphans_count = messages_col.count_documents({"username": {"$nin": current_valid_names}})

        if gone_ids or orphans_count > 0:
            context.user_data['pending_del_ids'] = gone_ids
            context.user_data['pending_del_names'] = gone_names
            
            info_text = f"⚠️ <b>Analisi completata:</b>\n\n"
            if gone_ids:
                nomi_lista = "\n".join([f"• <i>{name}</i>" for name in gone_names])
                info_text += f"Utenti usciti (<b>{len(gone_ids)}</b>):\n{nomi_lista}\n\n"
            if orphans_count:
                info_text += f"Messaggi orfani: <b>{orphans_count}</b>\n"
            
            kb = [[InlineKeyboardButton("🗑️ Sincronizza Ora", callback_data="do_del")], 
                  [InlineKeyboardButton("❌ Annulla", callback_data="can_del")]]
            await msg.edit_text(f"{info_text}<i>L'azione pulirà i totali eliminando ogni dato orfano.</i>", 
                                reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text("✅ Database già sincronizzato. Nessun utente uscito.")
    except Exception as e:
        logger.error(f"Errore refresh: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        if q.message.chat.id not in GROUP_ADMINS: return
        await q.answer()
        if q.data == "do_del":
            ids = context.user_data.get('pending_del_ids', [])
            names = context.user_data.get('pending_del_names', [])
            if ids:
                users_col.delete_many({"user_id": {"$in": ids}})
            
            valid_usernames = [u['username'] for u in users_col.find({}, {"username": 1})]
            res = messages_col.delete_many({"username": {"$nin": valid_usernames}})
            
            nomi_rimossi = "\n".join(names) if names else "nessuno"
            await q.edit_message_text(f"✅ <b>Bonifica completata!</b>\n\n👤 <b>Rimossi:</b>\n{nomi_rimossi}\n\n📊 <b>Messaggi orfani:</b> {res.deleted_count}", parse_mode=ParseMode.HTML)
        else:
            await q.edit_message_text("❌ Operazione annullata.")
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Errore pulsanti: {e}")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    status_msg = await update.message.reply_text("⏳ Calcolo classifica completa...")
    try:
        def get_data():
            pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}]
            return list(messages_col.aggregate(pipeline))
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, get_data)
        if not results:
            await status_msg.edit_text("📊 Database vuoto.")
            return
        await status_msg.delete()
        lines = [f"- {i['_id']}: <b>{i['total']}</b>" for i in results]
        chunk_size = 100
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            titolo = f"<b>📊 Classifica Messaggi (Parte {i//chunk_size + 1}):</b>\n"
            await context.bot.send_message(chat_id=update.effective_chat.id, text=titolo + "\n".join(chunk), parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Errore classifica: {e}")

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days, target = int(context.args[0]), context.args[1]
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        count = messages_col.count_documents({"username": target, "timestamp": {"$gte": limit}})
        await update.message.reply_text(f"📊 {target}: <b>{count}</b> msg in {days}gg.", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Uso: <code>/count 7 @username</code>", parse_mode=ParseMode.HTML)

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days = int(context.args[0])
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        inactive = list(users_col.find({
            "last_seen": {"$lt": limit},
            "username": {"$ne": "@Simnap87"}
        }).limit(30))
        lines = [f"- {u['username']} ({u['last_seen'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%d/%m')})" for u in inactive]
        await update.message.reply_text(f"⚠️ <b>Inattivi da {days}gg:</b>\n" + "\n".join(lines) if lines else "✅ Tutti attivi!", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Uso: <code>/list 5</code>", parse_mode=ParseMode.HTML)

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        target = context.args[0]
        users_col.delete_one({"username": target})
        messages_col.delete_many({"username": target})
        await update.message.reply_text(f"🗑️ Dati di {target} rimossi.")
    except:
        await update.message.reply_text("❌ Uso: <code>/clean @username</code>", parse_mode=ParseMode.HTML)

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        target = context.args[0]
        u = users_col.find_one({"username": target})
        if u:
            ora_it = u['last_seen'].replace(tzinfo=pytz.UTC).astimezone(ITALY_TZ).strftime('%d/%m %H:%M')
            await update.message.reply_text(f"👤 {u['username']}\nVisto ITA: {ora_it}\nUltimo msg: <code>{u['last_text']}</code>", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("❌ Utente non trovato.")
    except:
        await update.message.reply_text("❌ Uso: <code>/user @username</code>", parse_mode=ParseMode.HTML)

async def today_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calcola i messaggi inviati oggi (dalla mezzanotte italiana)"""
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        now_it = datetime.now(ITALY_TZ)
        start_of_day_it = now_it.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_day_utc = start_of_day_it.astimezone(pytz.UTC).replace(tzinfo=None)
        
        count = messages_col.count_documents({"timestamp": {"$gte": start_of_day_utc}})
        await update.message.reply_text(f"📅 <b>Messaggi oggi:</b> {count}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Errore /today: {e}")

async def test_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    msg = create_status_report()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# --- MAIN ---
def main():
    if not TOKEN:
        logger.error("BOT_TOKEN non configurato!")
        return
    try:
        app = Application.builder().token(TOKEN).build()
        if app.job_queue:
            app.job_queue.run_daily(perform_status_check, time=dt.time(hour=8, minute=0, tzinfo=ITALY_TZ))
            app.job_queue.run_daily(perform_status_check, time=dt.time(hour=14, minute=0, tzinfo=ITALY_TZ))
            app.job_queue.run_daily(perform_status_check, time=dt.time(hour=20, minute=0, tzinfo=ITALY_TZ))
        
        app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
        app.add_handler(CommandHandler("total", total_messages))
        app.add_handler(CommandHandler("today", today_messages))
        app.add_handler(CommandHandler("count", count_messages))
        app.add_handler(CommandHandler("list", list_inactive))
        app.add_handler(CommandHandler("clean", clean_user))
        app.add_handler(CommandHandler("user", get_user))
        app.add_handler(CommandHandler("refresh", refresh))
        app.add_handler(CommandHandler("status", test_manual))
        app.add_handler(CallbackQueryHandler(button_handler))

        logger.info("🚀 Bot avviato...")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"❌ Errore avvio bot: {e}")

if __name__ == '__main__':
    main()
