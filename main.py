import os
import re
import io
import uuid
import json
import time
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, timezone

import requests
from aiohttp import web

from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG / ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

# URL pública do Railway (ex: https://seu-servico.up.railway.app)
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

# Segredo simples pro webhook do MP (evita hits externos)
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()

# Grupo VIP (opcional, mas recomendado)
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()  # ex: -1001234567890

# Porta do Railway
PORT = int(os.getenv("PORT", "8080"))

# Banco local (Railway normalmente preserva o disco do serviço enquanto existir)
DB_PATH = os.getenv("DB_PATH", "primevip.sqlite3").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não encontrado nas variáveis de ambiente.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não encontrado nas variáveis de ambiente.")
if not BASE_URL:
    raise RuntimeError("BASE_URL não encontrado (ex: https://xxxx.up.railway.app).")
if not MP_WEBHOOK_SECRET:
    raise RuntimeError("MP_WEBHOOK_SECRET não encontrado (crie um texto aleatório).")

WEBHOOK_URL = f"{BASE_URL}/mp/webhook?secret={MP_WEBHOOK_SECRET}"

# =========================
# LOG
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("primevip")


# =========================
# DB (SQLite)
# =========================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        status TEXT NOT NULL,              -- active / expired
        plan_code TEXT NOT NULL,           -- W / M / A
        approved_payment_id INTEGER,
        approved_at TEXT,
        expires_at TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        payment_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        plan_code TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT NOT NULL,              -- pending / approved / rejected / cancelled
        external_reference TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(dt_str: str):
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str)


def upsert_payment(payment_id: int, user_id: int, chat_id: int, plan_code: str, amount: float, status: str, external_reference: str):
    conn = db_connect()
    cur = conn.cursor()
    ts = now_utc_iso()
    cur.execute("""
        INSERT INTO payments (payment_id, user_id, chat_id, plan_code, amount, status, external_reference, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(payment_id) DO UPDATE SET
            status=excluded.status,
            external_reference=excluded.external_reference,
            updated_at=excluded.updated_at
    """, (payment_id, user_id, chat_id, plan_code, amount, status, external_reference, ts, ts))
    conn.commit()
    conn.close()


def set_payment_status(payment_id: int, status: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE payments SET status=?, updated_at=? WHERE payment_id=?",
                (status, now_utc_iso(), payment_id))
    conn.commit()
    conn.close()


