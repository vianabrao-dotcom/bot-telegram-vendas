import os
import json
import time
import uuid
import logging
import sqlite3
import threading
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------
# Config / Env
# ----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_PAYER_EMAIL_PADRAO = os.getenv("MP_PAYER_EMAIL_PADRAO", "").strip()

TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()  # ex: -1001234567890
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK", "").strip()  # ex: https://t.me/+xxxxx

ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "").strip()  # ex: "123456789"
TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() in ("1", "true", "yes", "on")

SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()  # ex: https://t.me/seuuser?text=...

MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "").strip()        # ex: https://seuapp.up.railway.app/mp/webhook?token=SECRETO
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()  # "SECRETO"

PORT = int(os.getenv("PORT", "8080"))

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("primevip")

# ----------------------------
# Planos / Regras
# ----------------------------
PLANS_INITIAL = {
    "weekly": {"label": "Plano Semanal", "amount": 19.90, "duration_days": 7},
    "monthly": {"label": "Plano Mensal", "amount": 29.90, "duration_days": 30},
    "annual": {"label": "Plano Anual", "amount": 39.90, "duration_days": 365},
    "annual_promo": {"label": "Plano Anual Promocional", "amount": 29.99, "duration_days": 365},
}

PLANS_RENEWAL = {
    "weekly": {"label": "Plano Semanal", "amount": 10.90, "duration_days": 7},
    "monthly": {"label": "Plano Mensal", "amount": 15.90, "duration_days": 30},
    "annual": {"label": "Plano Anual", "amount": 19.90, "duration_days": 365},
}

RENEWAL_WINDOW_SECONDS = 24 * 60 * 60

# ----------------------------
# DB (SQLite)
# ----------------------------
DB_PATH = os.getenv("DB_PATH", "primevip.sqlite3").strip()

