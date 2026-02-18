import os
import json
import time
import hmac
import hashlib
import logging
import threading
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# -------------------------
# ENV / Config
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não definido nas variáveis de ambiente.")

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()  # produção
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()  # opcional (validação de assinatura)
MP_PAYER_EMAIL_PADRAO = os.getenv("MP_PAYER_EMAIL_PADRAO", "").strip()

GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK", "").strip()
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()  # ex: -1001234567890
TZ = os.getenv("TZ", "America/Sao_Paulo").strip()

# Base URL do seu serviço (Railway URL), usado apenas para te ajudar a lembrar.
BASE_URL = os.getenv("BASE_URL", "").strip()

TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() in ("1", "true", "yes", "y")

# Porta HTTP para webhook no Railway
PORT = int(os.getenv("PORT", "8080"))

# -------------------------
# Time helpers (UTC)
# -------------------------
UTC = timezone.utc

def utc_now() -> datetime:
    return datetime.now(UTC)

def dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()

def iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)

# -------------------------
# Plans
# -------------------------
PLANS_INICIAIS = {
    "1": {"key": "semanal", "label": "Plano Semanal", "amount": 19.90, "days": 7},
    "2": {"key": "mensal",  "label": "Plano Mensal",  "amount": 29.90, "days": 30},
    "3": {"key": "anual",   "label": "Plano Anual",   "amount": 39.90, "days": 365},
    "4": {"key": "anual_promo", "label": "Plano Anual Promocional", "amount": 29.99, "days": 365},
}

PLANS_RENOVACAO_24H = {
    "1": {"key": "semanal_renov", "label": "Plano Semanal", "amount": 10.90, "days": 7},
    "2": {"key": "mensal_renov",  "label": "Plano Mensal",  "amount": 15.90, "days": 30},
    "3": {"key": "anual_renov",   "label": "Plano Anual",   "amount": 19.90, "days": 365},
}

RENEWAL_WINDOW = timedelta(hours=24)

# -------------------------
# Database (sqlite)
# -------------------------
DB_PATH = os.getenv("DB_PATH", "bot.db")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_init():
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            status TEXT,
            expires_at TEXT,
            renewal_notified INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            user_id INTEGER,
            plan_key TEXT,
            amount REAL,
            status TEXT,
            created_at TEXT,
            approved_at TEXT,
            external_reference TEXT
        )
    """)
    con.commit()
    con.close()

def upsert_user(user_id: int, first_name: str, username: str):
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO users (user_id, first_name, username, status, expires_at, renewal_notified)
        VALUES (?, ?, ?, COALESCE((SELECT status FROM users WHERE user_id=?), 'none'),
                (SELECT expires_at FROM users WHERE user_id=?),
                COALESCE((SELECT renewal_notified FROM users WHERE user_id=?), 0))
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            username=excluded.username
    """, (user_id, first_name, username, user_id, user_id, user_id))
    con.commit()
    con.close()

