import os
import uuid
import json
import logging
import asyncio
from io import BytesIO

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG / VARIÁVEIS ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

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
# (valores iniciais + opção 4 promocional)
# =========================
MENU = (
    "🔥 BEM-VINDO AO PRIME VIP 🔥\n\n"
    "Escolha um plano digitando o número:\n\n"
    "1️⃣ Plano Semanal – R$19,90\n"
    "2️⃣ Plano Mensal – R$29,90\n"
    "3️⃣ Plano Anual – R$39,90\n\n"
    "4️⃣ 🎁 Plano Anual Promocional – R$29,99\n\n"
    "Digite apenas o número do plano desejado."
)

PLANOS = {
    "1": ("Plano Semanal", 19.90),
    "2": ("Plano Mensal", 29.90),
    "3": ("Plano Anual", 39.90),
    "4": ("Plano Anual Promocional", 29.99),
}

# =========================
# MERCADO PAGO: GERAR PIX
# =========================
def gerar_pix(valor: float, descricao: str, payer_email: str, payer_first_name: str = "Cliente", payer_last_name: str = "VIP"):
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
            "first_name": payer_first_name,
            "last_name": payer_last_name,
        },
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
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
    if not isinstance(mp_response, dict):
        return None, None, None

    poi = mp_response.get("point_of_interaction") or {}
    tx = poi.get("transaction_data") or {}

    qr_code = tx.get("qr_code")
    qr_code_base64 = tx.get("qr_code_base64")
    ticket_url = tx.get("ticket_url")

    return qr_code, qr_code_base64, ticket_url


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU)


def _is_start_text(texto: str) -> bool:
    t = (texto or "").strip().lower()
    return t in ("/start", "start", "iniciar", "menu")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (update.message.text or "").strip()

    # Se digitarem Start/Oi/etc, mostra menu e sai
    if _is_start_text(texto) or texto.lower() in ("oi", "ola", "olá"):
        await update.message.reply_text(MENU)
        return

    # Se não for uma opção válida
    if texto not in PLANOS:
        await update.message.reply_text("❌ Opção inválida. Digite 1, 2, 3 ou 4.\n\n" + MENU)
        return

    # trava por usuário para evitar vários PIX ao mesmo tempo
    if context.user_data.get("gerando_pix"):
        await update.message.reply_text("⏳ Já estou gerando um PIX pra você. Aguarde alguns segundos…")
        return

    context.user_data["gerando_pix"] = True
    try:
        nome_plano, valor = PLANOS[texto]
        user = update.effective_user

        # Email válido (não precisa existir). Usar +tg<ID> ajuda a diferenciar.
        # (mais seguro do que inventar domínio estranho)
        payer_email = f"braoviana+tg{user.id}@gmail.com"

        await update.message.reply_text("⏳ Gerando seu PIX...")

        descricao = f"{nome_plano} - Prime VIP"

        # roda requests fora do loop async (evita travar e sumir resposta)
        status, pagamento = await asyncio.to_thread(
            gerar_pix,
            valor,
            descricao,
            payer_email,
            user.first_name or "Cliente",
            user.last_name or "VIP",
        )

        if status not in (200, 201):
            await update.message.reply_text(
                "❌ Erro ao gerar Pix. Tente novamente.\n\n"
                f"Status: {status}\n"
                f"Resposta: {str(pagamento)[:3500]}"
            )
            return

        qr_code, qr_base64, ticket_url = extrair_pix_copia_cola(pagamento)

        if not qr_code:
            await update.message.reply_text(
                "❌ Pix retornou formato inesperado.\n\n"
                f"Resposta: {str(pagamento)[:3500]}"
            )
            return

        # Mensagem curta (sem Markdown pra não quebrar)
        msg = (
            "✅ PIX GERADO COM SUCESSO!\n\n"
            f"📦 Plano: {nome_plano}\n"
            f"💰 Valor: R${valor:.2f}\n\n"
            "📋 Copia e cola: (enviei também em arquivo .txt)\n"
        )
        if ticket_url:
            msg += f"\n🔗 Link do QR: {ticket_url}\n"

        msg += "\n⏳ Após pagar, aguarde a confirmação."

        await update.message.reply_text(msg)

        # Envia o copia-e-cola SEMPRE em TXT (cliente consegue copiar de boa)
        bio = BytesIO(qr_code.encode("utf-8"))
        bio.name = "pix_copia_e_cola.txt"
        await update.message.reply_document(document=bio, caption="📄 PIX Copia e Cola (arquivo)")

    finally:
        context.user_data["gerando_pix"] = False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Erro no bot:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.message:
            await update.message.reply_text("❌ Ocorreu um erro interno. Tente novamente em alguns segundos.")
    except Exception:
        pass


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot iniciado. Rodando polling...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
