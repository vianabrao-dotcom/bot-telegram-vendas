import os
import uuid
import logging
import asyncio
from typing import Any, Dict, Optional, Tuple

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG / VARIÁVEIS ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_PAYER_EMAIL_PADRAO = os.getenv("MP_PAYER_EMAIL_PADRAO", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não configurado.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

# =========================
# LOG
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# =========================
# PLANOS
# =========================
PLANS_INITIAL = {
    "1": ("Plano Semanal", 19.90),
    "2": ("Plano Mensal", 29.90),
    "3": ("Plano Anual", 39.90),
    "4": ("Plano Anual Promocional", 29.99),
}

# =========================
# TEXTOS
# =========================
WELCOME_TEXT = (
    "🔥 Bem-vindo!\n\n"
    "Escolha abaixo o plano ideal para entrar no grupo privado:"
)

# =========================
# BOTÕES
# =========================
def keyboard_initial() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Plano Semanal – R$19,90", callback_data="buy:1")],
        [InlineKeyboardButton("Plano Mensal – R$29,90", callback_data="buy:2")],
        [InlineKeyboardButton("Plano Anual – R$39,90", callback_data="buy:3")],
        [InlineKeyboardButton("🎁 Plano Anual Promocional – R$29,99", callback_data="buy:4")],
    ])

# =========================
# MERCADO PAGO
# =========================
def gerar_pix(valor: float, descricao: str, payer_email: str) -> Dict[str, Any]:
    url = "https://api.mercadopago.com/v1/payments"

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": str(uuid.uuid4()),
    }

    payload = {
        "transaction_amount": float(valor),
        "description": descricao,
        "payment_method_id": "pix",
        "payer": {"email": payer_email},
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()

def extrair_pix(mp_response: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    poi = mp_response.get("point_of_interaction", {})
    tx = poi.get("transaction_data", {})
    return tx.get("qr_code"), tx.get("ticket_url")

def payer_email_for_user(user_id: int) -> str:
    if MP_PAYER_EMAIL_PADRAO:
        return MP_PAYER_EMAIL_PADRAO
    return f"braoviana+tg{user_id}@gmail.com"

# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(WELCOME_TEXT, reply_markup=keyboard_initial())

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("buy:"):
        await query.edit_message_text("Opção inválida. Use /start.")
        return

    key = data.split(":")[1]
    if key not in PLANS_INITIAL:
        await query.edit_message_text("Plano inválido. Use /start.")
        return

    if context.user_data.get("gerando_pix"):
        await query.message.reply_text("⏳ Já estou gerando seu PIX, aguarde...")
        return

    context.user_data["gerando_pix"] = True

    try:
        nome_plano, valor = PLANS_INITIAL[key]

        try:
            await query.edit_message_text(
                f"⏳ Gerando seu PIX...\n\n"
                f"Plano: {nome_plano}\n"
                f"Valor: R${valor:.2f}"
            )
        except Exception:
            pass

        email = payer_email_for_user(query.from_user.id)

        pagamento = await asyncio.to_thread(
            gerar_pix,
            valor,
            f"{nome_plano} - Prime VIP",
            email,
        )

        qr_code, ticket_url = extrair_pix(pagamento)

        if not qr_code:
            await query.message.reply_text(
                "❌ Não foi possível gerar o PIX agora.\n"
                "Tente novamente com /start."
            )
            return

        # 🔴 TEXTO SIMPLES (SEM MARKDOWN, SEM .TXT)
        msg = (
            "✅ PIX GERADO COM SUCESSO!\n\n"
            f"Plano: {nome_plano}\n"
            f"Valor: R${valor:.2f}\n\n"
            "📋 Copia e cola:\n"
            f"{qr_code}\n\n"
        )

        if ticket_url:
            msg += f"🔗 QR Code: {ticket_url}\n\n"

        msg += "⏳ Após pagar, aguarde a confirmação automática."

        await query.message.reply_text(msg)

    except Exception as e:
        logger.exception("Erro ao gerar PIX")
        await query.message.reply_text(
            "❌ Erro ao gerar o PIX.\n"
            "Tente novamente em alguns instantes com /start."
        )

    finally:
        context.user_data["gerando_pix"] = False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower().strip()
    if text in ("/start", "start", "menu", "oi", "olá", "ola"):
        await start(update, context)
        return

    await update.message.reply_text(
        "Para continuar, escolha um plano clicando nos botões abaixo 👇",
        reply_markup=keyboard_initial(),
    )

# =========================
# MAIN
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado com sucesso.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
