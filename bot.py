import os
import threading
import asyncio
from flask import Flask
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError, RetryAfter
from pymongo import MongoClient
from werkzeug.serving import make_server

# --- SERVER WEB OTTIMIZZATO PER CRON JOB ---
webapp = Flask(__name__)

@webapp.route('/')
def home(): 
    return "OK", 200

class ServerThread(threading.Thread):
    def __init__(self, app):
        threading.Thread.__init__(self, daemon=True)
        port = int(os.environ.get('PORT', 8080))
        self.srv = make_server('0.0.0.0', port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        print(f"🌐 Server Web in ascolto sulla porta {os.environ.get('PORT', 8080)}...")
        self.srv.serve_forever()

# --- CONFIGURAZIONE ---
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
GROUP_ADMIN = int(os.getenv("GROUP_ADMIN", 0))

# Connessione al DB con timeout per evitare blocchi infiniti all'avvio
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client.monitor_bot
users_col = db.users
messages_col = db.messages
messages_col.create_index("timestamp", expireAfterSeconds=7776000)

def get_now():
    return datetime.utcnow() + timedelta(hours=1)

# --- GESTORE ERRORI GLOBALE (ANTI-CRASH) ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"⚠️ Errore rilevato: {context.error}")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ Si è verificato un problema tecnico, ma il bot è operativo.")
        except: pass

# --- LOGICA TRACKING ---
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

# --- COMANDO REFRESH (ALLEGGERITO PER LA CPU) ---
async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    
    users_col.delete_many({"$or": [{"username": {"$exists": False}}, {"user_id": {"$exists": False}}, {"username": None}]})
    status_msg = await update.message.reply_text("🔄 Sincronizzazione avviata. Controllo utenti...")
    
    all_users = list(users_col.find())
    gone_ids, gone_names = [], []

    for user in all_users:
        try:
            member = await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
            if member.status in ['left', 'kicked']:
                gone_ids.append(user['user_id'])
                gone_names.append(user['username'])
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            continue
        except (BadRequest, TelegramError):
            gone_ids.append(user['user_id'])
            gone_names.append(user['username'])
        
        # Delay leggermente aumentato (0.2s) per lasciare risorse a Flask/Cron Job
        await asyncio.sleep(0.2)

    if gone_ids:
        context.user_data['pending_delete'] = gone_ids
        elenco = "\n".join(f"- {u}" for u in gone_names[:15])
        keyboard = [[InlineKeyboardButton("🗑️ Elimina Usciti", callback_data="confirm_delete")]]
        await status_msg.edit_text(
            f"🔄 **Scan concluso.**\n\n⚠️ **Utenti usciti ({len(gone_ids)}):**\n{elenco}{'...' if len(gone_ids) > 15 else ''}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await status_msg.edit_text("✅ Database pulito. Tutti i membri sono presenti.")

# --- GESTIONE CALLBACK ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_delete":
        keyboard = [[InlineKeyboardButton("✅ PROCEDI", callback_data="do_delete")],
                    [InlineKeyboardButton("❌ ANNULLA", callback_data="cancel_delete")]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "do_delete":
        ids_to_remove = context.user_data.get('pending_delete', [])
        if ids_to_remove:
            # Recuperiamo i nomi prima di cancellarli per pulire anche i messaggi
            names = [u['username'] for u in users_col.find({"user_id": {"$in": ids_to_remove}})]
            users_col.delete_many({"user_id": {"$in": ids_to_remove}})
            messages_col.delete_many({"username": {"$in": names}})
            await query.edit_message_text(f"✅ Pulizia completata! Rimossi {len(ids_to_remove)} record.")
            context.user_data['pending_delete'] = []
        else:
            await query.edit_message_text("⚠️ Nessun dato da rimuovere.")

    elif query.data == "cancel_delete":
        await query.edit_message_text("❌ Operazione annullata.")

# --- ALTRI COMANDI ---
async def list_inactive(update, context):
    try:
        if not context.args:
            return await update.message.reply_text("Uso: `/list 5`", parse_mode="Markdown")
        days = int(context.args[0])
        limit_date = get_now() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(30)
        res = f"⚠️ Inattivi da {days}gg:\n" + "\n".join([f"- {u['username']}" for u in inactive])
        await update.message.reply_text(res)
    except: await update.message.reply_text("❌ Errore. Uso: `/list 7`")

async def total_messages(update, context):
    pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}, {"$limit": 40}]
    results = list(messages_col.aggregate(pipeline))
    res = "📊 **Classifica:**\n" + "\n".join([f"- {i['_id']}: {i['total']}" for i in results])
    await update.message.reply_text(res[:4000])

async def clean_user(update, context):
    try:
        name = " ".join(context.args)
        if not name: return await update.message.reply_text("Specifica un nome.")
        users_col.delete_one({"username": name})
        messages_col.delete_many({"username": name})
        await update.message.reply_text(f"✅ {name} eliminato.")
    except: pass

# --- MAIN ---
def main():
    # 1. Avvia Flask in un thread dedicato e isolato
    server = ServerThread(webapp)
    server.start()

    # 2. Avvia il Bot
    app = Application.builder().token(TOKEN).build()
    
    app.add_error_handler(error_handler)
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("total", total_messages))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("🚀 Bot operativo.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
