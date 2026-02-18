import os
import uuid
import logging
import asyncio
from datetime import datetime, timedelta, timezone
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
# CONFIG / ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

# (Opcional) Email padrão estável (recomendado)
MP_PAYER_EMAIL_PADRAO = os.getenv("MP_PAYER_EMAIL_PADRAO", "").strip()

# Admin (seu ID do Telegram) para comandos de teste
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "").strip()

# Link do grupo (pós-aprovação) - pode ser convite permanente ou seu fluxo atual
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não encontrado nas variáveis de ambiente.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não encontrado nas variáveis de ambiente.")

# =========================
# LOG
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_TELEGRAM_ID) and str(user_id) == str(ADMIN_TELEGRAM_ID)


# =========================
# PLANOS (INICIAL + PROMO)
# =========================
PLANS_INITIAL = {
    "1": {"name": "Plano Semanal", "amount": 19.90, "days": 7},
    "2": {"name": "Plano Mensal", "amount": 29.90, "days": 30},
    "3": {"name": "Plano Anual", "amount": 39.90, "days": 365},
    "4": {"name": "Plano Anual Promocional", "amount": 29.99, "days": 365},
}

# =========================
# PLANOS (RENOVAÇÃO - 24H) (deixei pronto, mas só exibimos quando você ligar sua regra)
# =========================
PLANS_RENEWAL = {
    "6": {"name": "Plano Semanal (Renovação)", "amount": 10.90, "days": 7},
    "7": {"name": "Plano Mensal (Renovação)", "amount": 15.90, "days": 30},
    "8": {"name": "Plano Anual (Renovação)", "amount": 19.90, "days": 365},
}

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
        [InlineKeyboardButton("Plano Semanal – R$10,90", callback_data="buy:renew:6")],
        [InlineKeyboardButton("Plano Mensal – R$15,90", callback_data="buy:renew:7")],
        [InlineKeyboardButton("Plano Anual – R$19,90", callback_data="buy:renew:8")],
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
    return tx.get("qr_code"), tx.get("ticket_url")


def payer_email_for_user(user_id: int) -> str:
    if MP_PAYER_EMAIL_PADRAO:
        return MP_PAYER_EMAIL_PADRAO
    # fallback válido
    return f"braoviana+tg{user_id}@gmail.com"


