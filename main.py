import os
import uuid
import json
import logging
import asyncio
from typing import Any, Dict, Optional, Tuple, Union

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

# (Opcional) Se quiser fixar um email real e estável:
# Railway -> Variables: MP_PAYER_EMAIL_PADRAO=seuemail@...
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
# PLANOS (INICIAL + PROMO)
# =========================
PLANS_INITIAL = {
    "1": ("Plano Semanal", 19.90),
    "2": ("Plano Mensal", 29.90),
    "3": ("Plano Anual", 39.90),
    "4": ("Plano Anual Promocional", 29.99),
}

# =========================
# PLANOS (RENOVAÇÃO - 24H)
# =========================
PLANS_RENEWAL = {
    "1": ("Plano Semanal (Renovação)", 10.90),
    "2": ("Plano Mensal (Renovação)", 15.90),
    "3": ("Plano Anual (Renovação)", 19.90),
}

# =========================
# TEXTOS
# =========================
WELCOME_TEXT = (
    "🔥 Bem-vindo! Você acaba de garantir acesso ao conteúdo mais exclusivo e atualizado do momento!\n"
    "Centenas de pessoas já estão dentro aproveitando todos os benefícios. Agora é a sua vez!\n\n"
    "Escolha abaixo o plano ideal e entre imediatamente no grupo privado:"
)

RENEW_TEXT = (
    "🎁 MENU EXCLUSIVO DE RENOVAÇÃO (válido por 24 horas)\n\n"
    "🔥 Oferta liberada por 24 horas:\n"
    "Escolha abaixo o plano de renovação com desconto:"
)

# =========================
# UI: BOTÕES
# =========================
def keyboard_initial() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("Plano Semanal – R$19,90", callback_data="buy:initial:1")],
        [InlineKeyboardButton("Plano Mensal – R$29,90", callback_data="buy:initial:2")],
        [InlineKeyboardButton("Plano Anual – R$39,90", callback_data="buy:initial:3")],
        [InlineKeyboardButton("🎁 Plano Anual Promocional – R$29,99", callback_data="buy:initial:4")],
    ]
    return InlineKeyboardMarkup(kb)

def keyboard_renewal() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("Plano Semanal – R$10,90", callback_data="buy:renew:1")],
        [InlineKeyboardButton("Plano Mensal – R$15,90", callback_data="buy:renew:2")],
        [InlineKeyboardButton("Plano Anual – R$19,90", callback_data="buy:renew:3")],
        [InlineKeyboardButton("⬅️ Voltar ao menu", callback_data="nav:initial")],
    ]
    return InlineKeyboardMarkup(kb)

# =========================
# MERCADO PAGO: GERAR PIX
# =========================
def gerar_pix(valor: float, descricao: str, payer_email: str) -> Tuple[int, Union[Dict[str, Any], str]]:
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
        "payer": {"email": payer_email},
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
    except Exception as e:
        return 0, {"error": str(e)}

