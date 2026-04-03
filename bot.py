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
import requests

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

# --- SERVER WEB (Corretto per Render) ---

@webapp.route('/')
def health():
    # Usiamo make_response per poter aggiungere l'header di chiusura
    r = make_response("OK", 200)
    r.headers['Connection'] = 'close'
    return r

def run_flask():
    # Configurazione identica al secondo codice (più stabile)
    port = int(os.environ.get('PORT', 10000))
    webapp.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# Avvio del thread prima del bot
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

# --- REPORT STATO (LOGICA) ---
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

# --- HANDLERS COMANDI ---

async def test_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        msg = create_status_report()
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Errore test_manual: {e}")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    status_msg = await update.message.reply_text("⏳ Calcolo classifica completa...")
    try:
        def get_data():
            # Rimosso il limite per prendere tutti gli utenti
            pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}]
            return list(messages_col.aggregate(pipeline))
        
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, get_data)
        
        if not results:
            await status_msg.edit_text("📊 Database vuoto.")
            return

        # Cancelliamo il messaggio di attesa per iniziare l'invio dei blocchi
        await status_msg.delete()

        lines = [f"- {i['_id']}: <b>{i['total']}</b>" for i in results]
        
        # Suddividiamo in blocchi da 100 utenti
        chunk_size = 100
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            titolo = f"<b>📊 Classifica Messaggi (Parte {i//chunk_size + 1}):</b>\n"
            message_text = titolo + "\n".join(chunk)
            
            # Invio del blocco (limitato comunque a 4000 char per sicurezza interna di Telegram)
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=message_text[:4000], 
                parse_mode=ParseMode.HTML
            )
            # Piccolo delay per evitare il flood limit di Telegram
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"Errore classifica: {e}")
        await update.message.reply_text("❌ Errore nel calcolo della classifica completa.")

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days, target = int(context.args[0]), context.args[1]
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        count = messages_col.count_documents({"username": target, "timestamp": {"$gte": limit}})
        await update.message.reply_text(f"📊 {target}: <b>{count}</b> msg in {days}gg.", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Uso: <code>/count 7 @username</code>", parse_mode=ParseMode.HTML)

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        msg = await update.message.reply_text("🔄 Sincronizzazione in corso...")
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
            if (i+1) % 10 == 0: await msg.edit_text(f"⏳ Verificati: {i+1}/{len(all_u)}")
            await asyncio.sleep(0.2)
        
        if gone_ids:
            context.user_data['pending_del'] = gone_ids
            elenco = "\n".join([f"- {n}" for n in gone_names[:15]])
            kb = [[InlineKeyboardButton("🗑️ ELIMINA", callback_data="do_del")], [InlineKeyboardButton("❌ ANNULLA", callback_data="can_del")]]
            await msg.edit_text(f"⚠️ <b>Trovati {len(gone_ids)} usciti:</b>\n{elenco}", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text("✅ Tutti presenti e sincronizzati.")
    except Exception as e:
        logger.error(f"Errore refresh: {e}")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in GROUP_ADMINS: return
    try:
        days = int(context.args[0])
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        inactive = list(users_col.find({"last_seen": {"$lt": limit}}).limit(30))
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
        await update.message.reply_text(f"🗑️ Dati di {target} rimossi dal database.")
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        if q.message.chat.id not in GROUP_ADMINS: return
        await q.answer()
        if q.data == "do_del":
            ids = context.user_data.get('pending_del', [])
            if ids: users_col.delete_many({"user_id": {"$in": ids}})
            await q.edit_message_text(f"✅ Rimossi {len(ids)} record dal database.")
        else:
            await q.edit_message_text("❌ Operazione annullata.")
        context.user_data['pending_del'] = []
    except Exception as e:
        logger.error(f"Errore pulsanti: {e}")

async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    try:
        # Usa l'URL pubblico del tuo bot su Render
        url = "https://activitybot-md6m.onrender.com" 
        response = requests.get(url, timeout=10)
        logger.info(f"📡 Self-ping: Status {response.status_code}")
    except Exception as e:
        logger.error(f"⚠️ Self-ping fallito: {e}")

def main():
        # 1. Avvia Flask per primo (come nel secondo codice)
        threading.Thread(target=run_flask, daemon=True).start()
        time.sleep(2) # Dagli un attimo per aprire la porta 10000

        # 2. Configura il bot
        app = Application.builder().token(TOKEN).build()

        # Nel main(), dopo app = Application.builder()...
        app.job_queue.run_repeating(self_ping, interval=600, first=10)
        
        # Report schedulati (Fuso Italia)
        app.job_queue.run_daily(perform_status_check, time=dt.time(hour=8, minute=0, tzinfo=ITALY_TZ))
        app.job_queue.run_daily(perform_status_check, time=dt.time(hour=14, minute=0, tzinfo=ITALY_TZ))
        app.job_queue.run_daily(perform_status_check, time=dt.time(hour=20, minute=0, tzinfo=ITALY_TZ))
        
        # Handlers
        app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
        app.add_handler(CommandHandler("total", total_messages))
        app.add_handler(CommandHandler("count", count_messages))
        app.add_handler(CommandHandler("refresh", refresh))
        app.add_handler(CommandHandler("list", list_inactive))
        app.add_handler(CommandHandler("clean", clean_user))
        app.add_handler(CommandHandler("user", get_user))
        app.add_handler(CommandHandler("test", test_manual))
        app.add_handler(CallbackQueryHandler(button_handler))
        
        logger.info("🚀 Bot avviato e pronto!")
        app.run_polling()

if __name__ == '__main__':
    main()