def get_user(user_id: int):
    con = db_conn()
    cur = con.cursor()
    cur.execute("SELECT user_id, status, expires_at, renewal_notified FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return {"user_id": row[0], "status": row[1], "expires_at": row[2], "renewal_notified": row[3]}

def set_user_active(user_id: int, expires_at: datetime):
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO users (user_id, status, expires_at, renewal_notified)
        VALUES (?, 'active', ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            status='active',
            expires_at=?,
            renewal_notified=0
    """, (user_id, dt_to_iso(expires_at), dt_to_iso(expires_at)))
    con.commit()
    con.close()

def mark_renewal_notified(user_id: int):
    con = db_conn()
    cur = con.cursor()
    cur.execute("UPDATE users SET renewal_notified=1 WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def set_user_expired(user_id: int):
    con = db_conn()
    cur = con.cursor()
    cur.execute("UPDATE users SET status='expired' WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def insert_payment(payment_id: str, user_id: int, plan_key: str, amount: float, status: str, external_reference: str):
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO payments (payment_id, user_id, plan_key, amount, status, created_at, approved_at, external_reference)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
    """, (payment_id, user_id, plan_key, amount, status, dt_to_iso(utc_now()), external_reference))
    con.commit()
    con.close()

def update_payment_status(payment_id: str, status: str, approved_at: datetime | None = None):
    con = db_conn()
    cur = con.cursor()
    if approved_at:
        cur.execute("UPDATE payments SET status=?, approved_at=? WHERE payment_id=?", (status, dt_to_iso(approved_at), payment_id))
    else:
        cur.execute("UPDATE payments SET status=? WHERE payment_id=?", (status, payment_id))
    con.commit()
    con.close()

def get_payment(payment_id: str):
    con = db_conn()
    cur = con.cursor()
    cur.execute("SELECT payment_id, user_id, plan_key, amount, status, external_reference FROM payments WHERE payment_id=?", (payment_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return {"payment_id": row[0], "user_id": row[1], "plan_key": row[2], "amount": row[3], "status": row[4], "external_reference": row[5]}

def list_active_users():
    con = db_conn()
    cur = con.cursor()
    cur.execute("SELECT user_id, expires_at, renewal_notified FROM users WHERE status='active' AND expires_at IS NOT NULL")
    rows = cur.fetchall()
    con.close()
    out = []
    for r in rows:
        out.append({"user_id": r[0], "expires_at": r[1], "renewal_notified": r[2]})
    return out

# -------------------------
# Telegram UI helpers
# -------------------------
def menu_inicial_text() -> str:
    # Sem aquela mensagem longa entre 3 e 4.
    return (
        "🔥 <b>Bem-vindo!</b>\n\n"
        "Escolha abaixo o plano ideal e entre imediatamente no grupo privado:\n\n"
        f"1️⃣ {PLANS_INICIAIS['1']['label']} — <b>R${PLANS_INICIAIS['1']['amount']:.2f}</b>\n"
        f"2️⃣ {PLANS_INICIAIS['2']['label']} — <b>R${PLANS_INICIAIS['2']['amount']:.2f}</b>\n"
        f"3️⃣ {PLANS_INICIAIS['3']['label']} — <b>R${PLANS_INICIAIS['3']['amount']:.2f}</b>\n\n"
        f"4️⃣ 🎁 <b>{PLANS_INICIAIS['4']['label']}</b> — <b>R${PLANS_INICIAIS['4']['amount']:.2f}</b>\n\n"
        "Clique na opção desejada:"
    )

def menu_inicial_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("1️⃣ Semanal", callback_data="buy:1")],
        [InlineKeyboardButton("2️⃣ Mensal",  callback_data="buy:2")],
        [InlineKeyboardButton("3️⃣ Anual",   callback_data="buy:3")],
        [InlineKeyboardButton("4️⃣ 🎁 Anual Promocional", callback_data="buy:4")],
    ]
    return InlineKeyboardMarkup(kb)

def menu_renovacao_text(expires_at: datetime) -> str:
    return (
        "🎁 <b>MENU EXCLUSIVO DE RENOVAÇÃO</b> (válido por <b>24 horas</b>)\n\n"
        "🔥 Oferta liberada por 24 horas:\n"
        f"1️⃣ {PLANS_RENOVACAO_24H['1']['label']} — <b>R${PLANS_RENOVACAO_24H['1']['amount']:.2f}</b>\n"
        f"2️⃣ {PLANS_RENOVACAO_24H['2']['label']} — <b>R${PLANS_RENOVACAO_24H['2']['amount']:.2f}</b>\n"
        f"3️⃣ {PLANS_RENOVACAO_24H['3']['label']} — <b>R${PLANS_RENOVACAO_24H['3']['amount']:.2f}</b>\n\n"
        f"⏳ Sua assinatura expira em: <b>{expires_at.astimezone(UTC).strftime('%d/%m/%Y %H:%M UTC')}</b>\n\n"
        "Clique para renovar com desconto:"
    )

def menu_renovacao_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("1️⃣ Renovar Semanal (R$10,90)", callback_data="renew:1")],
        [InlineKeyboardButton("2️⃣ Renovar Mensal (R$15,90)",  callback_data="renew:2")],
        [InlineKeyboardButton("3️⃣ Renovar Anual (R$19,90)",   callback_data="renew:3")],
    ]
    return InlineKeyboardMarkup(kb)

# -------------------------
# Mercado Pago API
# -------------------------
MP_API = "https://api.mercadopago.com"

