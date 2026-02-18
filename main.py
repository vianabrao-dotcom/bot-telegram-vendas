import os
import uuid
import logging
import asyncio
from io import BytesIO
from typing import Any, Dict, Optional, Tuple, Union

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

# Email padrão (mais seguro e menos problemático do que pedir CPF)
# Se estiver vazio, cai no formato: seuemail+tg<ID>@gmail.com
MP_PAYER_EMAIL_PADRAO = os.getenv("MP_PAYER_EMAIL_PADRAO", "").strip()

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
# MENUS / PLANOS
# =========================

# MENU INICIAL (valores “originais” + opção 4 promocional separado)
MENU = (
    "🔥 Bem-vindo! Você acaba de garantir acesso ao conteúdo mais exclusivo e atualizado do momento!\n"
    "Centenas de pessoas já estão dentro aproveitando todos os benefícios. Agora é a sua vez!\n\n"
    "Escolha abaixo o plano ideal e entre imediatamente no grupo privado:\n\n"
    "1️⃣ Plano Semanal — R$19,90\n"
    "2️⃣ Plano Mensal — R$29,90\n"
    "3️⃣ Plano Anual — R$39,90\n\n"
    "4️⃣ 🎁 Plano Anual Promocional — R$29,99\n\n"
    "Digite apenas o número do plano desejado."
)

# MENU DE RENOVAÇÃO (só aparece quando você disparar manualmente ou via sua lógica de 24h)
MENU_RENOVACAO = (
    "🎁 MENU EXCLUSIVO DE RENOVAÇÃO (válido por 24 horas)\n\n"
    "🔥 Oferta liberada por 24 horas:\n\n"
    "1️⃣ Plano Semanal — R$10,90\n"
    "2️⃣ Plano Mensal — R$15,90\n"
    "3️⃣ Plano Anual — R$19,90\n\n"
    "Esses valores expiram em 24 horas."
)

PLANOS_INICIAIS = {
    "1": ("Plano Semanal", 19.90),
    "2": ("Plano Mensal", 29.90),
    "3": ("Plano Anual", 39.90),
    "4": ("Plano Anual Promocional", 29.99),
}

PLANOS_RENOVACAO = {
    "1": ("Plano Semanal (Renovação)", 10.90),
    "2": ("Plano Mensal (Renovação)", 15.90),
    "3": ("Plano Anual (Renovação)", 19.90),
}

# Por padrão, o bot usa o MENU INICIAL.
# Você pode trocar para renovação guardando uma flag em user_data quando sua lógica de 24h disparar.
USER_MODE_KEY = "menu_mode"  # "initial" | "renew"


# =========================
# MERCADO PAGO: GERAR PIX
# =========================
def gerar_pix(valor: float, descricao: str, payer_email: str) -> Tuple[int, Union[Dict[str, Any], str]]:
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
        },
    }

    # Timeout curto pra não “prender” o bot.
    # Mesmo assim, se falhar, a gente mostra o erro.
    try:
        logger.info(f"[MP] Criando PIX: valor={valor} descricao='{descricao}' email='{payer_email}'")
        resp = requests.post(url, headers=headers, json=payload, timeout=20)

        try:
            data = resp.json()
        except Exception:
            data = resp.text

        logger.info(f"[MP] Resposta status={resp.status_code}")
        return resp.status_code, data

    except Exception as e:
        logger.exception("[MP] Erro criando PIX")
        return 0, {"error": str(e)}