# =========================
# ESTADO (em memória) — modo teste “realista”
# =========================
# guardamos por usuário:
# context.application.bot_data["orders"][user_id] = {
#   "pending": {...} ou None
#   "active": {...} ou None
# }
def get_orders_store(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    store = context.application.bot_data.get("orders")
    if not isinstance(store, dict):
        store = {}
        context.application.bot_data["orders"] = store
    return store


def get_user_record(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Dict[str, Any]:
    store = get_orders_store(context)
    rec = store.get(str(user_id))
    if not isinstance(rec, dict):
        rec = {"pending": None, "active": None}
        store[str(user_id)] = rec
    return rec


def set_pending(context: ContextTypes.DEFAULT_TYPE, user_id: int, pending: Dict[str, Any]):
    rec = get_user_record(context, user_id)
    rec["pending"] = pending
    get_orders_store(context)[str(user_id)] = rec


def set_active(context: ContextTypes.DEFAULT_TYPE, user_id: int, active: Dict[str, Any]):
    rec = get_user_record(context, user_id)
    rec["active"] = active
    rec["pending"] = None
    get_orders_store(context)[str(user_id)] = rec


# =========================
# HELPERS
# =========================
def is_start_like(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("/start", "start", "menu", "iniciar", "começar", "comecar")


def format_dt_br(dt: datetime) -> str:
    # Bahia é -03:00 na maior parte do tempo. Vamos exibir em BRT.
    brt = timezone(timedelta(hours=-3))
    return dt.astimezone(brt).strftime("%d/%m/%Y %H:%M")


def renewal_window_left(expires_at: datetime) -> timedelta:
    return expires_at - now_utc()


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(WELCOME_TEXT, reply_markup=keyboard_initial())


async def renovar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Mantive esse comando para você testar o menu de renovação manualmente
    context.user_data.clear()
    await update.message.reply_text(RENEW_TEXT, reply_markup=keyboard_renewal())


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rec = get_user_record(context, user_id)
    active = rec.get("active")
    pending = rec.get("pending")

    lines = []
    if pending:
        lines.append(
            f"🟡 PENDENTE: {pending['plan_name']} | R${pending['amount']:.2f} | criado {format_dt_br(pending['created_at'])}"
        )
    if active:
        exp = active["expires_at"]
        lines.append(
            f"🟢 ATIVO: {active['plan_name']} | expira {format_dt_br(exp)}"
        )
        left = renewal_window_left(exp)
        if timedelta(0) < left <= timedelta(hours=24):
            lines.append("🎁 Você está na janela de RENOVAÇÃO (últimas 24h).")
        elif left <= timedelta(0):
            lines.append("🔴 Já deveria estar expirado.")
    if not lines:
        lines.append("Nenhuma assinatura/pedido registrado ainda.")

    await update.message.reply_text("\n".join(lines))


async def aprovar_teste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Sem permissão.")
        return

    # você pode passar um ID: /aprovar_teste 123456
    args = context.args or []
    target_user_id = user_id
    if args and args[0].isdigit():
        target_user_id = int(args[0])

    rec = get_user_record(context, target_user_id)
    pending = rec.get("pending")
    if not pending:
        await update.message.reply_text("⚠️ Não existe pedido pendente para aprovar (teste).")
        return

    approved_at = now_utc()
    expires_at = approved_at + timedelta(days=int(pending["days"]))

    active = {
        "plan_name": pending["plan_name"],
        "amount": pending["amount"],
        "days": pending["days"],
        "approved_at": approved_at,
        "expires_at": expires_at,
        "test_mode": True,
    }
    set_active(context, target_user_id, active)

    # avisa o usuário alvo
    try:
        chat_id = pending["chat_id"]
        txt = (
            "✅ Pagamento aprovado (TESTE)!\n\n"
            f"📦 Plano: {active['plan_name']}\n"
            f"⏳ Válido até: {format_dt_br(expires_at)}\n"
        )
        if GROUP_INVITE_LINK:
            txt += f"\n🔗 Link do grupo: {GROUP_INVITE_LINK}\n"
        await context.bot.send_message(chat_id=chat_id, text=txt)
    except Exception as e:
        logger.warning(f"Falha ao notificar usuário alvo no aprovar_teste: {e}")

    await update.message.reply_text(
        f"✅ Aprovado em modo teste para user_id={target_user_id}.\nExpira em {format_dt_br(expires_at)}"
    )


async def expirar_teste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Sem permissão.")
        return

    args = context.args or []
    target_user_id = user_id
    if args and args[0].isdigit():
        target_user_id = int(args[0])

    rec = get_user_record(context, target_user_id)
    active = rec.get("active")
    if not active:
        await update.message.reply_text("⚠️ Não existe assinatura ativa para expirar (teste).")
        return

    rec["active"] = None
    get_orders_store(context)[str(target_user_id)] = rec

    await update.message.reply_text(f"⛔ Expiração forçada (teste) para user_id={target_user_id}.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # Navegação
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

    # anti-spam
    if context.user_data.get("gerando_pix"):
        await query.message.reply_text("⏳ Já estou gerando um PIX pra você. Aguarde alguns segundos…")
        return

    context.user_data["gerando_pix"] = True
    try:
        plan = plans[key]
        plan_name = plan["name"]
        amount = float(plan["amount"])
        days = int(plan["days"])

        user = query.from_user
        chat_id = query.message.chat_id

        # feedback imediato
        try:
            await query.edit_message_text(
                f"⏳ Gerando seu PIX...\n\nPlano: {plan_name}\nValor: R${amount:.2f}"
            )
        except Exception:
            pass

        email = payer_email_for_user(user.id)

        status, payment = await asyncio.to_thread(
            gerar_pix,
            amount,
            f"{plan_name} - Prime VIP",
            email
        )

        if status not in (200, 201) or not isinstance(payment, dict):
            await query.message.reply_text(
                "❌ Erro ao gerar Pix. Tente novamente.\n\n"
                f"Status: {status}\n"
                f"Resposta: {str(payment)[:2500]}"
            )
            return

        qr_code, ticket_url = extrair_pix_copia_cola(payment)
        if not qr_code:
            await query.message.reply_text(
                "❌ O Mercado Pago não retornou o código Pix (copia e cola).\n\n"
                f"Resposta: {str(payment)[:2500]}"
            )
            return

        # salva "pedido pendente" (para o /aprovar_teste ficar realista)
        pending = {
            "plan_name": plan_name,
            "amount": amount,
            "days": days,
            "created_at": now_utc(),
            "chat_id": chat_id,
            "mode": mode,     # initial/renew
            "key": key,
            "test_approvable": True
        }
        set_pending(context, user.id, pending)

        # envia pix (sem txt)
        msg = (
            "✅ PIX GERADO COM SUCESSO!\n\n"
            f"Plano: {plan_name}\n"
            f"Valor: R${amount:.2f}\n\n"
            "📋 Copia e cola:\n"
            f"{qr_code}\n\n"
        )
        if ticket_url:
            msg += f"🔗 QR Code: {ticket_url}\n\n"
        msg += "⏳ Após pagar, aguarde a confirmação."

        await query.message.reply_text(msg)

        # dica só pra admin (silenciosa pro usuário normal)
        if is_admin(user.id):
            await query.message.reply_text(
                "🧪 TESTE: para simular aprovação sem pagar, use:\n"
                "/aprovar_teste"
            )

    finally:
        context.user_data["gerando_pix"] = False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # Qualquer start/menu/oi reseta e mostra menu (nunca gera Pix)
    if is_start_like(text) or text.lower() in ("oi", "ola", "olá"):
        context.user_data.clear()
        await update.message.reply_text(WELCOME_TEXT, reply_markup=keyboard_initial())
        return

    # Se digitar números, orienta usar botão
    if text.isdigit():
        await update.message.reply_text("Para escolher mais rápido, clique em um botão abaixo 👇", reply_markup=keyboard_initial())
        return

    await update.message.reply_text("Clique em um botão para escolher o plano 👇", reply_markup=keyboard_initial())


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
    app.add_handler(CommandHandler("renovar", renovar))        # opcional (teste manual)
    app.add_handler(CommandHandler("status", status_cmd))      # status do usuário
    app.add_handler(CommandHandler("aprovar_teste", aprovar_teste))  # admin
    app.add_handler(CommandHandler("expirar_teste", expirar_teste))  # admin

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    logger.info("Bot iniciado. Rodando polling...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
