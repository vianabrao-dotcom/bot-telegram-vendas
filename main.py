import asyncio
from io import BytesIO
import os
import uuid
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import requests
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# =========================
# CONFIG / VARIÁVEIS ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()

# Se quiser mandar link do grupo após pagamento aprovado (opcional):
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK", "").strip()

# Se quiser que o MP notifique seu bot quando o pagamento for aprovado:
# Ex: https://SEUAPP.up.railway.app/mp/webhook
MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "").strip()

# Porta HTTP do Railway (pra webhook)
PORT = int(os.getenv("PORT", "8080"))

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
# "BANCO" simples em JSON
# =========================
DB_FILE = "db.json"
DB_LOCK = threading.Lock()

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def db_load() -> dict:
    with DB_LOCK:
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"users": {}, "payments": {}}

def db_save(db: dict) -> None:
    with DB_LOCK:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)

def get_user(db: dict, user_id: int) -> dict:
    u = db["users"].get(str(user_id))
    if not u:
        u = {
            "user_id": user_id,
            "chat_id": None,
            "active": False,
            "plan_code": None,
            "expires_at": None,              # ISO datetime UTC quando expira
            "renewal_offer_until": None,     # ISO datetime UTC (janela 24h)
            "last_payment_id": None,
        }
        db["users"][str(user_id)] = u
    return u

# =========================
# PLANOS / MENUS
# =========================
# MENU INICIAL (valores originais)
PLANS_INITIAL = {
    "1": {"name": "Plano Semanal", "amount": 19.90, "days": 7},
    "2": {"name": "Plano Mensal",  "amount": 29.90, "days": 30},
    "3": {"name": "Plano Anual",   "amount": 39.90, "days": 365},
    "4": {"name": "Plano Anual Promocional", "amount": 29.99, "days": 365},
}

# MENU RENOVAÇÃO (só quando faltam 24h)
PLANS_RENEWAL = {
    "1": {"name": "Plano Semanal (Renovação)", "amount": 10.90, "days": 7},
    "2": {"name": "Plano Mensal (Renovação)",  "amount": 15.90, "days": 30},
    "3": {"name": "Plano Anual (Renovação)",   "amount": 19.90, "days": 365},
}

WELCOME_TEXT = (
    "🔥 *Bem-vindo!*\n\n"
    "Escolha abaixo o plano ideal e entre imediatamente no grupo privado:\n\n"
    "1️⃣ Plano Semanal — *R$19,90*\n"
    "2️⃣ Plano Mensal — *R$29,90*\n"
    "3️⃣ Plano Anual — *R$39,90*\n\n"
    "4️⃣ 🎁 *Plano Anual Promocional* — *R$29,99*\n\n"
    "Digite apenas o número do plano desejado."
)

def renewal_menu_text(offer_until_iso: str) -> str:
    return (
        "🎁 *MENU EXCLUSIVO DE RENOVAÇÃO (válido por 24 horas)*\n\n"
        "🔥 Oferta liberada por 24 horas:\n\n"
        "1️⃣ Plano Semanal — *R$10,90*\n"
        "2️⃣ Plano Mensal — *R$15,90*\n"
        "3️⃣ Plano Anual — *R$19,90*\n\n"
        "⏳ Esses valores expiram em 24 horas."
    )

# =========================
# MERCADO PAGO: criar PIX
# =========================
def gerar_pix(amount: float, description: str, payer_email: str, payer_first_name: str, payer_last_name: str, external_reference: str):
    url = "https://api.mercadopago.com/v1/payments"
    idempotency_key = str(uuid.uuid4())

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": idempotency_key,
    }

    payload = {
        "transaction_amount": float(amount),
        "description": description,
        "payment_method_id": "pix",
        "external_reference": external_reference,  # pra você rastrear no webhook
        "payer": {
            "email": payer_email,
            "first_name": payer_first_name or "Cliente",
            "last_name": payer_last_name or "VIP",
        },
    }

    # configura o webhook DURANTE a criação do pagamento (mais simples do que painel)
    if MP_WEBHOOK_URL:
        payload["notification_url"] = MP_WEBHOOK_URL

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
    except Exception as e:
        return 0, {"error": str(e)}

def mp_get_payment(payment_id: str) -> dict:
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=30)
    return resp.json()

