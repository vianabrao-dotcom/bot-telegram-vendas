import os
import uuid
import json
import time
import logging
import asyncio
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from aiohttp import web


# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

# Grupo privado (para remover quando expirar)
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()

# Domínio para email "válido" (não usa seu email pessoal)
PAYER_EMAIL_DOMAIN = os.getenv("PAYER_EMAIL_DOMAIN", "primevip.com").strip()

# DB local (use Volume no Railway pra persistir)
USERS_DB_PATH = Path(os.getenv("USERS_DB_PATH", "/data/users.json" if os.getenv("RAILWAY_ENVIRONMENT") else "users.json"))

# Janela de renovação: 24h
RENEW_WINDOW_HOURS = int(os.getenv("RENEW_WINDOW_HOURS", "24"))

# Webhook server
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))

# URL pública do seu serviço (necessário para você cadastrar no Mercado Pago)
# Ex: https://observant-bravery-production-xxxx.up.railway.app
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

# Endpoint do webhook
MP_WEBHOOK_PATH = "/mp/webhook"

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
# PREÇOS
# =========================
# VALORES INICIAIS (menu padrão)
PRECO_INICIAL = {
    "1": ("Plano Semanal", 19.90, 7),
    "2": ("Plano Mensal", 29.90, 30),
    "3": ("Plano Anual", 39.90, 365),
}

# OPÇÃO 4: ANUAL PROMOCIONAL (menu padrão)
PRECO_ANUAL_PROMO = ("Plano Anual Promocional", 29.99, 365)

# MENU EXCLUSIVO DE RENOVAÇÃO (válido por 24h)
PRECO_RENOVACAO = {
    "1": ("Renovação Semanal", 10.90, 7),
    "2": ("Renovação Mensal", 15.90, 30),
    "3": ("Renovação Anual", 19.90, 365),
}


# =========================
# TEXTOS
# =========================
def menu_inicial_texto() -> str:
    return (
        "🔥 Bem-vindo! Você acaba de garantir acesso ao conteúdo mais exclusivo e atualizado do momento!\n"
        "Centenas de pessoas já estão dentro aproveitando todos os benefícios. Agora é a sua vez!\n\n"
        "Escolha abaixo o plano ideal e entre imediatamente no grupo privado: 👇\n\n"
        f"1️⃣ {PRECO_INICIAL['1'][0]} — R${PRECO_INICIAL['1'][1]:.2f}\n"
        f"2️⃣ {PRECO_INICIAL['2'][0]} — R${PRECO_INICIAL['2'][1]:.2f}\n"
        f"3️⃣ {PRECO_INICIAL['3'][0]} — R${PRECO_INICIAL['3'][1]:.2f}\n\n"
        f"4️⃣ 🎁 {PRECO_ANUAL_PROMO[0]} — R${PRECO_ANUAL_PROMO[1]:.2f}\n\n"
        "Digite apenas o número do plano desejado."
    )

def menu_renovacao_texto() -> str:
    return (
        "🎁 MENU EXCLUSIVO DE RENOVAÇÃO (válido por 24 horas)\n\n"
        "🔥 Oferta liberada por 24 horas:\n\n"
        f"1️⃣ {PRECO_RENOVACAO['1'][0]} — R${PRECO_RENOVACAO['1'][1]:.2f}\n"
        f"2️⃣ {PRECO_RENOVACAO['2'][0]} — R${PRECO_RENOVACAO['2'][1]:.2f}\n"
        f"3️⃣ {PRECO_RENOVACAO['3'][0]} — R${PRECO_RENOVACAO['3'][1]:.2f}\n\n"
        "Esses valores expiram em 24 horas."
    )