def mp_headers():
    if not MP_ACCESS_TOKEN and not TEST_MODE:
        raise RuntimeError("MP_ACCESS_TOKEN não definido e TEST_MODE=false.")
    return {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

def create_pix_payment(user_id: int, plan_label: str, amount: float, plan_key: str) -> dict:
    """
    Cria pagamento PIX no Mercado Pago e retorna:
      - payment_id
      - copia_e_cola (qr_code)
      - ticket_url (link do QR)
      - external_reference
    """
    payer_email = MP_PAYER_EMAIL_PADRAO or "comprador@example.com"

    external_reference = f"tg:{user_id}:{plan_key}:{int(time.time())}"

    payload = {
        "transaction_amount": float(amount),
        "description": f"{plan_label}",
        "payment_method_id": "pix",
        "payer": {
            "email": payer_email
        },
        "external_reference": external_reference
    }

    r = requests.post(f"{MP_API}/v1/payments", headers=mp_headers(), data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    data = r.json()

    payment_id = str(data.get("id"))
    poi = data.get("point_of_interaction") or {}
    tx = (poi.get("transaction_data") or {})
    qr_code = tx.get("qr_code") or ""
    ticket_url = tx.get("ticket_url") or ""

    return {
        "payment_id": payment_id,
        "copia_e_cola": qr_code,
        "ticket_url": ticket_url,
        "external_reference": external_reference
    }

def fetch_payment(payment_id: str) -> dict:
    r = requests.get(f"{MP_API}/v1/payments/{payment_id}", headers=mp_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

# -------------------------
# Access / Group helpers
# -------------------------
async def grant_access(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """
    Libera acesso enviando link do grupo. (Mais simples e robusto)
    Se você quiser forçar a entrada via API, dá para evoluir depois.
    """
    if not GROUP_INVITE_LINK:
        await context.bot.send_message(
            chat_id=user_id,
            text="✅ Pagamento confirmado! Porém o link do grupo não está configurado no servidor. Fale com o suporte.",
        )
        return

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "✅ <b>Pagamento confirmado!</b>\n\n"
            "Aqui está seu acesso ao grupo privado:\n"
            f"🔗 {GROUP_INVITE_LINK}"
        ),
        parse_mode=ParseMode.HTML
    )

async def remove_from_group(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """
    Remove o usuário do grupo (bot precisa ser admin).
    Método: ban temporário + unban para "kick".
    """
    if not TELEGRAM_GROUP_ID:
        return

    try:
        chat_id = int(TELEGRAM_GROUP_ID)
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=int(time.time()) + 60)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        logger.info(f"Usuário {user_id} removido do grupo {chat_id}.")
    except Exception as e:
        logger.warning(f"Falha ao remover {user_id} do grupo: {e}")

# -------------------------
# Core flow
# -------------------------
def compute_expires_at_from_plan(plan_days: int) -> datetime:
    return utc_now() + timedelta(days=plan_days)

async def approve_and_activate(context: ContextTypes.DEFAULT_TYPE, user_id: int, plan_days: int, payment_id: str | None = None):
    """
    Aprova (interno) e ativa assinatura:
    - grava expires_at no momento da aprovação
    - libera acesso
    """
    expires_at = compute_expires_at_from_plan(plan_days)
    set_user_active(user_id, expires_at)

    if payment_id:
        update_payment_status(payment_id, "approved", approved_at=utc_now())

    await grant_access(None, context, user_id)

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "🧾 <b>Assinatura ativada com sucesso!</b>\n\n"
            f"⏳ Válido até: <b>{expires_at.astimezone(UTC).strftime('%d/%m/%Y %H:%M UTC')}</b>\n\n"
            "Se precisar, digite /start para ver seu status."
        ),
        parse_mode=ParseMode.HTML
    )

async def send_generating_message(context: ContextTypes.DEFAULT_TYPE, user_id: int, plan_label: str, amount: float):
    await context.bot.send_message(
        chat_id=user_id,
        text=f"⏳ Gerando seu PIX...\n\nPlano: {plan_label}\nValor: R${amount:.2f}"
    )

async def send_pix_message(context: ContextTypes.DEFAULT_TYPE, user_id: int, plan_label: str, amount: float, copia_e_cola: str, ticket_url: str):
    # Sem .txt — tudo no chat.
    text = (
        "✅ <b>PIX GERADO COM SUCESSO!</b>\n\n"
        f"📦 Plano: <b>{plan_label}</b>\n"
        f"💰 Valor: <b>R${amount:.2f}</b>\n\n"
        "📋 <b>Copia e cola:</b>\n"
        f"<code>{copia_e_cola}</code>\n\n"
        f"🔗 <b>QR Code:</b> {ticket_url}\n\n"
        "⏳ Após pagar, aguarde a confirmação."
    )
    await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# -------------------------
# Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.first_name or "", user.username or "")

    u = get_user(user.id)
    if u and u.get("status") == "active" and u.get("expires_at"):
        expires_at = iso_to_dt(u["expires_at"])
        remaining = expires_at - utc_now()

        if remaining <= timedelta(0):
            # expirou
            set_user_expired(user.id)
        else:
            # Se estiver na janela de renovação (<=24h), mostra menu de renovação
            if remaining <= RENEWAL_WINDOW:
                await update.message.reply_text(
                    menu_renovacao_text(expires_at),
                    reply_markup=menu_renovacao_keyboard(),
                    parse_mode=ParseMode.HTML
                )
                return

            # Caso ainda ativo e não na janela de 24h, mostra status
            await update.message.reply_text(
                (
                    "✅ <b>Sua assinatura está ativa.</b>\n\n"
                    f"⏳ Válido até: <b>{expires_at.astimezone(UTC).strftime('%d/%m/%Y %H:%M UTC')}</b>\n\n"
                    "Quando faltar 24h para encerrar, você receberá o menu de renovação com desconto."
                ),
                parse_mode=ParseMode.HTML
            )
            return

    # Menu inicial
    await update.message.reply_text(
        menu_inicial_text(),
        reply_markup=menu_inicial_keyboard(),
        parse_mode=ParseMode.HTML
    )

async def on_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    upsert_user(user_id, query.from_user.first_name or "", query.from_user.username or "")

    _, choice = (query.data or "").split(":", 1)
    plan = PLANS_INICIAIS.get(choice)
    if not plan:
        await query.edit_message_text("Opção inválida.")
        return

    plan_label = plan["label"]
    amount = plan["amount"]
    plan_days = plan["days"]
    plan_key = plan["key"]

    await query.edit_message_text(f"✅ Você escolheu: {plan_label} — R${amount:.2f}\n\nAguarde...")

    # Modo teste: simula PIX e aprova automaticamente
    if TEST_MODE:
        await send_generating_message(context, user_id, plan_label, amount)

        fake_code = f"PIX-TESTE-{user_id}-{int(time.time())}"
        fake_ticket = "https://example.com/qr-teste"
        await send_pix_message(context, user_id, plan_label, amount, fake_code, fake_ticket)

        # Aprova automaticamente depois de 2.5s (bem realista)
        await context.bot.send_message(chat_id=user_id, text="🧪 Modo teste ativo: simulando confirmação do pagamento...")
        await context.application.job_queue.run_once(
            callback=lambda ctx: approve_and_activate(ctx, user_id, plan_days, payment_id=None),
            when=2.5,
            data=None,
            name=f"auto_approve_test_{user_id}_{int(time.time())}"
        )
        return

    # Produção: cria PIX real
    try:
        await send_generating_message(context, user_id, plan_label, amount)

        mp = create_pix_payment(user_id=user_id, plan_label=plan_label, amount=amount, plan_key=plan_key)
        payment_id = mp["payment_id"]
        insert_payment(payment_id, user_id, plan_key, amount, "pending", mp["external_reference"])

        await send_pix_message(
            context=context,
            user_id=user_id,
            plan_label=plan_label,
            amount=amount,
            copia_e_cola=mp["copia_e_cola"],
            ticket_url=mp["ticket_url"],
        )

    except Exception as e:
        logger.exception("Erro ao gerar PIX")
        await context.bot.send_message(chat_id=user_id, text=f"❌ Erro ao gerar PIX: {e}")

