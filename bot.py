import os
import threading
import asyncio
from flask import Flask
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError
from pymongo import MongoClient

# --- SERVER WEB PER TENERE SVEGLIO IL BOT ---
webapp = Flask(__name__)

@webapp.route('/')
def home(): return "Bot is Alive!"

def run_flask():
    webapp.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# --- LOGICA BOT ---
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
GROUP_ADMIN = int(os.getenv("GROUP_ADMIN", 0))

client = MongoClient(MONGO_URI)
db = client.monitor_bot
users_col = db.users
messages_col = db.messages

# Indice TTL per cancellare i messaggi dopo 90 giorni
messages_col.create_index("timestamp", expireAfterSeconds=7776000)

def get_now():
    return datetime.utcnow() + timedelta(hours=1)

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
            }}, upsert=True
        )
        
        messages_col.insert_one({
            "username": username,
            "timestamp": now
        })

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        days = int(context.args[0])
        target_username = context.args[1]
        search_days = min(days, 90)
        limit_date = get_now() - timedelta(days=search_days)
        
        count = messages_col.count_documents({
            "username": target_username,
            "timestamp": {"$gte": limit_date}
        })
        
        await update.message.reply_text(f"📊 {target_username} inviato **{count}** messaggi negli ultimi {search_days} giorni.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Uso: `/count 7 @username` (max 90gg)")

async def test_last_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    last_msg = messages_col.find_one(sort=[("timestamp", -1)])
    if last_msg:
        now = get_now()
        diff = now - last_msg['timestamp']
        is_online = diff.total_seconds() < 7200
        status_icon = "✅" if is_online else "⚠️"
        await update.message.reply_text(
            f"🔍 **Stato Monitoraggio:** {'Online' if is_online else 'Offline'} {status_icon}\n"
            f"👤 Ultimo: {last_msg['username']}\n"
            f"⏳ Ritardo: {int(diff.total_seconds() // 60)} min"
        )
    else:
        await update.message.reply_text("❌ Nessun messaggio nel DB.")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}]
    results = list(messages_col.aggregate(pipeline))
    if not results:
        await update.message.reply_text("Nessun dato.")
        return
    res = "📊 **Classifica Totale:**\n"
    for item in results:
        riga = f"- {item['_id']}: {item['total']}\n"
        if len(res) + len(riga) > 4000:
            await update.message.reply_text(res)
            res = ""
        res += riga
    await update.message.reply_text(res)

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        days = int(context.args[0])
        limit_date = get_now() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(30)
        res = f"⚠️ Inattivi da {days}gg:\n"
        count = 0
        for u in inactive:
            res += f"- {u['username']} ({u['last_seen'].strftime('%d/%m %H:%M')})\n"
            count += 1
        await update.message.reply_text(res if count > 0 else "✅ Tutti attivi!")
    except: await update.message.reply_text("Uso: /list 5")

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    if not context.args: return
    user_data = users_col.find_one({"username": context.args[0]})
    if user_data:
        await update.message.reply_text(f"👤 {user_data['username']}\n📅 Ultima: {user_data['last_seen'].strftime('%d/%m %H:%M')}\n💬: {user_data['last_text']}")
    else:
        await update.message.reply_text("❌ Utente non trovato.")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    username = " ".join(context.args)
    res_user = users_col.delete_one({"username": username})
    res_msgs = messages_col.delete_many({"username": username})
    await update.message.reply_text(f"✅ {username} rimosso ({res_msgs.deleted_count} msg cancellati).")

# --- NUOVO COMANDO REFRESH POTENZIATO ---
async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    
    # 1. Rimuove righe corrotte
    users_col.delete_many({"$or": [{"username": {"$exists": False}}, {"user_id": {"$exists": False}}, {"username": None}]})
    
    status_msg = await update.message.reply_text("🔄 Sincronizzazione database con il gruppo...")
    
    all_users = list(users_col.find())
    gone_users = []

    for user in all_users:
        try:
            member = await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
            # Se lo stato è uno di questi, l'utente non è più nel gruppo
            if member.status in ['left', 'kicked']:
                gone_users.append(user['username'])
        except (BadRequest, TelegramError):
            # Se l'utente non è trovato o il bot non può vederlo, è considerato "uscito"
            gone_users.append(user['username'])
        
        # Piccolo delay per non saturare le API di Telegram se hai molti utenti
        await asyncio.sleep(0.05)

    if gone_users:
        elenco = "\n".join(f"- {u}" for u in gone_users)
        testo = f"🔄 **Database Riorganizzato.**\n\n⚠️ **Utenti usciti/bannati/kikkati:**\n{elenco}\n\n*Nota: Questi utenti restano nel DB finché non usi /clean, ma ora sai chi sono.*"
    else:
        testo = "🔄 **Database Riorganizzato.**\n✅ Tutti gli utenti nel DB sono presenti nel gruppo."

    await status_msg.edit_text(testo[:4096])

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("count", count_messages))
    app.add_handler(CommandHandler("user", get_user))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("test", test_last_msg))
    app.add_handler(CommandHandler("total", total_messages))
    
    print("🚀 Bot avviato...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