def extrair_pix(mp_response: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """
    Extrai:
    - qr_code (copia e cola)
    - qr_code_base64
    - ticket_url
    - payment_id
    """
    poi = (mp_response.get("point_of_interaction") or {})
    tx = (poi.get("transaction_data") or {})

    qr_code = tx.get("qr_code")
    qr_code_base64 = tx.get("qr_code_base64")
    ticket_url = tx.get("ticket_url")
    payment_id = mp_response.get("id")

    return qr_code, qr_code_base64, ticket_url, payment_id


# =========================
# HELPERS TELEGRAM
# =========================
def _is_start_text(texto: str) -> bool:
    t = (texto or "").strip().lower()
    return t in ("/start", "start", "iniciar", "menu")


def _get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get(USER_MODE_KEY, "initial")


def _get_plans_by_mode(mode: str):
    return PLANOS_RENOVACAO if mode == "renew" else PLANOS_INICIAIS


def _get_menu_by_mode(mode: str) -> str:
    return MENU_RENOVACAO if mode == "renew" else MENU


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sempre inicia no menu inicial
    context.user_data[USER_MODE_KEY] = "initial"
    await update.message.reply_text(MENU)


async def renovar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando manual pra você testar o menu de renovação.
    Depois você liga isso automaticamente quando faltar 24h.
    """
    context.user_data[USER_MODE_KEY] = "renew"
    await update.message.reply_text(MENU_RENOVACAO)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = _get_mode(context)
    await update.message.reply_text(_get_menu_by_mode(mode))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (update.message.text or "").strip()

    # Se digitarem Start/Oi/etc, mostra menu
    if _is_start_text(texto) or texto.lower() in ("oi", "ola", "olá"):
        await menu(update, context)
        return

    # trava por usuário para evitar vários PIX simultâneos
    if context.user_data.get("gerando_pix"):
        await update.message.reply_text("⏳ Já estou gerando um PIX pra você. Aguarde alguns segundos…")
        return

    mode = _get_mode(context)
    planos = _get_plans_by_mode(mode)
    menu_txt = _get_menu_by_mode(mode)

    # opção inválida
    if texto not in planos:
        if mode == "renew":
            await update.message.reply_text("❌ Opção inválida. Digite 1, 2 ou 3.\n\n" + menu_txt)
        else:
            await update.message.reply_text("❌ Opção inválida. Digite 1, 2, 3 ou 4.\n\n" + menu_txt)
        return

    context.user_data["gerando_pix"] = True
    try:
        nome_plano, valor = planos[texto]
        user = update.effective_user

        # Email padrão seguro:
        # 1) se tiver MP_PAYER_EMAIL_PADRAO, usa ele
        # 2) senão, usa alias +tg<ID> no seu gmail
        if MP_PAYER_EMAIL_PADRAO:
            payer_email = MP_PAYER_EMAIL_PADRAO
        else:
            # Troque "braoviana" aqui se quiser — mas o ideal é usar MP_PAYER_EMAIL_PADRAO nas variáveis.
            payer_email = f"braoviana+tg{user.id}@gmail.com"

        await update.message.reply_text("⏳ Gerando seu PIX...")

        descricao = f"{nome_plano} - Prime VIP"

        # roda requests fora do loop async (evita travar o bot)
        status, pagamento = await asyncio.to_thread(
            gerar_pix,
            valor,
            descricao,
            payer_email,
        )

        if status not in (200, 201):
            await update.message.reply_text(
                "❌ Erro ao gerar Pix. Tente novamente.\n\n"
                f"Status: {status}\n"
                f"Resposta: {str(pagamento)[:3000]}"
            )
            return

        if not isinstance(pagamento, dict):
            await update.message.reply_text(
                "❌ Resposta inesperada do Mercado Pago (não veio JSON).\n\n"
                f"Resposta: {str(pagamento)[:3000]}"
            )
            return

        qr_code, qr_base64, ticket_url, payment_id = extrair_pix(pagamento)

        if not qr_code:
            await update.message.reply_text(
                "❌ O Mercado Pago não retornou o 'qr_code' (copia e cola).\n\n"
                f"Resposta: {str(pagamento)[:3000]}"
            )
            return

        # 1) Envia um resumo no chat
        msg = (
            "✅ PIX GERADO COM SUCESSO!\n\n"
            f"📦 Plano: {nome_plano}\n"
            f"💰 Valor: R${valor:.2f}\n"
        )
        if payment_id:
            msg += f"🧾 ID do pagamento: {payment_id}\n"
        if ticket_url:
            msg += f"\n🔗 Link do QR: {ticket_url}\n"

        msg += (
            "\n📄 Copia e cola: enviei também em arquivo .txt (mais fácil copiar no celular).\n"
            "⏳ Após pagar, aguarde a confirmação."
        )

        await update.message.reply_text(msg)

        # 2) Envia SEMPRE o copia-e-cola em TXT (pra não depender do copiar do Telegram)
        try:
            bio = BytesIO(qr_code.encode("utf-8"))
            bio.name = "pix_copia_e_cola.txt"
            await update.message.reply_document(document=bio, caption="📄 PIX Copia e Cola (arquivo)")
        except Exception:
            # Se falhar o documento, manda o copia-e-cola no texto mesmo (fallback)
            logger.exception("[TG] Falha ao enviar arquivo .txt. Enviando fallback em texto.")
            await update.message.reply_text(
                "📄 PIX Copia e Cola (fallback):\n\n" + qr_code
            )

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
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("renovar", renovar))  # teste manual do menu de renovação

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    logger.info("Bot iniciado. Rodando polling...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
