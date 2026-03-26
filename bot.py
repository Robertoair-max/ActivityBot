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

messages_col = db.messages
# Crea un indice che cancella i documenti dopo 90 giorni (90 * 24 * 3600 secondi)
messages_col.create_index("timestamp", expireAfterSeconds=7776000)

# Funzione per ottenere l'orario corretto (Italia UTC+1)
def get_now():
    return datetime.utcnow() + timedelta(hours=1)

async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id == GROUP_MONITOR:
        user = update.effective_user
        if not user or user.is_bot: return
        
        now = get_now()
        username = f"@{user.username}" if user.username else user.full_name
        
        # Mantieni l'aggiornamento dell'ultimo accesso (per /list e /user)
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {
                "username": username,
                "last_seen": now,
                "last_text": update.message.text[:100] if update.message.text else "No text"
            }}, upsert=True
        )
        
        # NUOVO: Salva il singolo messaggio per il conteggio storico
        messages_col.insert_one({
            "username": username,
            "timestamp": now
        })

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        # Uso: /count 7 @username
        days = int(context.args[0])
        target_username = context.args[1]
        
        # Cap a 90 giorni per coerenza con il database
        search_days = min(days, 90)
        limit_date = get_now() - timedelta(days=search_days)
        
        count = messages_col.count_documents({
            "username": target_username,
            "timestamp": {"$gte": limit_date}
        })
        
        await update.message.reply_text(
            f"📊 {target_username} ha inviato **{count}** messaggi negli ultimi {search_days} giorni."
        )
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Uso: `/count 7 @username` (max 90gg)")

async def test_last_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    
    # Prende l'ultimo messaggio salvato nel DB
    last_msg = messages_col.find_one(sort=[("timestamp", -1)])
    
    if last_msg:
        now = get_now()
        last_time = last_msg['timestamp']
        diff = now - last_time
        
        # Calcola se sono passate meno di 2 ore (7200 secondi)
        is_online = diff.total_seconds() < 7200
        status_icon = "✅" if is_online else "⚠️"
        status_text = "Bot Online" if is_online else "Bot Offline (Nessun msg da > 2h)"
        
        ora_f = last_time.strftime('%H:%M:%S')
        
        await update.message.reply_text(
            f"🔍 **Stato Monitoraggio:** {status_text} {status_icon}\n\n"
            f"👤 Ultimo utente: {last_msg['username']}\n"
            f"⏰ Ora ultimo msg: {ora_f}\n"
            f"⏳ Ritardo: {int(diff.total_seconds() // 60)} minuti fa"
        )
    else:
        await update.message.reply_text("❌ Nessun messaggio trovato nel database.")

async def total_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    
    # Aggregazione per contare TUTTI i messaggi per ogni utente
    pipeline = [
        {"$group": {"_id": "$username", "total": {"$sum": 1}}},
        {"$sort": {"total": -1}}  # Ordina dai più attivi ai meno attivi
    ]
    
    results = list(messages_col.aggregate(pipeline))
    
    if not results:
        await update.message.reply_text("Nessun dato disponibile.")
        return

    res = "📊 **Classifica Totale Messaggi (Tutti gli utenti):**\n"
    
    for item in results:
        riga = f"- {item['_id']}: {item['total']}\n"
        
        # Se il messaggio sta diventando troppo lungo per Telegram, invialo e ricomincia
        if len(res) + len(riga) > 4000:
            await update.message.reply_text(res)
            res = ""
        res += riga
    
    await update.message.reply_text(res)


async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        days = int(context.args[0])
        limit_date = get_now() - timedelta(days=days) # CORRETTO
        
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(30)
        
        testo_giorni = "OGGI" if days == 0 else f"{days}gg"
        res = f"⚠️ Inattivi da: {testo_giorni}\n"
        count = 0
        for u in inactive: 
            res += f"- {u['username']} ({u['last_seen'].strftime('%d/%m %H:%M')})\n"
            count += 1
            
        await update.message.reply_text(res if count > 0 else "✅ Tutti attivi!")
    except: await update.message.reply_text("Uso: /list 5")

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    if not context.args:
        await update.message.reply_text("Uso: /user @username")
        return

    target_user = context.args[0]
    user_data = users_col.find_one({"username": target_user})
    
    if user_data:
        data_f = user_data['last_seen'].strftime('%d/%m/%Y %H:%M')
        await update.message.reply_text(f"👤 {user_data['username']}\n📅 Ultima attività: {data_f}\n💬 Msg: {user_data['last_text']}")
    else:
        await update.message.reply_text(f"❌ L'utente {target_user} non è presente nel database o non è mai stato attivo dalla data di attivazione del bot.")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    if not context.args:
        await update.message.reply_text("❌ Uso: `/clean @username`")
        return
        
    username = context.args[0]
    
    # Elimina l'anagrafica utente
    res_user = users_col.delete_one({"username": username})
    
    # Elimina tutti i messaggi registrati per quel username
    res_msgs = messages_col.delete_many({"username": username})
    
    if res_user.deleted_count > 0 or res_msgs.deleted_count > 0:
        await update.message.reply_text(
            f"✅ Dati di **{username}** eliminati con successo!\n"
            f"- Record utente: {res_user.deleted_count}\n"
            f"- Messaggi rimossi: {res_msgs.deleted_count}"
        )
    else:
        await update.message.reply_text(f"⚠️ Nessun dato trovato per {username}.")

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
    
    print("🚀 Bot avviato e in ascolto...")
    app.run_polling(drop_pending_updates=True, close_loop=True)

if __name__ == "__main__":
    main()
