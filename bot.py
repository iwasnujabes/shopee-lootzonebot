
import os
import time
import json
import math
import hashlib
import asyncio
import logging
import requests

from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =====================================================
# CONFIGURAÇÕES
# =====================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID")

SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET")

SHOPEE_URL = (
    "https://open-api.affiliate.shopee.com.br/graphql"
)

BR_TZ = ZoneInfo("America/Sao_Paulo")

# =====================================================
# CONFIGURAÇÃO DO GARIMPO
# =====================================================

GARIMPO_CONFIG = {
    "min_commission_rate": Decimal("0.08"),
    "min_sales": 100,
    "min_price": Decimal("50.00"),
    "limit": 30,
    "max_pages": 2,
    "max_results": 8,
    "request_delay": 0.3,
}

GARIMPO_KEYWORDS = [
    "pelucia",
    "organizador",
    "luminaria",
    "decoracao",
    "gamer",
    "cozinha",
    "cadeira",
]

# =====================================================
# LOGS
# =====================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("LootZoneBot")


# =====================================================
# VALIDAÇÃO DE CONFIGURAÇÕES
# =====================================================

def validate_config():

    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": CHAT_ID_RAW,
        "SHOPEE_APP_ID": SHOPEE_APP_ID,
        "SHOPEE_SECRET": SHOPEE_SECRET,
    }

    missing = [
        name
        for name, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Variáveis ausentes no Railway: "
            + ", ".join(missing)
        )

    try:
        return int(CHAT_ID_RAW)
    except ValueError:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID precisa ser numérico."
        )


CHAT_ID = validate_config()


# =====================================================
# SEGURANÇA
# =====================================================

def autorizado(update: Update):

    if not update.effective_chat:
        return False

    return update.effective_chat.id == CHAT_ID


async def verificar_acesso(update: Update):

    if not autorizado(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Acesso não autorizado."
            )

        return False

    return True


# =====================================================
# FUNÇÕES AUXILIARES
# =====================================================

def to_decimal(value):

    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def format_money(value):

    try:
        number = float(value)

        return (
            f"R$ {number:,.2f}"
            .replace(",", "X")
            .replace(".", ",")
            .replace("X", ".")
        )

    except (ValueError, TypeError):
        return "R$ 0,00"


def get_period_start_end(start_dt, end_dt):

    return (
        int(start_dt.timestamp()),
        int(end_dt.timestamp())
    )


# =====================================================
# SHOPEE API
# =====================================================

