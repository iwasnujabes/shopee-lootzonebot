import os
import time
import json
import hashlib
import requests
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
                name = item.get("itemName", "Produto")[:50]
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
        return "⚠️ Erro ao buscar dados da Shopee.\nTente novamente em alguns minutos."

    orders, commission, top = summarize(nodes)

    text = f"📊 *{title}*\n"
    text += f"📅 Período: `{start_date}` → `{end_date}`\n\n"
    text += f"🛒 Pedidos: *{orders}*\n"
    text += f"💰 Comissão estimada: *{format_money(commission)}*\n\n"

    if top:
        text += "🔥 *Top produtos do período:*\n"
        for i, (name, data) in enumerate(top, 1):
            text += f"{i}. {name}\n"
            text += f"   └ {data['qty']} un  |  {format_money(data['commission'])}\n"
    else:
        text += "Nenhum pedido registrado neste período.\n"

    text += "\n——————————————\n"
    text += "⚠️ *Aviso:* Os dados da Shopee podem demorar algumas horas (às vezes até 1 dia) para atualizar por completo. Se estiver zerado, confira novamente mais tarde."

    return text

# ============ COMANDOS DO BOT ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Olá! Bot de métricas e garimpo Shopee Afiliado.\n\n"
        "📌 *Comandos disponíveis:*\n\n"
        "/ontem — Resultado do dia anterior\n"
        "/mes — Do dia 1 até ontem\n"
        "/periodo 01/11 22/11 — Período personalizado\n"
        "/garimpo — Produtos com boa comissão + foto\n"
        "/ajuda — Lista de comandos",
        parse_mode="Markdown"
    )

async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def ontem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Buscando dados de ontem...")
    yesterday = datetime.now() - timedelta(days=1)
    start_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)

    nodes, err = get_conversions(int(start_dt.timestamp()), int(end_dt.timestamp()))
    if err:
        await update.message.reply_text(f"❌ Erro na API:\n{err}")
        return

    text = make_report(
        "Relatório de Ontem",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Buscando dados do mês...")
    now = datetime.now()
    start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_dt = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

    nodes, err = get_conversions(int(start_dt.timestamp()), int(end_dt.timestamp()), limit=100)
    if err:
        await update.message.reply_text(f"❌ Erro na API:\n{err}")
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
        await update.message.reply_text("Use o formato:\n`/periodo 01/11 22/11`", parse_mode="Markdown")
        return

    try:
        start_dt = datetime.strptime(context.args[0], "%d/%m")
        end_dt = datetime.strptime(context.args[1], "%d/%m")
        year = datetime.now().year
        start_dt = start_dt.replace(year=year, hour=0, minute=0, second=0)
        end_dt = end_dt.replace(year=year, hour=23, minute=59, second=59)
    except:
        await update.message.reply_text("Formato inválido. Use:\n`/periodo 01/11 22/11`", parse_mode="Markdown")
        return

    await update.message.reply_text("🔎 Buscando período solicitado...")
    nodes, err = get_conversions(int(start_dt.timestamp()), int(end_dt.timestamp()), limit=100)
    if err:
        await update.message.reply_text(f"❌ Erro na API:\n{err}")
        return

    text = make_report(
        "Relatório Personalizado",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def garimpo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Garimpando produtos com boa comissão...\nAguarde alguns segundos.")

    query = """
    {
      productOfferV2(keyword: "kit", listType: 0, sortType: 5, page: 1, limit: 8) {
        nodes {
          itemId
          productName
          commissionRate
          sellerCommissionRate
          shopeeCommissionRate
          priceMin
          sales
          shopName
          offerLink
          imageUrl
        }
      }
    }
    """
    data = shopee_request(query)
    nodes = data.get("data", {}).get("productOfferV2", {}).get("nodes", [])

    if not nodes:
        query2 = """
        {
          productOfferV2(keyword: "pelucia", sortType: 5, page: 1, limit: 8) {
            nodes {
              itemId
              productName
              commissionRate
              sellerCommissionRate
              shopeeCommissionRate
              priceMin
              sales
              shopName
              offerLink
              imageUrl
            }
          }
        }
        """
        data = shopee_request(query2)
        nodes = data.get("data", {}).get("productOfferV2", {}).get("nodes", [])

    if not nodes:
        await update.message.reply_text("Nenhum produto encontrado no momento.\nTente novamente mais tarde.")
        return

    await update.message.reply_text(f"✅ Encontrei *{len(nodes)}* produtos interessantes:\n", parse_mode="Markdown")

    for i, p in enumerate(nodes, 1):
        rate = float(p.get("commissionRate") or 0) * 100
        seller_rate = float(p.get("sellerCommissionRate") or 0) * 100
        shopee_rate = float(p.get("shopeeCommissionRate") or 0) * 100
        price = p.get("priceMin", "0")
        sales = p.get("sales", 0)
        name = p.get("productName") or "Produto sem nome"
        shop = p.get("shopName", "-")
        link = p.get("offerLink") or ""
        image = p.get("imageUrl")
        item_id = p.get("itemId", "")

        caption = (
            f"*{i}. {name[:70]}*\n\n"
            f"💰 *Comissão total:* {rate:.1f}%\n"
            f"   ├ Seller: {seller_rate:.1f}%\n"
            f"   └ Shopee: {shopee_rate:.1f}%\n\n"
            f"💵 Preço: R$ {price}\n"
            f"📦 Vendas: {sales}\n"
            f"🏪 Loja: {shop}\n"
            f"🆔 ID: `{item_id}`\n"
        )

        if link:
            caption += f"\n🔗 [Abrir Link de Afiliado]({link})"

        try:
            if image:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=image,
                    caption=caption,
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(caption, parse_mode="Markdown", disable_web_page_preview=False)
        except Exception:
            await update.message.reply_text(caption, parse_mode="Markdown")

        await asyncio.sleep(0.9)

# ============ RELATÓRIO AUTOMÁTICO DIÁRIO ============
async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    yesterday = datetime.now() - timedelta(days=1)
    start_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)

    nodes, err = get_conversions(int(start_dt.timestamp()), int(end_dt.timestamp()))
    text = make_report(
        "Relatório Automático — Ontem",
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

    # Relatório automático todos os dias às 10h (horário de Brasília)
    job_queue = app.job_queue
    job_queue.run_daily(daily_report, time=datetime.strptime("13:00", "%H:%M").time())

    print("Bot iniciado com sucesso...")
    app.run_polling()

if __name__ == "__main__":
    main()
