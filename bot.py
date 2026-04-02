# --- COMANDI ADMIN CON GESTIONE ERRORI ---

async def count_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        # Aspetta: /count 7 @username
        if len(context.args) < 2:
            raise ValueError
        
        days = int(context.args[0])
        target = context.args[1]
        
        limit_date = get_now() - timedelta(days=min(days, 90))
        count = messages_col.count_documents({
            "username": target, 
            "timestamp": {"$gte": limit_date}
        })
        
        await update.message.reply_text(f"📊 {target}: **{count}** messaggi negli ultimi {days}gg.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Errore sintassi!\nUso corretto: `/count 7 @username`", parse_mode="Markdown")

async def list_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        # Aspetta: /list 5
        if not context.args:
            raise ValueError
            
        days = int(context.args[0])
        limit_date = get_now() - timedelta(days=days)
        
        inactive = users_col.find({"last_seen": {"$lt": limit_date}}).limit(30)
        lines = [f"- {u['username']} ({u['last_seen'].strftime('%d/%m')})" for u in inactive]
        
        if lines:
            res = f"⚠️ **Inattivi da {days}gg:**\n" + "\n".join(lines)
        else:
            res = "✅ Tutti gli utenti sono stati attivi in questo periodo."
            
        await update.message.reply_text(res, parse_mode="Markdown")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Errore sintassi!\nUso corretto: `/list 5` (dove 5 sono i giorni)", parse_mode="Markdown")

async def get_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        # Aspetta: /user @username
        if not context.args:
            raise ValueError
            
        target = context.args[0]
        u = users_col.find_one({"username": target})
        
        if u:
            await update.message.reply_text(
                f"👤 **{u['username']}**\n"
                f"📅 Ultimo avvistamento: {u['last_seen'].strftime('%d/%m %H:%M')}\n"
                f"💬 Ultimo testo: `{u['last_text']}`", 
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ Utente {target} non trovato nel database.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Errore sintassi!\nUso corretto: `/user @username`", parse_mode="Markdown")

async def clean_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ADMIN: return
    try:
        # Aspetta: /clean @username
        if not context.args:
            raise ValueError
            
        target = " ".join(context.args) # Gestisce anche nomi con spazi se non hanno @
        
        r1 = users_col.delete_one({"username": target})
        r2 = messages_col.delete_many({"username": target})
        
        if r1.deleted_count > 0 or r2.deleted_count > 0:
            await update.message.reply_text(f"🗑️ Dati di {target} eliminati.\n- Record profilo: {r1.deleted_count}\n- Messaggi: {r2.deleted_count}")
        else:
            await update.message.reply_text(f"⚠️ Nessun dato trovato per {target}.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Errore sintassi!\nUso corretto: `/clean @username`", parse_mode="Markdown")