# =========================
# USERS DB
# =========================
# Por usuário:
# {
#   "stage": "INITIAL" | "PENDING" | "ACTIVE" | "RENEW_WINDOW" | "EXPIRED",
#   "expires_at": 1700000000,           # epoch seconds (SÓ quando approved)
#   "last_payment_id": "123",
#   "pending": {
#       "payment_id": "123",
#       "days": 30,
#       "plan_name": "Plano Mensal",
#       "amount": 29.90,
#       "created_at": 1700000000
#   }
# }
def load_users_db() -> dict:
    try:
        if USERS_DB_PATH.exists():
            return json.loads(USERS_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def save_users_db(db: dict) -> None:
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERS_DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

def get_user(db: dict, user_id: int) -> dict:
    return db.get(str(user_id), {})

def set_user(db: dict, user_id: int, payload: dict) -> None:
    db[str(user_id)] = payload

def now_ts() -> int:
    return int(time.time())


# =========================
# MERCADO PAGO
# =========================
def gerar_pix(valor: float, descricao: str, payer_email: str, payer_first_name: str = "Cliente", payer_last_name: str = "VIP"):
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

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    return resp.status_code, data

def extrair_pix(mp_response: dict):
    poi = mp_response.get("point_of_interaction", {}) if isinstance(mp_response, dict) else {}
    tx = poi.get("transaction_data", {}) if isinstance(poi, dict) else {}

    qr_code = tx.get("qr_code")
    ticket_url = tx.get("ticket_url")
    payment_id = mp_response.get("id") if isinstance(mp_response, dict) else None

    return qr_code, ticket_url, payment_id

def buscar_pagamento(payment_id: str) -> dict:
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


# =========================
# EMAIL DO PAYER
# =========================
def make_payer_email(user_id: int) -> str:
    return f"user{user_id}@{PAYER_EMAIL_DOMAIN}"


# =========================
# REGRAS DE MENU
# =========================
def is_in_renew_window(user_data: dict) -> bool:
    exp = user_data.get("expires_at")
    if not exp:
        return False
    return (exp - now_ts()) <= (RENEW_WINDOW_HOURS * 3600) and (exp - now_ts()) > 0

def is_expired(user_data: dict) -> bool:
    exp = user_data.get("expires_at")
    return bool(exp) and now_ts() >= exp


# =========================
# GRUPO (REMOVER)
# =========================
async def remover_do_grupo_se_configurado(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if not TELEGRAM_GROUP_ID:
        return
    try:
        await context.bot.ban_chat_member(chat_id=int(TELEGRAM_GROUP_ID), user_id=user_id)
        await context.bot.unban_chat_member(chat_id=int(TELEGRAM_GROUP_ID), user_id=user_id)
    except Exception as e:
        logger.warning(f"Falha ao remover do grupo user_id={user_id}: {e}")


# =========================
# STATUS / APROVAÇÃO
# =========================
def parse_mp_datetime(dt_str: str) -> int:
    """
    Mercado Pago costuma retornar ISO com timezone (ex: 2026-02-16T06:27:55.123-04:00).
    Se falhar, usa agora.
    """
    if not dt_str:
        return now_ts()
    try:
        # Python 3.11: fromisoformat entende offset
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return now_ts()

def aplicar_aprovacao(db: dict, user_id: int, payment_data: dict) -> bool:
    """
    Se o pagamento está approved e existe um pending compatível,
    grava expires_at SOMENTE aqui.
    """
    u = get_user(db, user_id)
    pending = u.get("pending") or {}
    pending_payment_id = str(pending.get("payment_id") or "")

    payment_id = str(payment_data.get("id") or "")
    status = (payment_data.get("status") or "").lower()

    if not payment_id or status != "approved":
        return False

    # Só aplica se for o mesmo pagamento pendente
    if pending_payment_id and pending_payment_id != payment_id:
        return False

    days = int(pending.get("days") or 0)
    if days <= 0:
        # fallback: se não tiver days salvo, não aplica
        return False

    approved_ts = parse_mp_datetime(payment_data.get("date_approved") or payment_data.get("date_created"))
    expires_at = approved_ts + (days * 24 * 3600)

    u["stage"] = "ACTIVE"
    u["expires_at"] = expires_at
    u["last_payment_id"] = payment_id
    u["pending"] = {}  # limpa pendência

    set_user(db, user_id, u)
    return True

def find_user_by_payment_id(db: dict, payment_id: str) -> int | None:
    pid = str(payment_id)
    for k, v in db.items():
        pending = (v or {}).get("pending") or {}
        if str(pending.get("payment_id") or "") == pid:
            try:
                return int(k)
            except Exception:
                return None
    return None


# =========================
# TELEGRAM HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_users_db()
    u = get_user(db, update.effective_user.id)

    if is_expired(u):
        u["stage"] = "EXPIRED"
        set_user(db, update.effective_user.id, u)
        save_users_db(db)

    if is_in_renew_window(u) or u.get("stage") == "RENEW_WINDOW":
        await update.message.reply_text(menu_renovacao_texto())
        return

    await update.message.reply_text(menu_inicial_texto())


async def renovar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_users_db()
    u = get_user(db, update.effective_user.id)

    if is_in_renew_window(u):
        u["stage"] = "RENEW_WINDOW"
        set_user(db, update.effective_user.id, u)
        save_users_db(db)
        await update.message.reply_text(menu_renovacao_texto())
    else:
        await update.message.reply_text("⏳ A renovação com desconto só aparece quando faltar 24h para encerrar sua assinatura.")


async def verificar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fallback manual: /verificar
    Se tiver um pagamento pendente salvo, consulta o MP e aplica aprovação.
    """
    db = load_users_db()
    user_id = update.effective_user.id
    u = get_user(db, user_id)
    pending = u.get("pending") or {}
    payment_id = str(pending.get("payment_id") or "")

    if not payment_id:
        await update.message.reply_text("Você não tem nenhum pagamento pendente para verificar.")
        return

    await update.message.reply_text("🔎 Verificando pagamento no Mercado Pago...")

    try:
        payment_data = buscar_pagamento(payment_id)
    except Exception as e:
        await update.message.reply_text(f"❌ Falha ao consultar pagamento: {e}")
        return

    status = (payment_data.get("status") or "").lower()

    if status == "approved":
        ok = aplicar_aprovacao(db, user_id, payment_data)
        save_users_db(db)
        if ok:
            await update.message.reply_text("✅ Pagamento aprovado! Sua assinatura foi ativada e a validade foi registrada.")
        else:
            await update.message.reply_text("✅ Pagamento aprovado, mas não consegui vincular ao seu pedido pendente. Me envie o print do pagamento e eu ajusto.")
    else:
        await update.message.reply_text(f"⏳ Ainda não aprovado. Status atual: {status}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (update.message.text or "").strip().lower()
    user = update.effective_user

    if texto in ("/start", "start"):
        await start(update, context)
        return

    db = load_users_db()
    u = get_user(db, user.id)

    # Expirado -> remove e volta pro menu inicial
    if is_expired(u):
        await remover_do_grupo_se_configurado(context, user.id)
        u = {"stage": "EXPIRED"}
        set_user(db, user.id, u)
        save_users_db(db)
        await update.message.reply_text("🚫 Sua assinatura expirou. Para voltar ao grupo, faça uma nova assinatura com os valores iniciais.\n\n" + menu_inicial_texto())
        return

    in_renew = is_in_renew_window(u) or u.get("stage") == "RENEW_WINDOW"

    if in_renew:
        escolhas = {"1", "2", "3"}
    else:
        escolhas = {"1", "2", "3", "4"}

    if texto not in escolhas:
        await update.message.reply_text("❌ Opção inválida. Digite apenas 1, 2, 3" + ("" if in_renew else " ou 4") + ".")
        await update.message.reply_text(menu_renovacao_texto() if in_renew else menu_inicial_texto())
        return

    # Seleciona plano
    if in_renew:
        nome_plano, valor, dias = PRECO_RENOVACAO[texto]
        descricao = f"{nome_plano} - Prime VIP"
    else:
        if texto == "4":
            nome_plano, valor, dias = PRECO_ANUAL_PROMO
            descricao = f"{nome_plano} - Prime VIP"
        else:
            nome_plano, valor, dias = PRECO_INICIAL[texto]
            descricao = f"{nome_plano} - Prime VIP"

    payer_email = make_payer_email(user.id)

    await update.message.reply_text("⏳ Gerando seu PIX...")

    try:
        status, pagamento = gerar_pix(
            valor=valor,
            descricao=descricao,
            payer_email=payer_email,
            payer_first_name=user.first_name or "Cliente",
            payer_last_name=user.last_name or "VIP",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erro na requisição do PIX: {e}")
        return

    if status not in (200, 201):
        await update.message.reply_text(
            "❌ Erro ao gerar Pix. Tente novamente.\n\n"
            f"Status: {status}\n"
            f"Resposta: {str(pagamento)[:3500]}"
        )
        return

    qr_code, ticket_url, payment_id = extrair_pix(pagamento)

    if not qr_code or not payment_id:
        await update.message.reply_text(
            "❌ O Mercado Pago criou a cobrança, mas não retornou o PIX corretamente.\n\n"
            f"Resposta: {str(pagamento)[:3500]}"
        )
        return

    # >>> AQUI É A MUDANÇA IMPORTANTE:
    # Não grava expires_at aqui. Só salva como PENDENTE aguardando approval.
    u["stage"] = "PENDING"
    u["pending"] = {
        "payment_id": str(payment_id),
        "days": int(dias),
        "plan_name": nome_plano,
        "amount": float(valor),
        "created_at": now_ts(),
    }
    set_user(db, user.id, u)
    save_users_db(db)

    # Mensagem
    msg = (
        "✅ PIX GERADO COM SUCESSO!\n\n"
        f"📦 Plano: {nome_plano}\n"
        f"💰 Valor: R${valor:.2f}\n\n"
        "📋 Copia e cola: (enviei abaixo e também em arquivo .txt)\n"
    )
    if ticket_url:
        msg += f"🔗 Link do QR: {ticket_url}\n\n"
    msg += "⏳ Após pagar, você pode usar /verificar para confirmar. (o webhook também confirma automaticamente)"
    await update.message.reply_text(msg)

    # Copia e cola como mensagem + txt (sempre copiável)
    await update.message.reply_text(qr_code)

    txt = BytesIO(qr_code.encode("utf-8"))
    txt.name = "pix_copia_e_cola.txt"
    await update.message.reply_document(document=txt, filename="pix_copia_e_cola.txt")


# =========================
# WEBHOOK MERCADO PAGO (AIOHTTP)
# =========================
async def mp_webhook(request: web.Request) -> web.Response:
    """
    Mercado Pago pode enviar id por querystring (?data.id=123) ou no json.
    A gente resolve assim:
    - tenta query param data.id
    - tenta json -> data.id ou id
    - consulta /v1/payments/{id}
    - se status approved, procura usuário pendente e ativa
    """
    try:
        db = load_users_db()

        # 1) tenta querystring
        payment_id = request.query.get("data.id") or request.query.get("id")

        # 2) tenta body
        body = None
        try:
            body = await request.json()
        except Exception:
            body = None

        if not payment_id and isinstance(body, dict):
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            payment_id = data.get("id") or body.get("id")

        if not payment_id:
            return web.json_response({"ok": True, "note": "no payment id"}, status=200)

        # consulta pagamento
        try:
            payment_data = await asyncio.to_thread(buscar_pagamento, str(payment_id))
        except Exception as e:
            logger.warning(f"Webhook: falha ao buscar pagamento {payment_id}: {e}")
            return web.json_response({"ok": True, "note": "cannot fetch payment"}, status=200)

        status = (payment_data.get("status") or "").lower()
        if status != "approved":
            return web.json_response({"ok": True, "status": status}, status=200)

        # encontra usuário pelo payment_id pendente
        user_id = find_user_by_payment_id(db, str(payment_id))
        if not user_id:
            # pode acontecer se o db resetou (sem Volume) ou se payment_id não foi salvo
            logger.warning(f"Webhook: payment approved {payment_id} mas não encontrei usuário pendente.")
            return web.json_response({"ok": True, "status": "approved_no_user"}, status=200)

        ok = aplicar_aprovacao(db, user_id, payment_data)
        save_users_db(db)

        if ok:
            # não mando msg automática aqui pra evitar spam; se quiser eu adiciono depois
            logger.info(f"Webhook: aprovado e ativado user_id={user_id}, payment_id={payment_id}")
            return web.json_response({"ok": True, "status": "approved_activated"}, status=200)

        return web.json_response({"ok": True, "status": "approved_not_applied"}, status=200)

    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return web.json_response({"ok": True}, status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_post(MP_WEBHOOK_PATH, mp_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    logger.info(f"Webhook server rodando em http://{WEBHOOK_HOST}:{WEBHOOK_PORT}{MP_WEBHOOK_PATH}")


# =========================
# MAIN
# =========================
async def run():
    # inicia webserver + bot
    await start_web_server()

    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("renovar", renovar))
    tg_app.add_handler(CommandHandler("verificar", verificar))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if PUBLIC_BASE_URL:
        logger.info(f"Cadastre no MP a URL de webhook: {PUBLIC_BASE_URL.rstrip('/')}{MP_WEBHOOK_PATH}")
    else:
        logger.warning("PUBLIC_BASE_URL não definido. Você precisa disso para cadastrar o webhook no Mercado Pago.")

    logger.info("Bot iniciado. Rodando polling...")
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()

    # segura o processo vivo
    while True:
        await asyncio.sleep(3600)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
