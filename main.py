import os
import uuid
import json
import logging
import asyncio
import re
from typing import Tuple, Any

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters


# =========================
# CONFIG / VARIÁVEIS ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

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
# MENU / PLANOS
# =========================
MENU = (
    "🔥 BEM-VINDO AO PRIME VIP 🔥\n\n"
    "Escolha um plano digitando o número:\n\n"
    "1️⃣ Plano Semanal – R$10,90\n"
    "2️⃣ Plano Mensal – R$15,90\n"
    "3️⃣ Plano Anual – R$19,90\n"
)

PLANOS = {
    "1": ("Plano Semanal", 10.90),
    "2": ("Plano Mensal", 15.90),
    "3": ("Plano Anual", 19.90),
}


# =========================
# HELPERS
# =========================
def email_valido(email: str) -> bool:
    # Regex simples só pra garantir que tem formato ok
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def gerar_pix_sync(valor: float, descricao: str, payer_email: str, payer_first_name: str = "Cliente", payer_last_name: str = "VIP") -> Tuple[int, Any]:
    """
    Cria um pagamento PIX no Mercado Pago via API (requests).
    Retorna (status_code, response_json_ou_texto)
    """
    url = "https://api.mercadopago.com/v1/payments"
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
            "first_name": payer_first_name or "Cliente",
            "last_name": payer_last_name or "VIP",
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
    Extrai o QR Copia e Cola e ticket_url.
    """
    if not isinstance(mp_response, dict):
        return None, None

    poi = mp_response.get("point_of_interaction", {}) or {}
    tx = poi.get("transaction_data", {}) or {}

    qr_code = tx.get("qr_code")
    ticket_url = tx.get("ticket_url")

    return qr_code, ticket_url


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (update.message.text or "").strip()

    # aceitar "Start" e "/start"
    if texto.lower() in ("/start", "start"):
        await update.message.reply_text(MENU)
        return

    # trava: se já está gerando pix, não aceita outra opção
    if context.user_data.get("em_pagamento"):
        await update.message.reply_text("⏳ Já estou gerando um PIX para você. Aguarde finalizar antes de escolher outro plano.")
        return

    if texto not in PLANOS:
        await update.message.reply_text("❌ Opção inválida. Digite 1, 2 ou 3.\n\n" + MENU)
        return

    nome_plano, valor = PLANOS[texto]
    user = update.effective_user

    # Email de payer: precisa ser email real/valido (formato)
    payer_email = f"user{user.id}@gmail.com"

    # Garante formato válido
    if not email_valido(payer_email):
        payer_email = f"user{user.id}@example.com"

    context.user_data["em_pagamento"] = True
    try:
        await update.message.reply_text("⏳ Gerando seu PIX...")

        descricao = f"{nome_plano} - Prime VIP"

        # roda a chamada do MercadoPago fora do event loop (não trava o bot)
        status, pagamento = await asyncio.to_thread(
            gerar_pix_sync,
            valor,
            descricao,
            payer_email,
            user.first_name or "Cliente",
            user.last_name or "VIP",
        )

        # se falhar, NÃO use Markdown (pra não quebrar)
        if status not in (200, 201):
            resumo = str(pagamento)
            if len(resumo) > 1200:
                resumo = resumo[:1200] + "..."

            await update.message.reply_text(
                "❌ Erro ao gerar Pix. Tente novamente.\n\n"
                f"Status: {status}\n"
                f"Resposta: {resumo}"
            )
            return

        qr_code, ticket_url = extrair_pix_copia_cola(pagamento)

        if not qr_code:
            resumo = str(pagamento)
            if len(resumo) > 1200:
                resumo = resumo[:1200] + "..."
            await update.message.reply_text(
                "❌ Pix retornou formato inesperado.\n\n"
                f"Resposta: {resumo}"
            )
            return

        msg = (
            "✅ PIX GERADO COM SUCESSO!\n\n"
            f"📦 Plano: {nome_plano}\n"
            f"💰 Valor: R${valor:.2f}\n\n"
            "📋 Copia e cola:\n"
            f"{qr_code}\n\n"
        )
        if ticket_url:
            msg += f"🔗 Link do QR: {ticket_url}\n\n"
        msg += "⏳ Após pagar, aguarde a confirmação."

        await update.message.reply_text(msg)

    finally:
        context.user_data["em_pagamento"] = False


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado. Rodando polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
