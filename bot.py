import os
import threading
import asyncio
import logging
import time
import datetime as dt
from datetime import datetime, timedelta
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

# Connessione MongoDB
client = MongoClient(MONGO_URI)
db = client.monitor_bot
users_col = db.users
messages_col = db.messages
messages_col.create_index("timestamp", expireAfterSeconds=7776000)

def get_now():
    return datetime.utcnow() + timedelta(hours=1)

# --- SERVER WEB (Keep-Alive Prioritario) ---
webapp = Flask(__name__)
@webapp.route('/')
def home(): return "Bot is Alive!", 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"📡 Flask avviato sulla porta {port}")
    webapp.run(host='0.0.0.0', port=port)

# --- LOGICA ATTIVITÀ ---
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

# --- COMANDO REFRESH OTTIMIZZATO (400 UTENTI) ---
async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    
    users_col.delete_many({"$or": [{"username": {"$exists": False}}, {"user_id": {"$exists": False}}]})
    all_users = list(users_col.find())
    total_to_check = len(all_users)
    
    if total_to_check == 0:
        await update.message.reply_text("✅ Database vuoto.")
        return

    status_msg = await update.message.reply_text(f"🔄 Inizio scansione di {total_to_check} utenti...")
    gone_ids, gone_names = [], []

    for index, user in enumerate(all_users):
        current_count = index + 1
        try:
            member = await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
            if member.status in ['left', 'kicked']:
                gone_ids.append(user['user_id']); gone_names.append(user['username'])
        except (BadRequest, TelegramError):
            gone_ids.append(user['user_id']); gone_names.append(user['username'])
        
        # Feedback ogni 10 utenti
        if current_count % 10 == 0 or current_count == total_to_check:
            try:
                await status_msg.edit_text(
                    f"⏳ **Sincronizzazione...**\nVerificati: `{current_count}` / `{total_to_check}`\nUsciti rilevati: `{len(gone_ids)}`",
                    parse_mode="Markdown"
                )
            except: pass

        # Anti-Flood Delay: 0.2s (Sicuro per Telegram)
        await asyncio.sleep(0.2)

    if gone_ids:
        context.user_data['pending_delete'] = gone_ids
        elenco = "\n".join(f"- {u}" for u in gone_names[:15])
        if len(gone_names) > 15: elenco += f"\n\n...e altri {len(gone_names)-15}"
        keyboard = [[InlineKeyboardButton("🗑️ Conferma eliminazione", callback_data="confirm_delete")]]
        await status_msg.edit_text(f"⚠️ **Trovati {len(gone_ids)} usciti:**\n\n{elenco}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await status_msg.edit_text(f"✅ Scansione finita. Tutti i {total_to_check} utenti sono nel gruppo.")

# --- ALTRI COMANDI CON GESTIONE ERRORI ---
async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        days, target = int(context.args[0]), context.args[1]
        limit_date = get_now() - timedelta(days=min(days, 90))
        count = messages_col.count_documents({"username": target, "timestamp": {"$gte": limit_date}})
        await update.message.reply_text(f"📊 {target}: **{count}** msg negli ultimi {days}gg.")
    except: await update.message.reply_text("❌ Uso: `/count 7 @username`", parse_mode="Markdown")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        days = int(context.args[0])
        limit_date = get_now() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(30)
        lines = [f"- {u['username']} ({u['last_seen'].strftime('%d/%m')})" for u in inactive]
        res = f"⚠️ **Inattivi da {days}gg:**\n" + "\n".join(lines) if lines else "✅ Tutti attivi!"
        await update.message.reply_text(res)
    except: await update.message.reply_text("❌ Uso: `/list 5`", parse_mode="Markdown")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}]
    results = list(messages_col.aggregate(pipeline))
    res = "📊 **Classifica Totale:**\n" + "\n".join([f"- {i['_id']}: {i['total']}" for i in results])
    await update.message.reply_text(res[:4000])

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        target = context.args[0]
        u = users_col.find_one({"username": target})
        if u: await update.message.reply_text(f"👤 {u['username']}\nVisto: {u['last_seen'].strftime('%d/%m %H:%M')}\nMsg: `{u['last_text']}`", parse_mode="Markdown")
        else: await update.message.reply_text("❌ Utente non trovato.")
    except: await update.message.reply_text("❌ Uso: `/user @username`", parse_mode="Markdown")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        target = " ".join(context.args)
        r1, r2 = users_col.delete_one({"username": target}), messages_col.delete_many({"username": target})
        await update.message.reply_text(f"🗑️ {target} rimosso (Record: {r1.deleted_count}, Msg: {r2.deleted_count})")
    except: await update.message.reply_text("❌ Uso: `/clean @username`", parse_mode="Markdown")

async def test_last_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    last = messages_col.find_one(sort=[("timestamp", -1)])
    if last:
        diff = int((get_now() - last['timestamp']).total_seconds() // 60)
        status = "🟢 Online" if diff < 120 else "⚠️ Offline (>2h)"
        await update.message.reply_text(f"{status}\nUltimo msg: {last['username']} ({diff} min fa)")
    else: await update.message.reply_text("❌ Nessun dato nel DB.")

# --- CALLBACK E AVVIO ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_delete":
        keyboard = [[InlineKeyboardButton("✅ PROCEDI", callback_data="do_delete")], [InlineKeyboardButton("❌ ANNULLA", callback_data="cancel_delete")]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "do_delete":
        ids = context.user_data.get('pending_delete', [])
        if ids: users_col.delete_many({"user_id": {"$in": ids}})
        await query.edit_message_text(f"✅ Rimossi {len(ids)} record.")
    elif query.data == "cancel_delete": await query.edit_message_text("❌ Operazione annullata.")

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(2)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("count", count_messages))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("total", total_messages))
    app.add_handler(CommandHandler("user", get_user))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CommandHandler("test", test_last_msg))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
