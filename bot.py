import os
import threading
import asyncio
from flask import Flask
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError, RetryAfter
from pymongo import MongoClient

# --- SERVER WEB (ALLEGGERITO) ---
webapp = Flask(__name__)

@webapp.route('/')
def home(): 
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    webapp.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

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

# --- GESTORE ERRORI GLOBALE ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"⚠️ Errore critico: {context.error}")
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("❌ Si è verificato un errore imprevisto. Il bot è ancora attivo.")

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

# --- COMANDO REFRESH (SICURO) ---
async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    
    users_col.delete_many({"$or": [{"username": {"$exists": False}}, {"user_id": {"$exists": False}}, {"username": None}]})
    status_msg = await update.message.reply_text("🔄 Sincronizzazione in corso (potrebbe richiedere tempo)...")
    
    all_users = list(users_col.find())
    gone_ids, gone_names = [], []

    for user in all_users:
        try:
            member = await context.bot.get_chat_member(GROUP_MONITOR, user['user_id'])
            if member.status in ['left', 'kicked']:
                gone_ids.append(user['user_id'])
                gone_names.append(user['username'])
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after) # Rispetta il blocco di Telegram
            continue
        except (BadRequest, TelegramError):
            gone_ids.append(user['user_id'])
            gone_names.append(user['username'])
        
        await asyncio.sleep(0.1) # Pausa per non bloccare l'event loop

    if gone_ids:
        context.user_data['pending_delete'] = gone_ids
        elenco = "\n".join(f"- {u}" for u in gone_names[:20]) # Mostra solo i primi 20 per non eccedere limiti testo
        keyboard = [[InlineKeyboardButton("🗑️ Elimina tutti gli usciti", callback_data="confirm_delete")]]
        await status_msg.edit_text(
            f"🔄 **Scan completato.**\n\n⚠️ **Utenti rilevati come usciti ({len(gone_ids)}):**\n{elenco}{'...' if len(gone_ids) > 20 else ''}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await status_msg.edit_text("✅ Sincronizzazione completata. Nessun utente rimosso.")

# --- GESTIONE CALLBACK ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_delete":
        keyboard = [[InlineKeyboardButton("✅ SÌ, ELIMINA ORA", callback_data="do_delete")],
                    [InlineKeyboardButton("❌ ANNULLA", callback_data="cancel_delete")]]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "do_delete":
        ids_to_remove = context.user_data.get('pending_delete', [])
        if ids_to_remove:
            names = [u['username'] for u in users_col.find({"user_id": {"$in": ids_to_remove}})]
            users_col.delete_many({"user_id": {"$in": ids_to_remove}})
            messages_col.delete_many({"username": {"$in": names}})
            await query.edit_message_text(f"✅ Pulizia completata! Rimossi {len(ids_to_remove)} utenti.")
            context.user_data['pending_delete'] = []
        else:
            await query.edit_message_text("⚠️ Errore: Dati scaduti o non trovati.")

    elif query.data == "cancel_delete":
        await query.edit_message_text("❌ Operazione annullata.")

# --- ALTRI COMANDI (CON PROTEZIONE ERRORI) ---
async def list_inactive(update, context):
    try:
        if not context.args:
            return await update.message.reply_text("❌ Uso: `/list 5` (indica i giorni)", parse_mode="Markdown")
        
        days = int(context.args[0])
        limit_date = get_now() - timedelta(days=days)
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(30)
        
        user_list = [f"- {u['username']}" for u in inactive]
        if not user_list:
            return await update.message.reply_text(f"✅ Nessun inattivo da oltre {days} giorni.")
            
        res = f"⚠️ Inattivi da {days}gg:\n" + "\n".join(user_list)
        await update.message.reply_text(res)
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Errore: Inserisci un numero valido di giorni (es: `/list 7`)", parse_mode="Markdown")

async def total_messages(update, context):
    try:
        pipeline = [{"$group": {"_id": "$username", "total": {"$sum": 1}}}, {"$sort": {"total": -1}}, {"$limit": 50}]
        results = list(messages_col.aggregate(pipeline))
        if not results:
            return await update.message.reply_text("📭 Nessun messaggio registrato finora.")
            
        res = "📊 **Classifica Messaggi:**\n" + "\n".join([f"- {i['_id']}: {i['total']}" for i in results])
        await update.message.reply_text(res[:4000])
    except Exception as e:
        print(f"Errore aggregate: {e}")

async def clean_user(update, context):
    try:
        if not context.args:
            return await update.message.reply_text("❌ Uso: `/clean @Username`", parse_mode="Markdown")
        name = " ".join(context.args)
        users_col.delete_one({"username": name})
        messages_col.delete_many({"username": name})
        await update.message.reply_text(f"✅ {name} rimosso con successo.")
    except Exception as e:
        await update.message.reply_text(f"❌ Impossibile pulire l'utente: {e}")

# --- MAIN ---
def main():
    # 1. Avvia Flask immediatamente
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("🌐 Server Web attivo.")

    # 2. Configura il Bot
    app = Application.builder().token(TOKEN).build()
    
    # Gestore errori globale
    app.add_error_handler(error_handler)
    
    # Handlers
    app.add_handler(MessageHandler(filters.Chat(GROUP_MONITOR) & ~filters.COMMAND, track_activity))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CommandHandler("list", list_inactive))
    app.add_handler(CommandHandler("total", total_messages))
    app.add_handler(CommandHandler("clean", clean_user))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("🚀 Bot avviato...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