def get_subscription(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM subscriptions WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def upsert_subscription_active(user_id: int, chat_id: int, plan_code: str, approved_payment_id: int, approved_at_iso: str, expires_at_iso: str):
    conn = db_connect()
    cur = conn.cursor()
    created_at = now_utc_iso()
    cur.execute("""
        INSERT INTO subscriptions (user_id, chat_id, status, plan_code, approved_payment_id, approved_at, expires_at, created_at)
        VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            chat_id=excluded.chat_id,
            status='active',
            plan_code=excluded.plan_code,
            approved_payment_id=excluded.approved_payment_id,
            approved_at=excluded.approved_at,
            expires_at=excluded.expires_at
    """, (user_id, chat_id, plan_code, approved_payment_id, approved_at_iso, expires_at_iso, created_at))
    conn.commit()
    conn.close()


def mark_subscription_expired(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE subscriptions SET status='expired' WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def list_expired_active_subscriptions():
    """
    Retorna assinaturas ativas cujo expires_at já passou.
    """
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM subscriptions WHERE status='active' AND expires_at IS NOT NULL")
    rows = cur.fetchall()
    conn.close()

    expired = []
    now = datetime.now(timezone.utc)
    for r in rows:
        exp = parse_iso(r["expires_at"])
        if exp and exp <= now:
            expired.append(r)
    return expired


# =========================
# PLANOS / MENUS
# =========================
# Planos "originais" (menu inicial)
PLANS_INITIAL = {
    "1": ("Plano Semanal", 19.90, "W"),
    "2": ("Plano Mensal", 29.90, "M"),
    "3": ("Plano Anual", 39.90, "A"),
    # opção promocional anual
    "4": ("Plano Anual Promocional", 29.99, "A"),
}

# Planos de renovação (menu renovação, válido por 24h antes de expirar)
PLANS_RENEWAL = {
    "1": ("Plano Semanal (Renovação)", 10.90, "W"),
    "2": ("Plano Mensal (Renovação)", 15.90, "M"),
    "3": ("Plano Anual (Renovação)", 19.90, "A"),
}

WELCOME_TEXT = (
    "🔥 *Bem-vindo!* Você acaba de garantir acesso ao conteúdo mais exclusivo e atualizado do momento!\n"
    "Centenas de pessoas já estão dentro aproveitando todos os benefícios. Agora é sua vez!\n\n"
    "Escolha abaixo o plano ideal e entre imediatamente no grupo privado: 👇\n\n"
)

def menu_inicial():
    return (
        WELCOME_TEXT +
        "1️⃣ Plano Semanal — *R$19,90*\n"
        "2️⃣ Plano Mensal — *R$29,90*\n"
        "3️⃣ Plano Anual — *R$39,90*\n\n"
        "4️⃣ 🎁 *Plano Anual Promocional* — *R$29,99*\n\n"
        "_Digite apenas o número do plano desejado._"
    )

def menu_renovacao(expires_at_iso: str):
    exp = parse_iso(expires_at_iso)
    exp_local = exp.astimezone(timezone(timedelta(hours=-3))) if exp else None  # BRT aproximado
    exp_str = exp_local.strftime("%d/%m/%Y %H:%M") if exp_local else "em breve"
    return (
        "🎁 *MENU EXCLUSIVO DE RENOVAÇÃO (válido por 24h)*\n\n"
        "Oferta liberada por 24 horas:\n\n"
        "1️⃣ Plano Semanal — *R$10,90*\n"
        "2️⃣ Plano Mensal — *R$15,90*\n"
        "3️⃣ Plano Anual — *R$19,90*\n\n"
        f"⏳ Sua assinatura expira em: *{exp_str}*\n"
        "_Esses valores expiram em 24 horas._"
    )

def is_in_renewal_window(sub_row) -> bool:
    if not sub_row:
        return False
    if sub_row["status"] != "active":
        return False
    exp = parse_iso(sub_row["expires_at"])
    if not exp:
        return False
    now = datetime.now(timezone.utc)
    return (exp - now) <= timedelta(hours=24)


def plan_duration(plan_code: str) -> timedelta:
    if plan_code == "W":
        return timedelta(days=7)
    if plan_code == "M":
        return timedelta(days=30)
    if plan_code == "A":
        return timedelta(days=365)
    return timedelta(days=30)


# =========================
# MERCADO PAGO (PIX)
# =========================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def safe_payer_email(user) -> str:
    """
    Melhor prática aqui: usar um email real do seu negócio (estável e aceito).
    Isso evita erro de validação do MP e evita criar domínios "fakes" que às vezes falham.
    """
    # Se preferir outro, troque aqui.
    return "braoviana@gmail.com"

def mp_create_pix_payment(amount: float, description: str, payer_email: str, external_reference: str):
    url = "https://api.mercadopago.com/v1/payments"
    idempotency_key = str(uuid.uuid4())

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": idempotency_key,
    }

    if not EMAIL_RE.match(payer_email):
        raise ValueError("payer_email inválido")

    payload = {
        "transaction_amount": float(amount),
        "description": description,
        "payment_method_id": "pix",
        "notification_url": WEBHOOK_URL,  # <-- ESSENCIAL para Pix
        "external_reference": external_reference,
        "payer": {
            "email": payer_email,
        },
    }

    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    return resp.status_code, data


def mp_get_payment(payment_id: int):
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=30)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    return resp.status_code, data


def extract_pix_data(mp_response: dict):
    poi = mp_response.get("point_of_interaction", {}) if isinstance(mp_response, dict) else {}
    tx = poi.get("transaction_data", {}) if isinstance(poi, dict) else {}

    qr_code = tx.get("qr_code")
    ticket_url = tx.get("ticket_url")
    qr_code_base64 = tx.get("qr_code_base64")

    return qr_code, ticket_url, qr_code_base64


# =========================
# TELEGRAM: HELPERS
# =========================
async def send_pix_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, nome_plano: str, valor: float, qr_code: str, ticket_url: str):
    """
    Envia Pix no chat + anexa TXT para facilitar copiar (resolve o problema de "não consigo copiar")
    """
    # Mensagem curta + código (sem backticks gigantes — alguns clientes ficam ruins)
    msg = (
        "✅ *PIX GERADO COM SUCESSO!*\n\n"
        f"📦 Plano: *{nome_plano}*\n"
        f"💰 Valor: *R${valor:.2f}*\n\n"
        "📋 *Copia e cola:*\n"
        f"{qr_code}\n\n"
    )
    if ticket_url:
        msg += f"🔗 *Link do QR:* {ticket_url}\n\n"
    msg += "⏳ Após pagar, aguarde a confirmação."

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    # TXT anexo (muito mais fácil copiar/compartilhar)
    txt = f"Plano: {nome_plano}\nValor: R${valor:.2f}\n\nPIX COPIA E COLA:\n{qr_code}\n\nLink (se houver):\n{ticket_url or '-'}\n"
    bio = io.BytesIO(txt.encode("utf-8"))
    bio.name = "pix_copia_e_cola.txt"
    await update.message.reply_document(InputFile(bio), caption="📎 Arquivo com o PIX (copia e cola)")


