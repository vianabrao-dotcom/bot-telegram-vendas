import os
import uuid
import logging
from datetime import datetime

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# =========================
# CONFIG / VARIÁVEIS ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

# Email real e fixo (recomendado) para evitar bloqueios do MP.
# Configure no Railway: MP_PAYER_EMAIL_PADRAO=seuemail@dominio.com
MP_PAYER_EMAIL_PADRAO = os.getenv("MP_PAYER_EMAIL_PADRAO", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não encontrado nas variáveis de ambiente.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não encontrado nas variáveis de ambiente.")

# =========================
# LOG
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# =========================
# Mercado Pago helpers
# =========================
MP_PAYMENTS_URL = "https://api.mercadopago.com/v1/payments"

def mp_headers():
    return {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

def gerar_pix(valor: float, descricao: str, payer_email: str, payer_first_name: str = "Cliente", payer_last_name: str = "VIP"):
    payload = {
        "transaction_amount": float(valor),
        "description": descricao,
        "payment_method_id": "pix",
        "payer": {
            "email": payer_email,
            "first_name": payer_first_name or "Cliente",
            "last_name": payer_last_name or "VIP",
        },
    }

    try:
        resp = requests.post(MP_PAYMENTS_URL, headers=mp_headers(), json=payload, timeout=30)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data
    except Exception as e:
        return 0, {"error": str(e)}

def consultar_pagamento(payment_id: int):
    url = f"{MP_PAYMENTS_URL}/{payment_id}"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}, timeout=30)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data
    except Exception as e:
        return 0, {"error": str(e)}

def extrair_pix(mp_response: dict):
    """
    Retorna: (payment_id, status, qr_code, ticket_url)
    """
    if not isinstance(mp_response, dict):
        return None, None, None, None

    payment_id = mp_response.get("id")
    status = mp_response.get("status")

    poi = mp_response.get("point_of_interaction") or {}
    tx = poi.get("transaction_data") or {}

    qr_code = tx.get("qr_code")
    ticket_url = tx.get("ticket_url")

    return payment_id, status, qr_code, ticket_url

# =========================
# MENU
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
# TELEGRAM handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (update.message.text or "").strip()

    # aceitar "Start" e "/start"
    if texto.lower() in ("/start", "start"):
        await update.message.reply_text(MENU, parse_mode="Markdown")
        return

    if texto not in PLANOS:
        await update.message.reply_text("❌ Opção inválida. Digite 1, 2 ou 3.\n\n" + MENU, parse_mode="Markdown")
        return

    nome_plano, valor = PLANOS[texto]
    user = update.effective_user

    # Email do pagador: use um real e fixo (configurável no Railway)
    payer_email = MP_PAYER_EMAIL_PADRAO or "pagamentos@seudominio.com"

    await update.message.reply_text("⏳ Gerando seu PIX...")

    descricao = f"{nome_plano} - Prime VIP"
    status_code, mp_resp = gerar_pix(
        valor,
        descricao,
        payer_email,
        payer_first_name=user.first_name or "Cliente",
        payer_last_name=user.last_name or "VIP",
    )

    # Se falhar de cara
    if status_code not in (200, 201):
        await update.message.reply_text(
            "❌ *Erro ao gerar Pix.* Tente novamente.\n\n"
            f"Status: `{status_code}`\n"
            f"Resposta: `{str(mp_resp)[:3500]}`",
            parse_mode="Markdown",
        )
        return

    payment_id, mp_status, qr_code, ticket_url = extrair_pix(mp_resp)

    # Se não veio QR, tenta consultar 1x pelo ID (muitas vezes vem no GET)
    if payment_id and (not qr_code):
        sc2, mp2 = consultar_pagamento(payment_id)
        if sc2 in (200, 201):
            payment_id, mp_status, qr_code, ticket_url = extrair_pix(mp2)

    # Ainda sem QR? manda fallback com ID + instrução
    if not qr_code:
        await update.message.reply_text(
            "⚠️ *Pagamento criado no Mercado Pago, mas o QR não retornou para o bot.*\n\n"
            f"🧾 ID do pagamento: `{payment_id}`\n"
            f"📌 Status: `{mp_status}`\n\n"
            "Abra o app do Mercado Pago → *Atividade* e localize essa venda para copiar o Pix.\n\n"
            f"(Resposta MP resumida: `{str(mp_resp)[:1200]}`)",
            parse_mode="Markdown",
        )
        return

    # Sucesso: envia copia e cola
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