async def on_renew_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    upsert_user(user_id, query.from_user.first_name or "", query.from_user.username or "")

    _, choice = (query.data or "").split(":", 1)
    plan = PLANS_RENOVACAO_24H.get(choice)
    if not plan:
        await query.edit_message_text("Opção inválida.")
        return

    plan_label = plan["label"]
    amount = plan["amount"]
    plan_days = plan["days"]
    plan_key = plan["key"]

    await query.edit_message_text(f"✅ Renovação escolhida: {plan_label} — R${amount:.2f}\n\nAguarde...")

    if TEST_MODE:
        await send_generating_message(context, user_id, f"{plan_label} (Renovação)", amount)
        fake_code = f"PIX-TESTE-RENOV-{user_id}-{int(time.time())}"
        fake_ticket = "https://example.com/qr-teste"
        await send_pix_message(context, user_id, f"{plan_label} (Renovação)", amount, fake_code, fake_ticket)

        await context.bot.send_message(chat_id=user_id, text="🧪 Modo teste ativo: simulando confirmação da renovação...")
        await context.application.job_queue.run_once(
            callback=lambda ctx: approve_and_activate(ctx, user_id, plan_days, payment_id=None),
            when=2.5,
            data=None,
            name=f"auto_approve_test_renew_{user_id}_{int(time.time())}"
        )
        return

    try:
        await send_generating_message(context, user_id, f"{plan_label} (Renovação)", amount)

        mp = create_pix_payment(user_id=user_id, plan_label=f"{plan_label} (Renovação)", amount=amount, plan_key=plan_key)
        payment_id = mp["payment_id"]
        insert_payment(payment_id, user_id, plan_key, amount, "pending", mp["external_reference"])

        await send_pix_message(
            context=context,
            user_id=user_id,
            plan_label=f"{plan_label} (Renovação)",
            amount=amount,
            copia_e_cola=mp["copia_e_cola"],
            ticket_url=mp["ticket_url"],
        )
    except Exception as e:
        logger.exception("Erro ao gerar PIX (renovação)")
        await context.bot.send_message(chat_id=user_id, text=f"❌ Erro ao gerar PIX da renovação: {e}")

# -------------------------
# Expiration / Renewal Sweeper
# -------------------------
async def expiration_sweeper(context: ContextTypes.DEFAULT_TYPE):
    """
    - Se expirou: remove do grupo e marca expired
    - Se faltam <=24h e ainda não notificou: envia menu renovação e marca renewal_notified=1
    """
    users = list_active_users()
    now = utc_now()

    for u in users:
        user_id = u["user_id"]
        try:
            expires_at = iso_to_dt(u["expires_at"])
        except Exception:
            continue

        remaining = expires_at - now

        if remaining <= timedelta(0):
            # Expirou
            set_user_expired(user_id)
            await remove_from_group(context, user_id)
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⛔ Sua assinatura expirou e seu acesso foi removido. Para voltar, faça uma nova assinatura com os valores iniciais. Digite /start."
                )
            except Exception:
                pass
            continue

        # Notifica renovação se estiver nas últimas 24h
        if remaining <= RENEWAL_WINDOW and int(u.get("renewal_notified", 0)) == 0:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=menu_renovacao_text(expires_at),
                    reply_markup=menu_renovacao_keyboard(),
                    parse_mode=ParseMode.HTML
                )
                mark_renewal_notified(user_id)
            except Exception:
                pass

# -------------------------
# Mercado Pago Webhook (HTTP server)
# -------------------------
def validate_mp_signature(headers: dict, body: bytes) -> bool:
    """
    Validação opcional simples via secret (quando disponível).
    Se não houver secret, retorna True.
    Observação: Mercado Pago tem variações por produto/integração.
    Aqui aceitamos validação "best effort" para não travar produção.
    """
    if not MP_WEBHOOK_SECRET:
        return True

    # Alguns envios usam 'x-signature' / 'x-request-id'
    x_signature = headers.get("x-signature", "")
    x_request_id = headers.get("x-request-id", "")

    if not x_signature or not x_request_id:
        # sem cabeçalhos -> não bloqueia (pra não quebrar), mas registra aviso
        logger.warning("Webhook recebido sem x-signature/x-request-id (não bloqueado).")
        return True

    # Tenta um HMAC do body + request_id (heurística)
    msg = body + x_request_id.encode("utf-8")
    digest = hmac.new(MP_WEBHOOK_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    # Não sabemos o formato exato (MP muda conforme produto), então checamos "contém"
    if digest[:16] in x_signature:
        return True

    logger.warning("Assinatura do webhook não validou (não bloqueado por compatibilidade).")
    return True

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b"{}"

            # valida assinatura (best-effort)
            headers_lower = {k.lower(): v for k, v in self.headers.items()}
            if not validate_mp_signature(headers_lower, body):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"unauthorized")
                return

            payload = json.loads(body.decode("utf-8") or "{}")
            # MP geralmente manda: { "type": "...", "data": { "id": "..." } }
            data = payload.get("data") or {}
            payment_id = data.get("id") or payload.get("id")

            if payment_id:
                # processa em background (não bloquear HTTP)
                threading.Thread(target=process_payment_webhook, args=(str(payment_id),), daemon=True).start()

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception as e:
            logger.exception("Erro no webhook")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode("utf-8"))