async def try_send_group_invite(app, user_id: int, chat_id: int):
    """
    Cria convite do grupo e envia ao usuário.
    (O bot precisa ser admin no grupo e poder criar links.)
    """
    if not TELEGRAM_GROUP_ID:
        return

    group_id = int(TELEGRAM_GROUP_ID)
    try:
        invite = await app.bot.create_chat_invite_link(
            chat_id=group_id,
            member_limit=1,
            creates_join_request=False
        )
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ *Pagamento aprovado!*\n\n"
                "Aqui está seu link de acesso ao grupo VIP:\n"
                f"{invite.invite_link}\n\n"
                "Se tiver qualquer dúvida, me chame aqui."
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.exception("Falha ao criar/enviar convite do grupo: %s", e)
        await app.bot.send_message(
            chat_id=chat_id,
            text="✅ *Pagamento aprovado!* (Não consegui gerar o link do grupo automaticamente. Verifique se o bot é admin e tem permissão de criar convites.)",
            parse_mode=ParseMode.MARKDOWN
        )


async def remove_user_from_group(app, user_id: int):
    """
    Remove usuário do grupo ao expirar.
    """
    if not TELEGRAM_GROUP_ID:
        return
    group_id = int(TELEGRAM_GROUP_ID)
    try:
        # ban curto remove; depois pode desbanir automaticamente
        until = datetime.now(timezone.utc) + timedelta(minutes=1)
        await app.bot.ban_chat_member(chat_id=group_id, user_id=user_id, until_date=until)
        await app.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
    except Exception as e:
        logger.exception("Falha ao remover usuário do grupo: %s", e)


