import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 Bem-vindo!\n\n"
        "Escolha seu plano digitando o número:\n\n"
        "1️⃣ Plano Semanal — R$19,90\n"
        "2️⃣ Plano Mensal — R$29,90\n"
        "3️⃣ Plano Anual — R$39,90\n\n"
        "🔥 OFERTA ESPECIAL 🔥\n"
        "4️⃣ Plano Anual — R$29,99"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏳ Em breve vou gerar seu Pix automaticamente.\n"
        "Aguarde a próxima etapa."
    )

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN não encontrado")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
