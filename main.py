import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")

# ===============================
# FUNÇÃO PIX MERCADO PAGO
# ===============================
def gerar_pix(valor):
    url = "https://api.mercadopago.com/v1/payments"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": str(os.urandom(16).hex())
    }

    data = {
        "transaction_amount": float(valor),
        "description": "Assinatura VIP",
        "payment_method_id": "pix",
        "payer": {
            "email": "pagador_teste@gmail.com"
        }
    }

    resp = requests.post(url, headers=headers, json=data, timeout=20)
    try:
        return resp.status_code, resp.json()
    except:
        return resp.status_code, {"raw": resp.text}


# ===============================
# START
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 *BEM-VINDO AO PRIME VIP* 🔥\n\n"
        "Escolha um plano digitando o número:\n\n"
        "1️⃣ Plano Semanal – R$10,90\n"
        "2️⃣ Plano Mensal – R$15,90\n"
        "3️⃣ Plano Anual – R$19,90\n",
        parse_mode="Markdown"
    )

# ===============================
# MENSAGENS
# ===============================
async def handle_message(status, pagamento = gerar_pix(valor)

if status not in (200, 201):
    await update.message.reply_text(
        "❌ Erro ao gerar Pix.\n\n"
        f"Status: {status}\n"
        f"Resposta: {pagamento}"
    )
    return

try:
    tx = pagamento["point_of_interaction"]["transaction_data"]
    qr_copia_cola = tx["qr_code"]

    await update.message.reply_text(
        f"💳 PIX GERADO ✅\n"
        f"💰 Valor: R${valor}\n\n"
        f"📋 Copia e cola:\n{qr_copia_cola}\n\n"
        f"⏳ Após pagar, aguarde a liberação."
    )
except Exception as e:
    await update.message.reply_text(f"❌ Pix veio sem dados esperados: {pagamento}")

# ===============================
# APP
# ===============================
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.run_polling()
