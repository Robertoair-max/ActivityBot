import os
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from pymongo import MongoClient

# CONFIGURAZIONE (Sostituisci con i tuoi dati o usa variabili d'ambiente)
TOKEN = "8569217825:AAGi3QjP1F4m1LMuPHI_uDimxPBC5X2E4_w"
MONGO_URI = "mongodb+srv://ActivityBot:wNRWk5uXSH8alHQn@cluster0.v8ffddn.mongodb.net/?appName=Cluster0"
GROUP_MONITOR = -1003806281313  # ID Gruppo da monitorare
GROUP_ADMIN = -1003656422733   # ID Gruppo Amministrazione

# Connessione DB
client = MongoClient(MONGO_URI)
db = client.monitor_bot
users_col = db.users

# --- MONITORAGGIO ---
async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_MONITOR:
        user = update.effective_user
        if user.is_bot: return
        
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "username": f"@{user.username}" if user.username else user.full_name,
                "last_seen": datetime.utcnow(),
                "last_text": update.message.text[:100] # Salva i primi 100 caratteri
            }},
            upsert=True
        )

# --- COMANDI ADMIN ---
async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        days = int(context.args[0])
        limit_date = datetime.utcnow() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit_date}})
        
        res = f"⚠️ Utenti inattivi da oltre {days} giorni:\n"
        for u in inactive:
            res += f"- {u['username']} (Ultimo: {u['last_seen'].strftime('%d/%m/%Y')})\n"
        await update.message.reply_text(res if " - " in res else "Nessun utente inattivo trovato.")
    except:
        await update.message.reply_text("Uso: /list <giorni>")

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    username = context.args[0]
    user_data = users_col.find_one({"username": username})
    if user_data:
        await update.message.reply_text(f"Profilo {username}:\nData: {user_data['last_seen']}\nMessaggio: {user_data['last_text']}")
    else:
        await update.message.reply_text("Utente non trovato nel database.")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    username = context.args[0]
    users_col.delete_one({"username": username})
    await update.message.reply_text(f"Record di {username} eliminato.")

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    # Pulisce righe vuote o corrotte
    users_col.delete_many({"$or": [{"username": None}, {"last_seen": None}]})
    await update.message.reply_text("Database pulito. Il bot è attivo.")

# --- MAIN ---
def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("user", get_user))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CommandHandler("refresh", refresh))
    
    print("Bot in ascolto...")
    app.run_polling()

if __name__ == "__main__":
    main()