def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _column_exists(cur, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return col in cols

def db_init():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,                 -- new|pending|active|expired
            plan_key TEXT,
            payment_id TEXT,
            payment_status TEXT,                  -- pending|approved|...
            amount REAL,
            created_at INTEGER,
            paid_at INTEGER,
            expires_at INTEGER,
            last_interaction_at INTEGER,
            pix_copia_cola TEXT,
            pix_ticket_url TEXT
        )
        """
    )
    conn.commit()

    if not _column_exists(cur, "users", "pix_copia_cola"):
        cur.execute("ALTER TABLE users ADD COLUMN pix_copia_cola TEXT")
    if not _column_exists(cur, "users", "pix_ticket_url"):
        cur.execute("ALTER TABLE users ADD COLUMN pix_ticket_url TEXT")
    conn.commit()
    conn.close()

def upsert_user(telegram_id: int, **fields):
    conn = db_conn()
    cur = conn.cursor()

    now = int(time.time())
    fields.setdefault("last_interaction_at", now)

    cur.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,))
    exists = cur.fetchone() is not None

    if not exists:
        base = {
            "telegram_id": telegram_id,
            "status": "new",
            "plan_key": None,
            "payment_id": None,
            "payment_status": None,
            "amount": None,
            "created_at": now,
            "paid_at": None,
            "expires_at": None,
            "last_interaction_at": now,
            "pix_copia_cola": None,
            "pix_ticket_url": None,
        }
        base.update(fields)
        cols = ", ".join(base.keys())
        qs = ", ".join(["?"] * len(base))
        cur.execute(f"INSERT INTO users ({cols}) VALUES ({qs})", tuple(base.values()))
    else:
        sets = ", ".join([f"{k}=?" for k in fields.keys()])
        cur.execute(f"UPDATE users SET {sets} WHERE telegram_id = ?", (*fields.values(), telegram_id))

    conn.commit()
    conn.close()

def get_user(telegram_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_by_payment_id(payment_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE payment_id = ?", (payment_id,))
    row = cur.fetchone()
    conn.close()
    return row

def set_active(telegram_id: int, plan_key: str, duration_days: int):
    now = int(time.time())
    expires = now + duration_days * 24 * 60 * 60
    upsert_user(
        telegram_id,
        status="active",
        paid_at=now,
        expires_at=expires,
        payment_status="approved",
    )

def clear_pending(telegram_id: int):
    upsert_user(
        telegram_id,
        status="new",
        plan_key=None,
        payment_id=None,
        payment_status=None,
        amount=None,
        pix_copia_cola=None,
        pix_ticket_url=None,
    )

# ----------------------------
# Helpers
# ----------------------------
def human_time_left(seconds: int) -> str:
    if seconds <= 0:
        return "expirado"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}min"
    return f"{minutes}min"

def within_renewal_window(user_row) -> bool:
    if not user_row:
        return False
    if user_row["status"] != "active":
        return False
    expires_at = user_row["expires_at"]
    if not expires_at:
        return False
    now = int(time.time())
    remaining = expires_at - now
    return 0 < remaining <= RENEWAL_WINDOW_SECONDS

async def safe_remove_from_group(context: ContextTypes.DEFAULT_TYPE, telegram_id: int):
    if not TELEGRAM_GROUP_ID:
        return
    try:
        await context.bot.ban_chat_member(chat_id=int(TELEGRAM_GROUP_ID), user_id=telegram_id)
        await context.bot.unban_chat_member(chat_id=int(TELEGRAM_GROUP_ID), user_id=telegram_id)
    except Exception as e:
        logger.warning(f"Falha ao remover do grupo: {e}")

async def enforce_expiration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    tid = update.effective_user.id
    user = get_user(tid)
    if not user:
        return

    if user["status"] == "active" and user["expires_at"]:
        now = int(time.time())
        if user["expires_at"] <= now:
            upsert_user(tid, status="expired")
            await safe_remove_from_group(context, tid)
            try:
                await context.bot.send_message(
                    chat_id=tid,
                    text=(
                        "⛔ Sua assinatura expirou e o acesso foi removido.\n\n"
                        "Para voltar, faça uma nova assinatura com os valores iniciais usando /start."
                    ),
                )
            except Exception:
                pass

def pix_action_keyboard(payment_id: str, ticket_url: str) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("📋 Copiar Pix", callback_data=f"pixcopy:{payment_id}"),
        InlineKeyboardButton("✅ Já paguei", callback_data=f"pixcheck:{payment_id}"),
    ]
    row2 = []
    if ticket_url:
        row2.append(InlineKeyboardButton("🌐 Abrir QR Code", url=ticket_url))
    if SUPPORT_URL:
        row2.append(InlineKeyboardButton("🆘 Suporte", url=SUPPORT_URL))

    kb = [row1]
    if row2:
        kb.append(row2)
    return InlineKeyboardMarkup(kb)

# ----------------------------
# Menus
# ----------------------------
def menu_initial_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("1️⃣ Plano Semanal — R$19,90", callback_data="buy:weekly")],
        [InlineKeyboardButton("2️⃣ Plano Mensal — R$29,90", callback_data="buy:monthly")],
        [InlineKeyboardButton("3️⃣ Plano Anual — R$39,90", callback_data="buy:annual")],
        [InlineKeyboardButton("4️⃣ 🎁 Plano Anual Promocional — R$29,99", callback_data="buy:annual_promo")],
    ]
    return InlineKeyboardMarkup(kb)

def menu_renewal_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("1️⃣ Renovar Semanal — R$10,90", callback_data="renew:weekly")],
        [InlineKeyboardButton("2️⃣ Renovar Mensal — R$15,90", callback_data="renew:monthly")],
        [InlineKeyboardButton("3️⃣ Renovar Anual — R$19,90", callback_data="renew:annual")],
    ]
    return InlineKeyboardMarkup(kb)

def build_welcome_text_initial() -> str:
    return (
        "🔥 *Bem-vindo!* 🔥\n\n"
        "Escolha abaixo o plano ideal e entre imediatamente no grupo privado:\n"
        "_(clique na opção desejada)_"
    )

def build_welcome_text_renewal(user_row) -> str:
    now = int(time.time())
    remaining = max(0, int(user_row["expires_at"] or 0) - now)
    return (
        "🎁 *MENU EXCLUSIVO DE RENOVAÇÃO* (válido por 24 horas)\n\n"
        f"⏳ Tempo restante do seu acesso: *{human_time_left(remaining)}*\n\n"
        "Escolha um plano com desconto para renovar agora:\n"
        "_(clique na opção desejada)_\n\n"
        "⚠️ Se não renovar até o prazo acabar, o acesso será removido."
    )

# ----------------------------
# Mercado Pago
# ----------------------------
def mp_headers():
    return {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

def mp_create_pix_payment(amount: float, description: str, payer_email: str):
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    payload = {
        "transaction_amount": float(amount),
        "description": description,
        "payment_method_id": "pix",
        "payer": {"email": payer_email},
    }

    headers = mp_headers()
    headers["X-Idempotency-Key"] = str(uuid.uuid4())

    r = requests.post(
        "https://api.mercadopago.com/v1/payments",
        headers=headers,
        json=payload,   # <- melhor que data=json.dumps
        timeout=20,
    )

    # Se der erro, imprime a resposta do Mercado Pago (isso é o ouro)
    if r.status_code >= 400:
        raise RuntimeError(f"MP {r.status_code}: {r.text}")

    data = r.json()

    pid = str(data.get("id"))
    poi = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    copia_cola = poi.get("qr_code")
    ticket_url = poi.get("ticket_url")

    return {"id": pid, "copia_cola": copia_cola, "ticket_url": ticket_url, "raw": data}


def mp_get_payment(payment_id: str):
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")
    r = requests.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers=mp_headers(),
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

# ----------------------------
# TEST MODE
# ----------------------------
def test_generate_pix_like(amount: float, description: str):
    fake_payment_id = f"TEST-{uuid.uuid4().hex[:10]}"
    copia = (
        "00020126"
        "580014br.gov.bcb.pix"
        "0136" + uuid.uuid4().hex[:36]
        + "52040000"
        + "5303986"
        + f"5405{amount:.2f}"
        + "5802BR"
        + "5910SAAB123214"
        + "6009Sao Paulo"
        + "621405"
        + "21mpqprinter"
        + uuid.uuid4().hex[:12]
    )
    ticket_url = "https://www.mercadopago.com.br/"
    return {"id": fake_payment_id, "copia_cola": copia, "ticket_url": ticket_url, "raw": {"test": True, "description": description}}

# ----------------------------
# Pós aprovação
# ----------------------------
async def grant_access(context: ContextTypes.DEFAULT_TYPE, tid: int):
    if GROUP_INVITE_LINK:
        await context.bot.send_message(
            chat_id=tid,
            text=f"✅ Pagamento confirmado!\n\n🔗 Acesse o grupo privado: {GROUP_INVITE_LINK}",
            disable_web_page_preview=True,
        )
    else:
        await context.bot.send_message(
            chat_id=tid,
            text="✅ Pagamento confirmado! Porém o link do grupo não está configurado. Fale com o suporte.",
        )

# ----------------------------
# Telegram Handlers
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enforce_expiration(update, context)

    user = update.effective_user
    if not user:
        return

    tid = user.id
    existing = get_user(tid)
    upsert_user(tid, status=existing["status"] if existing else "new")

    row = get_user(tid)

    if within_renewal_window(row):
        await update.message.reply_text(
            build_welcome_text_renewal(row),
            reply_markup=menu_renewal_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return

    if row and row["status"] == "active" and row["expires_at"]:
        now = int(time.time())
        remaining = max(0, int(row["expires_at"]) - now)
        text = (
            "✅ Sua assinatura está ativa.\n\n"
            f"⏳ Tempo restante: *{human_time_left(remaining)}*\n\n"
        )
        if GROUP_INVITE_LINK:
            text += f"🔗 Acesse o grupo privado: {GROUP_INVITE_LINK}\n"
        else:
            text += "⚠️ O link do grupo ainda não foi configurado.\n"
        text += "\nQuando faltar 24h, o menu de renovação com desconto aparecerá aqui no /start."
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    await update.message.reply_text(
        build_welcome_text_initial(),
        reply_markup=menu_initial_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

async def handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enforce_expiration(update, context)

    query = update.callback_query
    if not query:
        return
    await query.answer()

    tid = query.from_user.id
    data = query.data or ""
    mode, key = data.split(":", 1)

    row = get_user(tid)

    if mode == "renew":
        if not within_renewal_window(row):
            await query.edit_message_text("⚠️ Renovação só nas últimas 24 horas. Use /start.")
            return
        plan = PLANS_RENEWAL.get(key)
        if not plan:
            await query.edit_message_text("Opção inválida. Use /start.")
            return
        plan_key = key
        description = f"Renovação - {plan['label']}"
    else:
        plan = PLANS_INITIAL.get(key)
        if not plan:
            await query.edit_message_text("Opção inválida. Use /start.")
            return
        plan_key = key
        description = f"Assinatura - {plan['label']}"

    amount = float(plan["amount"])
    label = plan["label"]

    # Sem parse_mode aqui para nunca quebrar
    await query.edit_message_text(f"⏳ Gerando seu PIX...\n\nPlano: {label}\nValor: R${amount:.2f}".replace(".", ","))

    payer_email = MP_PAYER_EMAIL_PADRAO or "cliente@example.com"

    try:
        if TEST_MODE:
            payment = test_generate_pix_like(amount, description)
        else:
            payment = mp_create_pix_payment(amount, description, payer_email)

        payment_id = str(payment["id"])
        copia_cola = (payment.get("copia_cola") or "").strip()
        ticket_url = (payment.get("ticket_url") or "").strip()

        upsert_user(
            tid,
            status="pending",
            plan_key=plan_key,
            payment_id=payment_id,
            payment_status="pending",
            amount=amount,
            pix_copia_cola=copia_cola,
            pix_ticket_url=ticket_url,
        )

        # Mensagem em HTML (não quebra com link/underscore)
        msg = (
            "✅ <b>PIX GERADO COM SUCESSO!</b>\n\n"
            f"Plano: <b>{html.escape(label)}</b>\n"
            f"Valor: <b>R${amount:.2f}</b>\n\n"
            "📋 <b>Copia e cola:</b>\n"
            f"<code>{html.escape(copia_cola)}</code>\n\n"
        )
        if ticket_url:
            msg += f"🔗 <b>QR Code:</b> {html.escape(ticket_url)}\n\n"

        msg += "⬇️ Use os botões abaixo para copiar ou confirmar o pagamento."

        if TEST_MODE:
            msg += "\n\n🧪 <b>MODO TESTE ATIVO</b>\nPara aprovar sem pagar, envie: <code>/aprovar_teste</code>"

        await context.bot.send_message(
            chat_id=tid,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=pix_action_keyboard(payment_id, ticket_url),
        )

   except Exception as e:
    logger.exception("Erro ao gerar PIX")
    clear_pending(tid)

    # Mostra o erro real pro usuário (depois que estiver OK, você pode voltar pro modo “limpo”)
    await context.bot.send_message(
        chat_id=tid,
        text=(
            "❌ Não consegui gerar o PIX agora.\n\n"
            "Detalhe técnico (para suporte):\n"
            f"{str(e)}"
        ),
    )

    # Se você tiver ADMIN_TELEGRAM_ID, também manda pra você
    if ADMIN_TELEGRAM_ID:
        try:
            await context.bot.send_message(
                chat_id=int(ADMIN_TELEGRAM_ID),
                text=f"⚠️ Erro ao gerar PIX para {tid}:\n{str(e)}",
            )
        except Exception:
            pass

        )

async def pix_copy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    _, payment_id = (query.data or "").split(":", 1)
    row = get_user_by_payment_id(payment_id)

    if not row or not row["pix_copia_cola"]:
        await query.message.reply_text("❌ Não encontrei esse PIX. Gere um novo com /start.")
        return

    pix = row["pix_copia_cola"].strip()
    await query.message.reply_text(
        "📋 <b>Pix Copia e Cola</b>\n"
        "<i>(toque e segure para copiar)</i>\n\n"
        f"<code>{html.escape(pix)}</code>",
        parse_mode=ParseMode.HTML,
    )

async def pix_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer("Verificando...")

    _, payment_id = (query.data or "").split(":", 1)
    row = get_user_by_payment_id(payment_id)
    if not row:
        await query.message.reply_text("❌ Não encontrei esse pagamento. Gere um novo com /start.")
        return

    tid = int(row["telegram_id"])

    if TEST_MODE:
        await query.message.reply_text("🧪 Modo teste: para aprovar, envie /aprovar_teste.")
        return

    try:
        mp = mp_get_payment(payment_id)
        status = (mp.get("status") or "").lower()
    except Exception:
        await query.message.reply_text("⚠️ Não consegui consultar agora. Tente novamente em instantes.")
        return

    if status == "approved":
        plan_key = row["plan_key"]
        if plan_key in PLANS_INITIAL:
            duration_days = PLANS_INITIAL[plan_key]["duration_days"]
            label = PLANS_INITIAL[plan_key]["label"]
        elif plan_key in PLANS_RENEWAL:
            duration_days = PLANS_RENEWAL[plan_key]["duration_days"]
            label = PLANS_RENEWAL[plan_key]["label"]
        else:
            duration_days = 7
            label = "Plano"

        set_active(tid, plan_key, duration_days)
        await safe_remove_from_group(context, tid)
        await grant_access(context, tid)

        await query.message.reply_text(f"✅ Pagamento confirmado!\nPlano: {label}\n\nAcesso liberado ✅")
        return

    if status in ("pending", "in_process"):
        await query.message.reply_text(
            "⏳ Ainda não recebi a confirmação do pagamento.\n"
            "Se você acabou de pagar, aguarde 1–3 minutos e toque em ✅ Já paguei novamente."
        )
        return

    upsert_user(tid, payment_status=status)
    await query.message.reply_text(f"❌ Pagamento não aprovado (status: {status}). Gere um novo PIX com /start.")

async def aprovar_teste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enforce_expiration(update, context)

    user = update.effective_user
    if not user:
        return
    tid = user.id

    if not TEST_MODE:
        await update.message.reply_text("⚠️ O /aprovar_teste só funciona quando TEST_MODE=true.")
        return

    row = get_user(tid)
    if not row or row["status"] != "pending":
        await update.message.reply_text("⚠️ Não encontrei pagamento pendente. Use /start e gere um PIX primeiro.")
        return

    plan_key = row["plan_key"]
    if plan_key in PLANS_INITIAL:
        duration_days = PLANS_INITIAL[plan_key]["duration_days"]
        label = PLANS_INITIAL[plan_key]["label"]
    elif plan_key in PLANS_RENEWAL:
        duration_days = PLANS_RENEWAL[plan_key]["duration_days"]
        label = PLANS_RENEWAL[plan_key]["label"]
    else:
        await update.message.reply_text("❌ Plano inválido. Use /start novamente.")
        clear_pending(tid)
        return

    set_active(tid, plan_key, duration_days)
    await safe_remove_from_group(context, tid)
    await grant_access(context, tid)

    await update.message.reply_text(
        f"✅ PAGAMENTO APROVADO (TESTE)\nPlano: {label}\nValidade: {duration_days} dias\n\nAcesso liberado ✅"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enforce_expiration(update, context)

    user = update.effective_user
    if not user:
        return
    tid = user.id

    row = get_user(tid)
    if not row:
        await update.message.reply_text("Sem cadastro ainda. Use /start.")
        return

    now = int(time.time())
    expires = row["expires_at"] or 0
    remaining = expires - now if expires else 0

    await update.message.reply_text(
        f"Status: {row['status']}\n"
        f"Plano: {row['plan_key']}\n"
        f"Pagamento: {row['payment_status']}\n"
        f"Expira em: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires)) if expires else '-'}\n"
        f"Tempo restante: {human_time_left(remaining) if expires else '-'}"
    )

# ----------------------------
# Webhook Mercado Pago (opcional)
# ----------------------------
class MPWebhookHandler(BaseHTTPRequestHandler):
    def _send(self, code=200, body="ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path != "/mp/webhook":
                return self._send(404, "not found")

            qs = parse_qs(parsed.query)
            token = (qs.get("token", [""])[0] or "").strip()
            if MP_WEBHOOK_SECRET and token != MP_WEBHOOK_SECRET:
                return self._send(401, "unauthorized")

            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            payload = json.loads(raw or "{}")

            ptype = payload.get("type")
            data = payload.get("data") or {}
            mp_id = data.get("id")

            if ptype != "payment" or not mp_id:
                return self._send(200, "ignored")

            payment = mp_get_payment(str(mp_id))
            status = (payment.get("status") or "").lower()

            row = get_user_by_payment_id(str(mp_id))
            if not row:
                return self._send(200, "payment not mapped")

            tid = int(row["telegram_id"])
            plan_key = row["plan_key"]

            if status == "approved":
                if plan_key in PLANS_INITIAL:
                    duration_days = PLANS_INITIAL[plan_key]["duration_days"]
                elif plan_key in PLANS_RENEWAL:
                    duration_days = PLANS_RENEWAL[plan_key]["duration_days"]
                else:
                    duration_days = 7

                set_active(tid, plan_key, duration_days)
                upsert_user(tid, payment_status="approved")
            else:
                upsert_user(tid, payment_status=status)

            return self._send(200, "ok")

        except Exception:
            logger.exception("Erro no webhook MP")
            return self._send(500, "error")

def start_webhook_server():
    if not MP_WEBHOOK_URL:
        logger.info("MP_WEBHOOK_URL não configurada — webhook não será iniciado.")
        return

    server = HTTPServer(("0.0.0.0", PORT), MPWebhookHandler)

    def run():
        logger.info(f"Webhook server rodando em 0.0.0.0:{PORT} (path /mp/webhook)")
        server.serve_forever()

    th = threading.Thread(target=run, daemon=True)
    th.start()

# ----------------------------
# Fallback
# ----------------------------
async def any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await enforce_expiration(update, context)
    if update.message:
        await update.message.reply_text("Use /start para ver os planos e gerar o PIX.")

# ----------------------------
# Main
# ----------------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN não configurado.")

    db_init()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("aprovar_teste", aprovar_teste))
    application.add_handler(CommandHandler("status", status))

    application.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^(buy|renew):"))
    application.add_handler(CallbackQueryHandler(pix_copy_callback, pattern=r"^pixcopy:"))
    application.add_handler(CallbackQueryHandler(pix_check_callback, pattern=r"^pixcheck:"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))

    if MP_WEBHOOK_URL:
        if MP_WEBHOOK_SECRET:
            logger.info("Webhook protegido por token. Garanta que a URL do MP tenha ?token=SEU_SEGREDO")
        else:
            logger.warning("Webhook sem token (MP_WEBHOOK_SECRET vazio). Recomendo configurar.")
        start_webhook_server()

    logger.info(f"Bot iniciado. TEST_MODE={TEST_MODE}")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