# =========================
# HANDLERS TELEGRAM
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sub = get_subscription(user.id)

    if sub and sub["status"] == "active" and is_in_renewal_window(sub):
        await update.message.reply_text(menu_renovacao(sub["expires_at"]), parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text(menu_inicial(), parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    texto = (update.message.text or "").strip()

    # aceitar "Start" como /start (muita gente digita Start)
    if texto.lower() in ("/start", "start"):
        await start(update, context)
        return

    sub = get_subscription(user.id)
    renewal_mode = sub and sub["status"] == "active" and is_in_renewal_window(sub)

    plans = PLANS_RENEWAL if renewal_mode else PLANS_INITIAL

    if texto not in plans:
        if renewal_mode:
            await update.message.reply_text("❌ Opção inválida. Digite 1, 2 ou 3.\n\n" + menu_renovacao(sub["expires_at"]), parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Opção inválida. Digite 1, 2, 3 ou 4.\n\n" + menu_inicial(), parse_mode=ParseMode.MARKDOWN)
        return

    nome_plano, valor, plan_code = plans[texto]

    await update.message.reply_text("⏳ Gerando seu PIX...")

    payer_email = safe_payer_email(user)

    # external_reference para identificar o usuário ao receber webhook
    external_reference = f"tg_user:{user.id}|chat:{chat_id}|plan:{plan_code}|opt:{texto}|ref:{uuid.uuid4().hex[:10]}"

    descricao = f"{nome_plano} - Prime VIP"

    # chamada MP (em thread pra não travar o bot)
    try:
        status_code, pagamento = await asyncio.to_thread(
            mp_create_pix_payment, valor, descricao, payer_email, external_reference
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erro interno ao gerar Pix: {e}")
        return

    if status_code not in (200, 201):
        await update.message.reply_text(
            "❌ *Erro ao gerar Pix.* Tente novamente.\n\n"
            f"Status: `{status_code}`\n"
            f"Resposta: `{str(pagamento)[:3500]}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    payment_id = pagamento.get("id")
    mp_status = pagamento.get("status", "pending")
    ext_ref = pagamento.get("external_reference", external_reference)

    if payment_id:
        upsert_payment(int(payment_id), user.id, chat_id, plan_code, float(valor), str(mp_status), str(ext_ref))

    qr_code, ticket_url, _ = extract_pix_data(pagamento)

    if not qr_code:
        await update.message.reply_text(
            "❌ O Mercado Pago criou o pagamento, mas não retornou o código PIX neste formato.\n\n"
            f"Resposta: `{str(pagamento)[:3500]}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await send_pix_to_user(update, context, nome_plano, float(valor), qr_code, ticket_url)


# =========================
# WEBHOOK MP (AIOHTTP)
# =========================
async def mp_webhook_handler(request: web.Request):
    # valida segredo
    secret = request.query.get("secret", "")
    if secret != MP_WEBHOOK_SECRET:
        return web.Response(status=401, text="unauthorized")

    # MP pode enviar JSON ou query params (topic/id)
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    payment_id = None

    # formato comum: {"type":"payment","data":{"id":123}}
    if isinstance(payload, dict):
        data = payload.get("data") or {}
        if isinstance(data, dict) and "id" in data:
            payment_id = data.get("id")

    # fallback por query string
    if not payment_id:
        payment_id = request.query.get("id") or request.query.get("data.id")

    if not payment_id:
        return web.Response(status=200, text="ok")

    try:
        payment_id = int(payment_id)
    except Exception:
        return web.Response(status=200, text="ok")

    # consulta pagamento no MP pra confirmar status + external_reference
    status_code, mp_data = await asyncio.to_thread(mp_get_payment, payment_id)
    if status_code != 200 or not isinstance(mp_data, dict):
        return web.Response(status=200, text="ok")

    status = mp_data.get("status", "")
    ext_ref = mp_data.get("external_reference", "")

    # tenta encontrar user/chat pelo external_reference salvo
    # (a gente salva payments quando cria)
    user_id = None
    chat_id = None
    plan_code = None
    amount = None

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,))
    row = cur.fetchone()
    conn.close()

    if row:
        user_id = int(row["user_id"])
        chat_id = int(row["chat_id"])
        plan_code = row["plan_code"]
        amount = float(row["amount"])

    # atualiza status do payment no DB
    try:
        set_payment_status(payment_id, status or "pending")
    except Exception:
        pass

    # se não achou no DB, não tem como liberar automaticamente
    if not user_id or not chat_id:
        logger.warning("Webhook recebido, mas pagamento %s não encontrado no DB. ext_ref=%s", payment_id, ext_ref)
        return web.Response(status=200, text="ok")

    # processa aprovação
    if status == "approved":
        approved_at = datetime.now(timezone.utc)
        expires_at = approved_at + plan_duration(plan_code)

        upsert_subscription_active(
            user_id=user_id,
            chat_id=chat_id,
            plan_code=plan_code,
            approved_payment_id=payment_id,
            approved_at_iso=approved_at.isoformat(),
            expires_at_iso=expires_at.isoformat()
        )

        # avisa e manda link do grupo
        app = request.app["tg_app"]
        try:
            await try_send_group_invite(app, user_id=user_id, chat_id=chat_id)
        except Exception:
            logger.exception("Falha ao avisar usuário após approved.")

    return web.Response(status=200, text="ok")


# =========================
# JOB: EXPIRAR E REMOVER DO GRUPO
# =========================
async def enforce_expirations(app):
    expired = list_expired_active_subscriptions()
    if not expired:
        return

    for sub in expired:
        user_id = int(sub["user_id"])
        chat_id = int(sub["chat_id"])

        mark_subscription_expired(user_id)

        try:
            await remove_user_from_group(app, user_id)
        except Exception:
            logger.exception("Falha removendo do grupo user_id=%s", user_id)

        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⛔️ Sua assinatura expirou e o acesso foi encerrado.\n\n"
                    "Para voltar, faça uma nova assinatura com os valores iniciais.\n\n"
                    "Digite /start para ver os planos."
                )
            )
        except Exception:
            logger.exception("Falha enviando msg de expiração user_id=%s", user_id)


async def expiration_sweeper(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    await enforce_expirations(app)


# =========================
# STARTUP / AIOHTTP SERVER
# =========================
async def post_init(app):
    # inicia DB
    db_init()

    # inicia servidor aiohttp (para webhook MP)
    aio = web.Application()
    aio["tg_app"] = app
    aio.router.add_post("/mp/webhook", mp_webhook_handler)

    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    app.bot_data["aio_runner"] = runner
    logger.info("Servidor MP webhook ativo em %s", WEBHOOK_URL)

    # job para varrer expirados a cada 10 min
    app.job_queue.run_repeating(expiration_sweeper, interval=600, first=30)


async def post_shutdown(app):
    runner = app.bot_data.get("aio_runner")
    if runner:
        try:
            await runner.cleanup()
        except Exception:
            pass


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado. Rodando polling + webhook MP...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