def shopee_request(query: str):

    timestamp = str(int(time.time()))

    payload = json.dumps(
        {"query": query},
        separators=(",", ":"),
        ensure_ascii=False,
    )

    signature = hashlib.sha256(
        (
            SHOPEE_APP_ID
            + timestamp
            + payload
            + SHOPEE_SECRET
        ).encode("utf-8")
    ).hexdigest()

    headers = {
        "Authorization": (
            f"SHA256 Credential={SHOPEE_APP_ID}, "
            f"Timestamp={timestamp}, "
            f"Signature={signature}"
        ),
        "Content-Type": "application/json",
    }

    response = requests.post(
        SHOPEE_URL,
        data=payload.encode("utf-8"),
        headers=headers,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    if data.get("errors"):
        raise RuntimeError(
            f"Erro GraphQL: {data['errors']}"
        )

    return data


# =====================================================
# RELATÓRIOS DE CONVERSÃO
# =====================================================

def get_conversions(
    start_ts: int,
    end_ts: int,
    limit: int = 50
):

    query = f"""
    query {{
      conversionReport(
        purchaseTimeStart: {start_ts},
        purchaseTimeEnd: {end_ts},
        limit: {limit}
      ) {{
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

    try:

        data = shopee_request(query)

        report = (
            data.get("data", {})
            .get("conversionReport", {})
        )

        nodes = report.get("nodes", [])

        page_info = report.get("pageInfo", {})

        if page_info.get("hasNextPage"):
            logger.warning(
                "Relatório possui próxima página. "
                "A paginação ainda precisa ser implementada."
            )

        return nodes, None

    except Exception as e:

        logger.exception(
            "Falha ao buscar conversões."
        )

        return None, str(e)


# =====================================================
# RESUMO DE CONVERSÕES
# =====================================================

def summarize(nodes):

    total_orders = 0
    total_commission = 0.0

    products = {}

    for node in nodes:

        total_commission += float(
            node.get("netCommission") or 0
        )

        for order in node.get("orders", []):

            total_orders += 1

            for item in order.get("items", []):

                name = (
                    item.get("itemName") or "Produto"
                )[:50]

                commission = float(
                    item.get("itemTotalCommission") or 0
                )

                qty = int(item.get("qty") or 1)

                if name not in products:

                    products[name] = {
                        "qty": 0,
                        "commission": 0.0,
                    }

                products[name]["qty"] += qty

                products[name]["commission"] += commission

    top_products = sorted(
        products.items(),
        key=lambda x: x[1]["commission"],
        reverse=True,
    )[:5]

    return (
        total_orders,
        total_commission,
        top_products,
    )


# =====================================================
# MONTAGEM DE RELATÓRIOS
# =====================================================

def make_report(
    title,
    nodes,
    start_date,
    end_date,
):

    if nodes is None:

        return (
            "⚠️ Erro ao buscar dados da Shopee.\n"
            "Tente novamente em alguns minutos."
        )

    orders, commission, top = summarize(nodes)

    text = f"📊 *{title}*\n"

    text += (
        f"📅 Período: `{start_date}` "
        f"→ `{end_date}`\n\n"
    )

    text += f"🛒 Pedidos: *{orders}*\n"

    text += (
        f"💰 Comissão registrada: "
        f"*{format_money(commission)}*\n\n"
    )

    if top:

        text += "🔥 *Top produtos do período:*\n"

        for i, (name, data) in enumerate(top, 1):

            text += f"{i}. {name}\n"

            text += (
                f"   └ {data['qty']} un | "
                f"{format_money(data['commission'])}\n"
            )

    else:

        text += (
            "Nenhum pedido registrado neste período.\n"
        )

    text += "\n——————————————\n"

    text += (
        "⚠️ Os dados da Shopee podem demorar "
        "algumas horas ou até 1 dia para atualizar."
    )

    return text


# =====================================================
# COMANDO START
# =====================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    await update.message.reply_text(
        "Olá! Bem-vindo ao LootZone Analytics.\n\n"

        "📊 RELATÓRIOS\n"
        "/ontem — Resultado de ontem\n"
        "/mes — Resultado do mês\n"
        "/periodo 01/11 22/11 — Período personalizado\n\n"

        "🔎 GARIMPO\n"
        "/garimpo — Garimpo inteligente\n\n"

        "⚙️ SISTEMA\n"
        "/status — Status do bot\n"
        "/ajuda — Lista de comandos",
    )


async def ajuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await start(update, context)


# =====================================================
# COMANDO ONTEM
# =====================================================

async def ontem(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    await update.message.reply_text(
        "🔎 Buscando dados de ontem..."
    )

    yesterday = datetime.now(BR_TZ) - timedelta(days=1)

    start_dt = yesterday.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    end_dt = yesterday.replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=0,
    )

    start_ts, end_ts = get_period_start_end(
        start_dt,
        end_dt,
    )

    nodes, err = await asyncio.to_thread(
        get_conversions,
        start_ts,
        end_ts,
    )

    if err:

        await update.message.reply_text(
            f"❌ Erro na API:\n{err[:3000]}"
        )

        return

    report = make_report(
        "Relatório de Ontem",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )

    await update.message.reply_text(
        report,
        parse_mode="Markdown",
    )


# =====================================================
# COMANDO MÊS
# =====================================================

async def mes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    await update.message.reply_text(
        "🔎 Buscando dados do mês..."
    )

    now = datetime.now(BR_TZ)

    start_dt = now.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    end_dt = (now - timedelta(days=1)).replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=0,
    )

    if now.day == 1:

        await update.message.reply_text(
            "Ainda não há dias anteriores "
            "neste mês para consultar."
        )

        return

    start_ts, end_ts = get_period_start_end(
        start_dt,
        end_dt,
    )

    nodes, err = await asyncio.to_thread(
        get_conversions,
        start_ts,
        end_ts,
        100,
    )

    if err:

        await update.message.reply_text(
            f"❌ Erro na API:\n{err[:3000]}"
        )

        return

    report = make_report(
        "Relatório do Mês (até ontem)",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )

    await update.message.reply_text(
        report,
        parse_mode="Markdown",
    )


# =====================================================
# COMANDO PERÍODO PERSONALIZADO
# =====================================================

async def periodo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "Use o formato:\n"
            "/periodo 01/11 22/11"
        )

        return

    try:

        now = datetime.now(BR_TZ)

        start_dt = datetime.strptime(
            context.args[0],
            "%d/%m",
        )

        end_dt = datetime.strptime(
            context.args[1],
            "%d/%m",
        )

        start_year = now.year
        end_year = now.year

        if start_dt.month > end_dt.month:
            end_year += 1

        start_dt = start_dt.replace(
            year=start_year,
            hour=0,
            minute=0,
            second=0,
        ).replace(tzinfo=BR_TZ)

        end_dt = end_dt.replace(
            year=end_year,
            hour=23,
            minute=59,
            second=59,
        ).replace(tzinfo=BR_TZ)

        if end_dt < start_dt:
            raise ValueError("Período invertido.")

        if end_dt > now:
            raise ValueError("Data futura.")

    except ValueError:

        await update.message.reply_text(
            "Data inválida ou período incorreto.\n\n"
            "Exemplo: /periodo 01/11 22/11"
        )

        return

    await update.message.reply_text(
        "🔎 Buscando período solicitado..."
    )

    start_ts, end_ts = get_period_start_end(
        start_dt,
        end_dt,
    )

    nodes, err = await asyncio.to_thread(
        get_conversions,
        start_ts,
        end_ts,
        100,
    )

    if err:

        await update.message.reply_text(
            f"❌ Erro na API:\n{err[:3000]}"
        )

        return

    report = make_report(
        "Relatório Personalizado",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )

    await update.message.reply_text(
        report,
        parse_mode="Markdown",
    )


# =====================================================
# GARIMPO INTELIGENTE - CONSULTA DE PRODUTOS
# =====================================================

def fetch_products(keyword, sort_type):

    all_products = []

    safe_keyword = json.dumps(
        keyword,
        ensure_ascii=False,
    )

    for page in range(
        1,
        GARIMPO_CONFIG["max_pages"] + 1,
    ):

        query = f"""
        {{
          productOfferV2(
            keyword: {safe_keyword},
            listType: 0,
            sortType: {sort_type},
            page: {page},
            limit: {GARIMPO_CONFIG["limit"]}
          ) {{
            nodes {{
              itemId
              productName
              commissionRate
              sellerCommissionRate
              shopeeCommissionRate
              commission
              priceMin
              priceMax
              priceDiscountRate
              sales
              ratingStar
              shopName
              shopId
              shopType
              offerLink
              imageUrl
            }}
            pageInfo {{
              page
              limit
              hasNextPage
            }}
          }}
        }}
        """

        data = shopee_request(query)

        result = (
            data.get("data", {})
            .get("productOfferV2", {})
        )

        nodes = result.get("nodes", [])

        page_info = result.get("pageInfo", {})

        all_products.extend(nodes)

        if not page_info.get("hasNextPage"):
            break

        time.sleep(
            GARIMPO_CONFIG["request_delay"]
        )

    return all_products


# =====================================================
# GARIMPO - FILTROS
# =====================================================

def filter_product(product):

    rate = to_decimal(
        product.get("commissionRate")
    )

    price = to_decimal(
        product.get("priceMin")
    )

    sales = int(
        product.get("sales") or 0
    )

    return (
        rate >= GARIMPO_CONFIG["min_commission_rate"]
        and price >= GARIMPO_CONFIG["min_price"]
        and sales >= GARIMPO_CONFIG["min_sales"]
    )


# =====================================================
# GARIMPO - PREPARAÇÃO DOS PRODUTOS
# =====================================================

def prepare_garimpo(products):

    unique = {}

    for product in products:

        item_id = str(
            product.get("itemId") or ""
        )

        if not item_id:
            continue

        if not filter_product(product):
            continue

        price = to_decimal(
            product.get("priceMin")
        )

        rate = to_decimal(
            product.get("commissionRate")
        )

        api_commission = to_decimal(
            product.get("commission")
        )

        # Prioriza a comissão informada pela API.
        # Se não estiver disponível, calcula uma estimativa.

        estimated = (
            api_commission
            if api_commission > 0
            else price * rate
        )

        product["price_value"] = float(price)

        product["estimated_rate"] = float(rate)

        product["estimated_commission"] = float(
            estimated
        )

        product["sales_value"] = int(
            product.get("sales") or 0
        )

        previous = unique.get(item_id)

        if previous is None:

            unique[item_id] = product

        else:

            if (
                product["estimated_commission"]
                > previous["estimated_commission"]
            ):

                unique[item_id] = product

    candidates = list(unique.values())

    if not candidates:
        return []

    max_sales = max(
        p["sales_value"]
        for p in candidates
    )

    max_commission = max(
        p["estimated_commission"]
        for p in candidates
    )

    for product in candidates:

        sales_score = (
            math.log1p(product["sales_value"])
            / math.log1p(max_sales)
            * 100
            if max_sales > 0
            else 0
        )

        commission_score = (
            product["estimated_commission"]
            / max_commission
            * 100
            if max_commission > 0
            else 0
        )

        rating = float(
            to_decimal(
                product.get("ratingStar")
            )
        )

        rating_score = min(
            max(rating, 0),
            5
        ) / 5 * 100

        score = (
            sales_score * 0.40
            + commission_score * 0.40
            + rating_score * 0.20
        )

        product["score"] = round(
            score,
            1,
        )

    return sorted(
        candidates,
        key=lambda p: (
            p["score"],
            p["estimated_commission"],
        ),
        reverse=True,
    )


# =====================================================
# GARIMPO - EXECUÇÃO DA PESQUISA
# =====================================================

def execute_garimpo():

    all_products = []

    total_searches = 0

    errors = []

    for keyword in GARIMPO_KEYWORDS:

        for sort_type in (2, 5):

            try:

                logger.info(
                    "Garimpo: %s | ordenação %s",
                    keyword,
                    sort_type,
                )

                products = fetch_products(
                    keyword,
                    sort_type,
                )

                all_products.extend(products)

                total_searches += 1

            except Exception as e:

                logger.exception(
                    "Falha na pesquisa: %s",
                    keyword,
                )

                errors.append(
                    f"{keyword}: {str(e)[:150]}"
                )

            time.sleep(
                GARIMPO_CONFIG["request_delay"]
            )

    candidates = prepare_garimpo(
        all_products
    )

    return {
        "products": candidates,
        "total_raw": len(all_products),
        "total_approved": len(candidates),
        "total_searches": total_searches,
        "errors": errors,
    }


# =====================================================
# GARIMPO - ENVIO DOS RESULTADOS
# =====================================================

async def garimpo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    status = await update.message.reply_text(
        "🔎 GARIMPO INTELIGENTE LOOTZONE\n\n"
        "Iniciando pesquisa automática...\n"
        "Analisando demanda, comissão e avaliações."
    )

    try:

        result = await asyncio.to_thread(
            execute_garimpo
        )

    except Exception as e:

        logger.exception(
            "Erro geral no garimpo."
        )

        await status.edit_text(
            "❌ Erro ao executar o garimpo.\n"
            f"{str(e)[:1000]}"
        )

        return

    candidates = result["products"]

    if not candidates:

        message = (
            "Nenhum produto passou pelos filtros.\n\n"
            f"Pesquisas realizadas: "
            f"{result['total_searches']}\n"
            f"Produtos analisados: "
            f"{result['total_raw']}\n\n"
            "Tente novamente mais tarde."
        )

        if result["errors"]:
            message += (
                "\n\nAlgumas pesquisas falharam. "
                "Consulte os logs do Railway."
            )

        await status.edit_text(message)

        return

    selected = candidates[
        :GARIMPO_CONFIG["max_results"]
    ]

    summary = (
        "✅ GARIMPO CONCLUÍDO\n\n"
        f"Pesquisas realizadas: "
        f"{result['total_searches']}\n"
        f"Produtos encontrados: "
        f"{result['total_raw']}\n"
        f"Aprovados nos filtros: "
        f"{result['total_approved']}\n"
        f"Selecionados: {len(selected)}\n\n"
        "FILTROS:\n"
        "• Comissão mínima: 8%\n"
        "• Vendas mínimas: 100\n"
        "• Preço mínimo: R$ 50,00\n\n"
        "Classificação: demanda, rentabilidade "
        "e avaliação."
    )

    if result["errors"]:

        summary += (
            "\n\n⚠️ Algumas pesquisas falharam. "
            "O resultado pode estar incompleto."
        )

    await status.edit_text(summary)

    for i, product in enumerate(selected, 1):

        name = (
            product.get("productName")
            or "Produto sem nome"
        )

        shop = (
            product.get("shopName")
            or "Não informada"
        )

        link = (
            product.get("offerLink")
            or ""
        )

        image = (
            product.get("imageUrl")
        )

        price = product["price_value"]

        rate = (
            product["estimated_rate"] * 100
        )

        commission = (
            product["estimated_commission"]
        )

        sales = product["sales_value"]

        rating = to_decimal(
            product.get("ratingStar")
        )

        seller_rate = (
            to_decimal(
                product.get("sellerCommissionRate")
            ) * 100
        )

        shopee_rate = (
            to_decimal(
                product.get("shopeeCommissionRate")
            ) * 100
        )

        score = product["score"]

        discount = to_decimal(
            product.get("priceDiscountRate")
        )

        caption = (
            f"#{i} | {name[:65]}\n\n"

            f"🏆 Pontuação: {score}/100\n\n"

            f"💰 Comissão total: {rate:.2f}%\n"
            f"   ├ Vendedor: {seller_rate:.2f}%\n"
            f"   └ Shopee: {shopee_rate:.2f}%\n\n"

            f"💵 Ganho estimado: "
            f"{format_money(commission)}\n"

            f"🏷️ Preço: {format_money(price)}\n"
            f"📦 Vendas informadas: {sales}\n"
            f"⭐ Avaliação: {rating:.2f}/5\n"
            f"📉 Desconto informado: {discount:.0f}%\n"
            f"🏪 Loja: {shop[:40]}\n\n"

            "📊 Concorrência: não mensurada.\n"
            "A API não informa quantidade de afiliados."
        )

        if link:
            caption += (
                f"\n\n🔗 Link de afiliado:\n{link}"
            )

        try:

            if image:

                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=image,
                    caption=caption[:1024],
                )

            else:

                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=caption[:4000],
                )

        except Exception:

            logger.exception(
                "Falha ao enviar produto %s",
                product.get("itemId"),
            )

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=caption[:4000],
            )

        await asyncio.sleep(0.9)


# =====================================================
# STATUS DO BOT
# =====================================================

async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    await update.message.reply_text(
        "🟢 LOOTZONE ANALYTICS\n\n"
        "Bot: Online\n"
        "API: Configurada\n"
        "Fuso: America/Sao_Paulo\n\n"
        "GARIMPO:\n"
        f"Comissão mínima: "
        f"{GARIMPO_CONFIG['min_commission_rate'] * 100}%\n"
        f"Vendas mínimas: "
        f"{GARIMPO_CONFIG['min_sales']}\n"
        f"Preço mínimo: "
        f"{format_money(GARIMPO_CONFIG['min_price'])}\n\n"
        "Para testar a API, execute /garimpo."
    )


# =====================================================
# RELATÓRIO AUTOMÁTICO DIÁRIO
# =====================================================

async def daily_report(
    context: ContextTypes.DEFAULT_TYPE,
):

    yesterday = (
        datetime.now(BR_TZ)
        - timedelta(days=1)
    )

    start_dt = yesterday.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    end_dt = yesterday.replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=0,
    )

    start_ts, end_ts = get_period_start_end(
        start_dt,
        end_dt,
    )

    nodes, err = await asyncio.to_thread(
        get_conversions,
        start_ts,
        end_ts,
    )

    if err:

        text = (
            "⚠️ Não foi possível gerar "
            "o relatório automático.\n\n"
            f"Erro: {err[:1000]}"
        )

    else:

        text = make_report(
            "Relatório Automático — Ontem",
            nodes,
            start_dt.strftime("%d/%m/%Y"),
            end_dt.strftime("%d/%m/%Y"),
        )

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode="Markdown",
    )


# =====================================================
# TRATAMENTO GLOBAL DE ERROS
# =====================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Erro não tratado:",
        exc_info=context.error,
    )


# =====================================================
# MAIN
# =====================================================

def main():

    logger.info(
        "Iniciando LootZone Analytics..."
    )

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # COMANDOS

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("ajuda", ajuda)
    )

    app.add_handler(
        CommandHandler("ontem", ontem)
    )

    app.add_handler(
        CommandHandler("mes", mes)
    )

    app.add_handler(
        CommandHandler("periodo", periodo)
    )

    app.add_handler(
        CommandHandler("garimpo", garimpo)
    )

    app.add_handler(
        CommandHandler("status", status)
    )

    # ERROS

    app.add_error_handler(
        error_handler
    )

    # RELATÓRIO DIÁRIO ÀS 10H DE BRASÍLIA

    app.job_queue.run_daily(
        daily_report,
        time=dt_time(
            hour=10,
            minute=0,
            tzinfo=BR_TZ,
        ),
        name="relatorio_diario",
    )

    logger.info(
        "Bot iniciado. Agendamento: 10h Brasília."
    )

    app.run_polling()


if __name__ == "__main__":
    main()