def extrair_pix(mp_response: dict):
    poi = mp_response.get("point_of_interaction", {}) if isinstance(mp_response, dict) else {}
    tx = poi.get("transaction_data", {}) if isinstance(poi, dict) else {}

    qr_code = tx.get("qr_code")
    ticket_url = tx.get("ticket_url")
    return qr_code, ticket_url

# =========================
# TELEGRAM
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")

def _user_in_renewal_window(u: dict) -> bool:
    until = u.get("renewal_offer_until")
    if not until:
        return False
    try:
        until_dt = datetime.fromisoformat(until)
        return _now_utc() <= until_dt
    except Exception:
        return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.lower() in ("/start", "start"):
        await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")
        return

    db = db_load()
    user = update.effective_user
    u = get_user(db, user.id)
    u["chat_id"] = update.effective_chat.id

    # escolhe qual tabela de planos vale
    if _user_in_renewal_window(u):
        plans = PLANS_RENEWAL
    else:
        plans = PLANS_INITIAL

    if text not in plans:
        # se estiver na janela de renovação, mostra o menu de renovação; senão o inicial
        if _user_in_renewal_window(u):
            await update.message.reply_text(renewal_menu_text(u["renewal_offer_until"]), parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Opção inválida.\n\n" + WELCOME_TEXT, parse_mode="Markdown")
        db_save(db)
        return

    plan = plans[text]
    plan_code = text
    amount = plan["amount"]
    days = plan["days"]
    plan_name = plan["name"]

    # email válido e “neutro” (evita problema do telegram.local)
    payer_email = f"user{user.id}@example.com"

    await update.message.reply_text("⏳ Gerando seu PIX...")

    external_reference = f"tg:{user.id}|plan:{plan_code}|renew:{'1' if plans is PLANS_RENEWAL else '0'}"
    description = f"{plan_name} - Prime VIP"

    status, payment = gerar_pix(
        amount=amount,
        description=description,
        payer_email=payer_email,
        payer_first_name=user.first_name or "Cliente",
        payer_last_name=user.last_name or "VIP",
        external_reference=external_reference,
    )

    if status not in (200, 201) or not isinstance(payment, dict):
        await update.message.reply_text(
            "❌ *Erro ao gerar Pix.*\n\n"
            f"Status: `{status}`\n"
            f"Resposta: `{str(payment)[:3500]}`",
            parse_mode="Markdown",
        )
        return

    payment_id = str(payment.get("id", ""))
    u["last_payment_id"] = payment_id
    db["payments"][payment_id] = {
        "payment_id": payment_id,
        "user_id": user.id,
        "chat_id": update.effective_chat.id,
        "plan_name": plan_name,
        "plan_days": days,
        "amount": amount,
        "created_at": _now_utc().isoformat(),
        "status": payment.get("status"),
    }
    db_save(db)

    qr_code, ticket_url = extrair_pix(payment)

    if not qr_code:
        await update.message.reply_text(
            "⚠️ O pagamento foi criado, mas o Mercado Pago não retornou o *copia e cola*.\n\n"
            "Abra pelo link abaixo e finalize por lá:\n"
            f"{ticket_url or 'Sem link disponível no retorno.'}",
            parse_mode="Markdown",
        )
        return

    # 1) Mensagem bonita (resumo)
    msg = (
        "✅ *PIX GERADO COM SUCESSO!*\n\n"
        f"📦 Plano: *{plan_name}*\n"
        f"💰 Valor: *R${amount:.2f}*\n\n"
        "📋 *Copia e cola (enviei também em .txt para facilitar copiar):*\n"
        "(veja a próxima mensagem)\n\n"
    )
    if ticket_url:
        msg += f"🔗 Link do QR: {ticket_url}\n\n"
    msg += "⏳ Após pagar, aguarde a confirmação automática."
    await update.message.reply_text(msg, parse_mode="Markdown")

    # 2) Mensagem só com o código (mais fácil copiar)
    await update.message.reply_text(qr_code)

    # 3) Arquivo TXT com o código (dribla limitações de cópia em alguns chats)
    bio = BytesIO(qr_code.encode("utf-8"))
    bio.name = "pix_copia_e_cola.txt"
    await update.message.reply_document(InputFile(bio), caption="📎 Pix Copia e Cola (arquivo)")

# =========================
# WEBHOOK HTTP (std lib)
# =========================
class MPWebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        # MP manda formatos diferentes dependendo do produto/evento
        # Normalmente vem: {"type":"payment","data":{"id":"123"}} ou algo parecido
        payment_id = None
        try:
            if isinstance(payload, dict):
                if isinstance(payload.get("data"), dict) and payload["data"].get("id"):
                    payment_id = str(payload["data"]["id"])
                elif payload.get("id"):
                    payment_id = str(payload["id"])
        except Exception:
            payment_id = None

        # responde rápido pro MP
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

        if not payment_id:
            logger.warning(f"Webhook recebido sem payment_id: {payload}")
            return

        try:
            payment_full = mp_get_payment(payment_id)
            status = payment_full.get("status")
            logger.info(f"Webhook payment_id={payment_id} status={status}")

            db = db_load()
            p = db["payments"].get(str(payment_id))
            if not p:
                # se não achou no db, tenta pelo external_reference
                ext = payment_full.get("external_reference", "")
                logger.warning(f"Pagamento {payment_id} não encontrado no db. ext_ref={ext}")
                return

            # atualiza status
            p["status"] = status
            db["payments"][str(payment_id)] = p

            # Se aprovado: ativa assinatura e grava expires_at a partir de AGORA
            if status == "approved":
                user_id = int(p["user_id"])
                u = get_user(db, user_id)

                expires_at = (_now_utc() + timedelta(days=int(p["plan_days"]))).isoformat()
                u["active"] = True
                u["plan_code"] = p["plan_name"]
                u["expires_at"] = expires_at
                u["renewal_offer_until"] = None

                db["users"][str(user_id)] = u
                db_save(db)

                # avisa no telegram
                chat_id = p.get("chat_id")
                if chat_id:
                    try:
                        # usamos o bot via requests do Telegram API pra não depender do context aqui
                        tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                        text = (
                            "✅ *Pagamento aprovado!*\n\n"
                            f"📦 Plano: *{p['plan_name']}*\n"
                            f"⏳ Válido até: *{expires_at.replace('T',' ').replace('+00:00',' UTC')}*\n\n"
                        )
                        if GROUP_INVITE_LINK:
                            text += f"🔗 Entre no grupo: {GROUP_INVITE_LINK}\n"
                        requests.post(tg_url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15)
                    except Exception as e:
                        logger.error(f"Falha ao avisar Telegram approved: {e}")

        except Exception as e:
            logger.error(f"Erro processando webhook: {e}")

def start_http_server():
    server = HTTPServer(("0.0.0.0", PORT), MPWebhookHandler)
    logger.info(f"HTTP server webhook rodando na porta {PORT}")
    server.serve_forever()

# =========================
# SWEEPER: expiração/renovação
# =========================
async def expiration_sweeper(context: ContextTypes.DEFAULT_TYPE):
    db = db_load()
    now = _now_utc()
    changed = False

    for uid_str, u in list(db["users"].items()):
        expires = u.get("expires_at")
        if not expires:
            continue
        try:
            exp_dt = datetime.fromisoformat(expires)
        except Exception:
            continue

        # Se faltam <= 24h e ainda não abriu janela de renovação, abre
        if exp_dt > now and exp_dt - now <= timedelta(hours=24):
            if not u.get("renewal_offer_until"):
                u["renewal_offer_until"] = (now + timedelta(hours=24)).isoformat()
                changed = True

                # manda menu de renovação
                chat_id = u.get("chat_id")
                if chat_id:
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=renewal_menu_text(u["renewal_offer_until"]),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.error(f"Falha ao enviar menu renovação: {e}")

        # Se expirou: desativa (a remoção do grupo você pode implementar depois, se quiser)
        if exp_dt <= now:
            u["active"] = False
            u["plan_code"] = None
            u["expires_at"] = None
            u["renewal_offer_until"] = None
            changed = True

            chat_id = u.get("chat_id")
            if chat_id:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⛔ Sua assinatura expirou. Para voltar, assine novamente pelo menu inicial: /start",
                    )
                except Exception as e:
                    logger.error(f"Falha ao avisar expiração: {e}")

        db["users"][uid_str] = u

    if changed:
        db_save(db)
        logger.info("expiration_sweeper: atualizações salvas.")

# =========================
# MAIN
# =========================
def main():
    # sobe servidor HTTP do webhook (não usa aiohttp)
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # roda sweeper a cada 10 minutos (ajuste se quiser)
    app.job_queue.run_repeating(expiration_sweeper, interval=600, first=20)

    logger.info("Bot iniciado. Rodando polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
