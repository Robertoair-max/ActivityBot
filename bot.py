import os
import threading
from flask import Flask
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
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

async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_MONITOR:
        user = update.effective_user
        if not user or user.is_bot: return
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "username": f"@{user.username}" if user.username else user.full_name,
                "last_seen": datetime.utcnow(),
                "last_text": update.message.text[:100] if update.message.text else "No text"
            }}, upsert=True
        )

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        days = int(context.args[0])
        limit = datetime.utcnow() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit}})
        res = f"⚠️ Inattivi > {days}gg:\n"
        for u in inactive: res += f"- {u['username']} ({u['last_seen'].strftime('%d/%m')})\n"
        await update.message.reply_text(res if "-" in res else "Tutti attivi!")
    except: await update.message.reply_text("Uso: /list 5")

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    user_data = users_col.find_one({"username": context.args[0]})
    if user_data: await update.message.reply_text(f"👤 {user_data['username']}\nUltimo: {user_data['last_seen']}")
    else: await update.message.reply_text("Non trovato.")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    users_col.delete_one({"username": context.args[0]})
    await update.message.reply_text("Eliminato.")

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    users_col.delete_many({"$or": [{"username": None}, {"last_seen": None}]})
    await update.message.reply_text("DB Pulito.")

def main():
    # Avvia Flask in un thread separato
    threading.Thread(target=run_flask, daemon=True).start()
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("user", get_user))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CommandHandler("refresh", refresh))
    app.run_polling()

if __name__ == "__main__":
    main()
