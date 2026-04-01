import os
import threading
import asyncio
from flask import Flask
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError
from pymongo import MongoClient

# --- SERVER WEB ---
webapp = Flask(__name__)
@webapp.route('/')
def home(): return "Bot is Alive!"

def run_flask():
    webapp.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# --- CONFIGURAZIONE ---
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GROUP_MONITOR = int(os.getenv("GROUP_MONITOR", 0))
GROUP_ADMIN = int(os.getenv("GROUP_ADMIN", 0))

client = MongoClient(MONGO_URI)
db = client.monitor_bot
users_col = db.users
messages_col = db.messages
messages_col.create_index("timestamp", expireAfterSeconds=7776000)

def get_now():
    return datetime.utcnow() + timedelta(hours=1)

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

# --- COMANDO REFRESH CON BOTTONE ---
async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    
    # Pulizia righe corrotte nel DB
    users_col.delete_many({"$or": [{"username": {"$exists": False}}, {"user_id": {"$exists": False}}, {"username": None}]})
    
    status_msg = await update.message.reply_text("🔄 Sincronizzazione in corso...")
    
    all_users = list(users_col.find())
    gone_ids = []
    gone_names = []

    for user in all_users:
        try:
            member = await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
            if member.status in ['left', 'kicked']:
                gone_ids.append(user['user_id'])
                gone_names.append(user['username'])
        except (BadRequest, TelegramError):
            gone_ids.append(user['user_id'])
            gone_names.append(user['username'])
        await asyncio.sleep(0.05)

    if gone_ids:
        # Salviamo temporaneamente la lista degli ID da eliminare nei dati utente del bot
        context.user_data['pending_delete'] = gone_ids
        
        elenco = "\n".join(f"- {u}" for u in gone_names)
        keyboard = [[InlineKeyboardButton("🗑️ Elimina tutti gli usciti", callback_data="confirm_delete")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await status_msg.edit_text(
            f"🔄 **Database Riorganizzato.**\n\n⚠️ **Utenti usciti/bannati ({len(gone_ids)}):**\n{elenco}",
            reply_markup=reply_markup
        )
    else:
        await status_msg.edit_text("🔄 **Database Riorganizzato.**\n✅ Tutti i membri sono presenti.")

# --- GESTIONE CALLBACK (BOTTONI) ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_delete":
        # Chiediamo conferma definitiva
        keyboard = [
            [InlineKeyboardButton("✅ SÌ, ELIMINA ORA", callback_data="do_delete")],
            [InlineKeyboardButton("❌ ANNULLA", callback_data="cancel_delete")]
        ]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "do_delete":
        ids_to_remove = context.user_data.get('pending_delete', [])
        if ids_to_remove:
            # Recuperiamo i nomi per il log finale prima di cancellare
            names = [u['username'] for u in users_col.find({"user_id": {"$in": ids_to_remove}})]
            
            # Eliminazione fisica
            users_col.delete_many({"user_id": {"$in": ids_to_remove}})
            # Opzionale: eliminiamo anche i loro messaggi storici
            messages_col.delete_many({"username": {"$in": names}})
            
            await query.edit_message_text(f"✅ Pulizia completata! Rimossi {len(ids_to_remove)} utenti dal database.")
            context.user_data['pending_delete'] = []
        else:
            await query.edit_message_text("⚠️ Errore: Nessun dato da eliminare.")

    elif query.data == "cancel_delete":
        await query.edit_message_text("❌ Operazione annullata. Gli utenti restano nel database.")

# --- ALTRI COMANDI (Semplificati per brevità, uguali a prima) ---
async def list_inactive(update, context):
    try:
        days = int(context.args[0]); limit_date = get_now() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(30)
        res = f"⚠️ Inattivi da {days}gg:\n" + "\n".join([f"- {u['username']}" for u in inactive])
        await update.message.reply_text(res)
    except: await update.message.reply_text("Uso: /list 5")

async def total_messages(update, context):
    pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}]
    results = list(messages_col.aggregate(pipeline))
    res = "📊 **Classifica Totale:**\n" + "\n".join([f"- {i['_id']}: {i['total']}" for i in results])
    await update.message.reply_text(res[:4000])

async def clean_user(update, context):
    name = " ".join(context.args)
    users_col.delete_one({"username": name})
    messages_col.delete_many({"username": name})
    await update.message.reply_text(f"✅ {name} rimosso.")

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("total", total_messages))
    app.add_handler(CommandHandler("clean", clean_user))
    
    # Handler per i bottoni
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("🚀 Bot avviato con sistema di conferma eliminazione...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
