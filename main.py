import os
import uuid
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters


# =========================
# CONFIG / VARIÁVEIS ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

# Opcional (só se você quiser validar que o user está no grupo):
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()  # ex: -1003861201532

TZ = os.getenv("TZ", "America/Sao_Paulo").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não encontrado nas variáveis de ambiente.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não encontrado nas variáveis de ambiente.")


# =========================
# LOG
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# =========================
# FUNÇÃO: GERAR PIX (Mercado Pago)
# =========================
def gerar_pix(valor: float, descricao: str, payer_email: str, payer_first_name: str = "Cliente", payer_last_name: str = "VIP"):
    """
    Cria um pagamento PIX no Mercado Pago via API (requests).
    Retorna (status_code, response_json_ou_texto)
    """
    url = "https://api.mercadopago.com/v1/payments"

    # idempotency evita duplicar pix se repetir a requisição
    idempotency_key = str(uuid.uuid4())

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": idempotency_key,
    }

    payload = {
        "transaction_amount": float(valor),
        "description": descricao,
        "payment_method_id": "pix",
        "payer": {
            "email": payer_email,
            "first_name": payer_first_name,
            "last_name": payer_last_name,
        },
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
    except Exception as e:
        return 0, {"error": str(e)}


def extrair_pix_copia_cola(mp_response: dict):
    """
    Extrai o QR Copia e Cola e alguns campos úteis do retorno do MP.
    """
    poi = mp_response.get("point_of_interaction", {}) if isinstance(mp_response, dict) else {}
    tx = poi.get("transaction_data", {}) if isinstance(poi, dict) else {}

    qr_code = tx.get("qr_code")
    qr_code_base64 = tx.get("qr_code_base64")
    ticket_url = tx.get("ticket_url")

    return qr_code, qr_code_base64, ticket_url


# =========================
# MENSAGENS / MENU
# =========================
MENU = (
    "🔥 *BEM-VINDO AO PRIME VIP* 🔥\n\n"
    "Escolha um plano digitando o número:\n\n"
    "1️⃣ Plano Semanal – *R$10,90*\n"
    "2️⃣ Plano Mensal – *R$15,90*\n"
    "3️⃣ Plano Anual – *R$19,90*\n"
)

PLANOS = {
    "1": ("Plano Semanal", 10.90),
    "2": ("Plano Mensal", 15.90),
    "3": ("Plano Anual", 19.90),
}


# =========================
# HANDLERS TELEGRAM
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (update.message.text or "").strip()

    # Se mandar /start no chat (alguns clientes fazem isso como texto)
    if texto.lower() == "/start":
        await update.message.reply_text(MENU, parse_mode="Markdown")
        return

    if texto not in PLANOS:
        await update.message.reply_text("❌ Opção inválida. Digite 1, 2 ou 3.\n\n" + MENU, parse_mode="Markdown")
        return

    nome_plano, valor = PLANOS[texto]

    # Email "fake" para o payer (Mercado Pago exige email).
    # Você pode trocar por um email real se quiser.
    user = update.effective_user
    payer_email = f"user{user.id}@gmail.com"

    await update.message.reply_text("⏳ Gerando seu PIX...")

    descricao = f"{nome_plano} - Prime VIP"
    status, pagamento = gerar_pix(valor, descricao, payer_email, payer_first_name=user.first_name or "Cliente", payer_last_name=user.last_name or "VIP")

    if status not in (200, 201):
        await update.message.reply_text(
            "❌ *Erro ao gerar Pix.* Tente novamente.\n\n"
            f"Status: `{status}`\n"
            f"Resposta: `{str(pagamento)[:3500]}`",
            parse_mode="Markdown"
        )
        return

    # Extrair copia e cola
    qr_code, qr_base64, ticket_url = extrair_pix_copia_cola(pagamento)

    if not qr_code:
        await update.message.reply_text(
            "❌ Pix retornou formato inesperado.\n\n"
            f"Resposta: `{str(pagamento)[:3500]}`",
            parse_mode="Markdown"
        )
        return

    msg = (
        f"✅ *PIX GERADO COM SUCESSO!*\n\n"
        f"📦 Plano: *{nome_plano}*\n"
        f"💰 Valor: *R${valor:.2f}*\n\n"
        f"📋 *Copia e cola:*\n"
        f"`{qr_code}`\n\n"
    )

    if ticket_url:
        msg += f"🔗 Link do QR: {ticket_url}\n\n"

    msg += "⏳ Após pagar, aguarde a confirmação."

    await update.message.reply_text(msg, parse_mode="Markdown")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado. Rodando polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