def extrair_pix_copia_cola(mp_response: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(mp_response, dict):
        return None, None

    poi = mp_response.get("point_of_interaction") or {}
    tx = poi.get("transaction_data") or {}

    qr_code = tx.get("qr_code")
    ticket_url = tx.get("ticket_url")
    return qr_code, ticket_url

def payer_email_for_user(user_id: int) -> str:
    # Melhor forma “sem dor de cabeça”: use um email real via ENV.
    if MP_PAYER_EMAIL_PADRAO:
        return MP_PAYER_EMAIL_PADRAO
    # Fallback: alias no gmail (formato válido). Troque "braoviana" se quiser.
    return f"braoviana+tg{user_id}@gmail.com"

# =========================
# HELPERS
# =========================
def is_start_like(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("/start", "start", "menu", "iniciar", "começar", "comecar")

# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(WELCOME_TEXT, reply_markup=keyboard_initial())

async def renovar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Você pode manter esse comando para testes.
    # No seu fluxo final, ele só deve aparecer quando faltar 24h (seu sweeper faz isso).
    context.user_data.clear()
    await update.message.reply_text(RENEW_TEXT, reply_markup=keyboard_renewal())

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    # Navegação simples
    if data == "nav:initial":
        context.user_data.clear()
        await query.edit_message_text(WELCOME_TEXT, reply_markup=keyboard_initial())
        return

    # Compra: buy:<initial|renew>:<key>
    if not data.startswith("buy:"):
        await query.edit_message_text("❌ Ação inválida. Use /start novamente.")
        return

    parts = data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("❌ Ação inválida. Use /start novamente.")
        return

    _, mode, key = parts

    plans = PLANS_INITIAL if mode == "initial" else PLANS_RENEWAL
    if key not in plans:
        await query.edit_message_text("❌ Opção inválida. Use /start novamente.")
        return

    # trava por usuário para evitar vários pix simultâneos
    if context.user_data.get("gerando_pix"):
        await query.message.reply_text("⏳ Já estou gerando um PIX pra você. Aguarde alguns segundos…")
        return

    context.user_data["gerando_pix"] = True
    try:
        nome_plano, valor = plans[key]
        user = query.from_user

        # Atualiza mensagem para feedback instantâneo
        try:
            await query.edit_message_text(
                f"⏳ Gerando seu PIX...\n\nPlano: {nome_plano}\nValor: R${valor:.2f}"
            )
        except Exception:
            # se não der pra editar (ex.: mensagem antiga), só segue
            pass

        email = payer_email_for_user(user.id)

        status, pagamento = await asyncio.to_thread(
            gerar_pix,
            float(valor),
            f"{nome_plano} - Prime VIP",
            email,
        )

        if status not in (200, 201) or not isinstance(pagamento, dict):
            await query.message.reply_text(
                "❌ Erro ao gerar Pix. Tente novamente.\n\n"
                f"Status: {status}\n"
                f"Resposta: {str(pagamento)[:2500]}"
            )
            return

        qr_code, ticket_url = extrair_pix_copia_cola(pagamento)

        if not qr_code:
            await query.message.reply_text(
                "❌ O Mercado Pago não retornou o código Pix (copia e cola).\n\n"
                f"Resposta: {str(pagamento)[:2500]}"
            )
            return

        # Mensagem SEM Markdown para facilitar copiar
        msg = (
            "✅ PIX GERADO COM SUCESSO!\n\n"
            f"Plano: {nome_plano}\n"
            f"Valor: R${valor:.2f}\n\n"
            "📋 Copia e cola:\n"
            f"{qr_code}\n\n"
        )
        if ticket_url:
            msg += f"🔗 QR Code: {ticket_url}\n\n"
        msg += "⏳ Após pagar, aguarde a confirmação."

        await query.message.reply_text(msg)

    finally:
        context.user_data["gerando_pix"] = False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # Qualquer "start/menu/oi" reseta e mostra o menu correto (não gera pix)
    if is_start_like(text) or text.lower() in ("oi", "ola", "olá"):
        context.user_data.clear()
        await update.message.reply_text(WELCOME_TEXT, reply_markup=keyboard_initial())
        return

    # Se a pessoa digitar números mesmo assim, a gente ajuda (fallback)
    if text in PLANS_INITIAL:
        # simula clique no botão inicial
        fake_update = update
        await update.message.reply_text("👆 Para facilitar, escolha clicando em um botão abaixo:", reply_markup=keyboard_initial())
        return

    if text in PLANS_RENEWAL:
        await update.message.reply_text("👆 Para facilitar, escolha clicando em um botão abaixo:", reply_markup=keyboard_renewal())
        return

    # Caso geral
    await update.message.reply_text("Para escolher, clique em um dos botões abaixo 👇", reply_markup=keyboard_initial())

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Erro no bot:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Ocorreu um erro interno. Tente novamente em alguns segundos.",
            )
    except Exception:
        pass

# =========================
# MAIN
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("renovar", renovar))  # opcional (teste/manual)
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    logger.info("Bot iniciado. Rodando polling...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