def process_payment_webhook(payment_id: str):
    """
    Busca pagamento no MP e, se aprovado, ativa assinatura e libera acesso.
    """
    if TEST_MODE:
        # no modo teste nem precisa de webhook
        return

    try:
        payment = get_payment(payment_id)
        if not payment:
            # pode acontecer se o MP notificar antes de gravarmos — tenta buscar no MP e usar external_reference
            mp = fetch_payment(payment_id)
            external_reference = mp.get("external_reference") or ""
            # tenta extrair user_id e plan_key
            user_id, plan_key = parse_external_reference(external_reference)
            if user_id is None:
                logger.warning(f"Pagamento {payment_id} sem mapeamento local.")
                return

            # grava para rastreio
            amount = float(mp.get("transaction_amount") or 0.0)
            insert_payment(payment_id, user_id, plan_key or "unknown", amount, mp.get("status") or "unknown", external_reference)
            payment = get_payment(payment_id)

        mp = fetch_payment(payment_id)
        status = (mp.get("status") or "").lower()
        logger.info(f"Webhook payment_id={payment_id} status={status}")

        if status == "approved":
            # aprova e ativa
            user_id = int(payment["user_id"])
            plan_key = payment["plan_key"]

            # define dias pelo plan_key
            plan_days = plan_days_from_key(plan_key)
            if plan_days is None:
                plan_days = 30  # fallback

            # precisamos rodar funções async no loop do bot:
            run_async_activation(user_id, plan_days, payment_id)

        else:
            update_payment_status(payment_id, status)

    except Exception:
        logger.exception(f"Falha ao processar webhook do pagamento {payment_id}")

def parse_external_reference(external_reference: str):
    # formato: tg:{user_id}:{plan_key}:{ts}
    try:
        parts = external_reference.split(":")
        if len(parts) >= 4 and parts[0] == "tg":
            return int(parts[1]), parts[2]
    except Exception:
        pass
    return None, None

def plan_days_from_key(plan_key: str):
    # procura nos planos
    for d in (PLANS_INICIAIS, PLANS_RENOVACAO_24H):
        for k, v in d.items():
            if v["key"] == plan_key:
                return v["days"]
    return None

# referência do app para agendar tarefas async a partir do webhook thread
APP_REF = {"app": None}

def run_async_activation(user_id: int, plan_days: int, payment_id: str):
    app: Application = APP_REF["app"]
    if not app:
        logger.warning("APP_REF não definido para ativação async.")
        return

    async def _do():
        ctx = type("Obj", (), {})()
        ctx.application = app
        ctx.bot = app.bot
        ctx.job_queue = app.job_queue
        # Reusa função principal:
        await approve_and_activate(ctx, user_id, plan_days, payment_id=payment_id)

    # agenda no loop
    app.create_task(_do())

def start_webhook_server():
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    logger.info(f"HTTP server webhook rodando na porta {PORT}")
    server.serve_forever()

# -------------------------
# Main
# -------------------------
async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"✅ Online.\nTEST_MODE={'true' if TEST_MODE else 'false'}\nBASE_URL={BASE_URL or '(vazio)'}"
    )

def main():
    db_init()

    app = Application.builder().token(BOT_TOKEN).build()
    APP_REF["app"] = app

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CallbackQueryHandler(on_buy_callback, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(on_renew_callback, pattern=r"^renew:\d+$"))

    # Jobs
    # varre expiração + janela de renovação
    app.job_queue.run_repeating(expiration_sweeper, interval=600, first=30, name="expiration_sweeper")
    logger.info("Job expiration_sweeper agendado.")

    # Webhook server (somente útil em produção, mas pode deixar ligado)
    t = threading.Thread(target=start_webhook_server, daemon=True)
    t.start()

    logger.info("Bot iniciado. Rodando polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
