import os
import time
import json
import hashlib
import requests
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio

# ============ CONFIGURAÇÕES (vêm do Railway) ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET")

# ============ FUNÇÕES DA SHOPEE ============
def shopee_request(query: str):
    timestamp = str(int(time.time()))
    payload = json.dumps({"query": query}, separators=(",", ":"))
    signature = hashlib.sha256(
        (SHOPEE_APP_ID + timestamp + payload + SHOPEE_SECRET).encode()
    ).hexdigest()

    headers = {
        "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={signature}",
        "Content-Type": "application/json",
    }
    r = requests.post(
        "https://open-api.affiliate.shopee.com.br/graphql",
        data=payload,
        headers=headers,
        timeout=30,
    )
    return r.json()

def get_conversions(start_ts: int, end_ts: int, limit: int = 50):
    query = f"""
    query {{
      conversionReport(purchaseTimeStart: {start_ts}, purchaseTimeEnd: {end_ts}, limit: {limit}) {{
        nodes {{
          purchaseTime
          clickTime
          utmContent
          buyerType
          totalCommission
          netCommission
          sellerCommission
          orders {{
            orderId
            items {{
              itemId
              itemName
              qty
              actualAmount
              itemTotalCommission
            }}
          }}
        }}
        pageInfo {{
          hasNextPage
          scrollId
        }}
      }}
    }}
    """
    data = shopee_request(query)
    if "errors" in data:
        return None, data["errors"]
    nodes = data.get("data", {}).get("conversionReport", {}).get("nodes", [])
    return nodes, None

def format_money(value):
    try:
        return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "R$ 0,00"

def summarize(nodes):
    total_orders = 0
    total_commission = 0.0
    products = {}

    for node in nodes:
        total_commission += float(node.get("netCommission") or 0)
        for order in node.get("orders", []):
            total_orders += 1
            for item in order.get("items", []):
                name = item.get("itemName", "Produto")[:45]
                comm = float(item.get("itemTotalCommission") or 0)
                qty = int(item.get("qty") or 1)
                if name not in products:
                    products[name] = {"qty": 0, "commission": 0.0}
                products[name]["qty"] += qty
                products[name]["commission"] += comm

    top_products = sorted(products.items(), key=lambda x: x[1]["commission"], reverse=True)[:5]
    return total_orders, total_commission, top_products

def make_report(title: str, nodes, start_date: str, end_date: str):
    if nodes is None:
        return f"⚠️ Erro ao buscar dados da Shopee.\nTente novamente em alguns minutos."

    orders, commission, top = summarize(nodes)

    text = f"📊 *{title}*\n"
    text += f"📅 Período: {start_date} → {end_date}\n\n"
    text += f"🛒 Pedidos: *{orders}*\n"
    text += f"💰 Comissão estimada: *{format_money(commission)}*\n\n"

    if top:
        text += "🔥 *Top produtos:*\n"
        for i, (name, data) in enumerate(top, 1):
            text += f"{i}. {name}\n   → {data['qty']} un | {format_money(data['commission'])}\n"
    else:
        text += "Nenhum pedido neste período.\n"

    text += "\n⚠️ *Aviso importante:*\n"
    text += "Os dados da Shopee podem demorar algumas horas (ou até 1 dia) para atualizar completamente. Se aparecer zerado, pode ser atraso de sincronização — confira novamente mais tarde."

    return text

# ============ COMANDOS DO BOT ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Olá! Bot de métricas Shopee Afiliado ativo.\n\n"
        "Comandos disponíveis:\n"
        "/ontem - Resultado do dia anterior\n"
        "/mes - Do dia 1 até ontem\n"
        "/periodo 01/11 22/11 - Período personalizado\n"
        "/garimpo - Produtos com boa comissão\n"
        "/ajuda - Lista de comandos"
    )

async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def ontem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando dados de ontem... aguarde.")
    yesterday = datetime.now() - timedelta(days=1)
    start_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)

    nodes, err = get_conversions(int(start_dt.timestamp()), int(end_dt.timestamp()))
    if err:
        await update.message.reply_text(f"Erro na API: {err}")
        return

    text = make_report(
        "Relatório de Ontem",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando dados do mês... aguarde.")
    now = datetime.now()
    start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_dt = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

    nodes, err = get_conversions(int(start_dt.timestamp()), int(end_dt.timestamp()), limit=100)
    if err:
        await update.message.reply_text(f"Erro na API: {err}")
        return

    text = make_report(
        "Relatório do Mês (até ontem)",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def periodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Use: /periodo 01/11 22/11")
        return

    try:
        start_dt = datetime.strptime(context.args[0], "%d/%m")
        end_dt = datetime.strptime(context.args[1], "%d/%m")
        year = datetime.now().year
        start_dt = start_dt.replace(year=year, hour=0, minute=0, second=0)
        end_dt = end_dt.replace(year=year, hour=23, minute=59, second=59)
    except:
        await update.message.reply_text("Formato inválido. Use: /periodo 01/11 22/11")
        return

    await update.message.reply_text("Buscando período... aguarde.")
    nodes, err = get_conversions(int(start_dt.timestamp()), int(end_dt.timestamp()), limit=100)
    if err:
        await update.message.reply_text(f"Erro na API: {err}")
        return

    text = make_report(
        "Relatório Personalizado",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def garimpo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Garimpando produtos com boa comissão... aguarde.")

    query = """
    {
      productOfferV2(listType: 1, sortType: 5, page: 1, limit: 8) {
        nodes {
          productName
          commissionRate
          sellerCommissionRate
          priceMin
          sales
          shopName
          offerLink
        }
      }
    }
    """
    data = shopee_request(query)
    nodes = data.get("data", {}).get("productOfferV2", {}).get("nodes", [])

    if not nodes:
        await update.message.reply_text("Nenhum produto encontrado no momento.")
        return

    text = "🔍 *Garimpo - Produtos com boa comissão*\n\n"
    for i, p in enumerate(nodes, 1):
        rate = float(p.get("commissionRate") or 0) * 100
        price = p.get("priceMin", "0")
        sales = p.get("sales", 0)
        name = (p.get("productName") or "")[:50]
        shop = p.get("shopName", "")
        text += f"{i}. *{name}*\n"
        text += f"   💰 Comissão: {rate:.1f}% | Preço: R$ {price}\n"
        text += f"   📦 Vendas: {sales} | Loja: {shop}\n\n"

    text += "⚠️ Use o link oficial da Shopee para gerar seu link de afiliado."
    await update.message.reply_text(text, parse_mode="Markdown")

# ============ RELATÓRIO AUTOMÁTICO DIÁRIO ============
async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    yesterday = datetime.now() - timedelta(days=1)
    start_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)

    nodes, err = get_conversions(int(start_dt.timestamp()), int(end_dt.timestamp()))
    text = make_report(
        "Relatório Automático - Ontem",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

# ============ MAIN ============
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("ontem", ontem))
    app.add_handler(CommandHandler("mes", mes))
    app.add_handler(CommandHandler("periodo", periodo))
    app.add_handler(CommandHandler("garimpo", garimpo))

    # Relatório automático todos os dias às 10:00 (horário de Brasília aproximado)
    job_queue = app.job_queue
    job_queue.run_daily(daily_report, time=datetime.strptime("13:00", "%H:%M").time())  # 10h BRT = 13h UTC

    print("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
