
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
# 1. CONFIGURAÇÕES
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
# 2. CONFIGURAÇÕES DO GARIMPO V2
# =====================================================

GARIMPO_CONFIG = {
    "min_commission_rate": Decimal("0.08"),
    "min_sales": 100,
    "min_price": Decimal("50.00"),

    "limit": 30,
    "max_results": 15,

    "request_delay": 0.25,

    # Máximo de produtos da mesma categoria
    # durante a redistribuição das vagas.
    "fallback_category_limit": 5,
}

# Categorias, palavras-chave e vagas iniciais.
# A ordem também determina a prioridade de pesquisa.

GARIMPO_CATEGORIES = {
    "Casa e decoração": {
        "quota": 5,
        "keywords": [
            "decoracao",
            "quadros decorativos",
        ],
    },

    "Cozinha e utilidades": {
        "quota": 3,
        "keywords": [
            "pote hermetico",
            "fruteira",
        ],
    },

    "Iluminação": {
        "quota": 2,
        "keywords": [
            "luminaria",
            "abajur",
        ],
    },

    "Móveis e organização": {
        "quota": 2,
        "keywords": [
            "aparador",
            "organizador de casa",
        ],
    },

    "Geek e colecionáveis": {
        "quota": 1,
        "keywords": [
            "pelucia pokemon",
            "funko pop",
        ],
    },

    "Tecnologia e gadgets": {
        "quota": 1,
        "keywords": [
            "suporte celular",
            "fone bluetooth",
        ],
    },

    "Moda e vestuário": {
        "quota": 1,
        "keywords": [
            "camiseta oversized",
            "moletom",
        ],
    },
}

# Ordenações da Shopee:
# 2 = Mais vendidos
# 5 = Maior comissão

GARIMPO_SORT_TYPES = [2, 5]


# =====================================================
# 3. LOGS
# =====================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("LootZoneAnalytics")


# =====================================================
# 4. VALIDAÇÃO DAS CONFIGURAÇÕES
# =====================================================

def validate_config():

    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": CHAT_ID_RAW,
        "SHOPEE_APP_ID": SHOPEE_APP_ID,
        "SHOPEE_SECRET": SHOPEE_SECRET,
    }

    missing = [
        key
        for key, value in required.items()
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
            "TELEGRAM_CHAT_ID inválido."
        )


CHAT_ID = validate_config()


# =====================================================
# 5. FUNÇÕES AUXILIARES
# =====================================================

def to_decimal(value):

    try:
        return Decimal(str(value or "0"))

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):
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
        int(end_dt.timestamp()),
    )


# =====================================================
# 6. SEGURANÇA DO TELEGRAM
# =====================================================

def autorizado(update: Update):

    return (
        update.effective_chat is not None
        and update.effective_chat.id == CHAT_ID
    )


async def verificar_acesso(update: Update):

    if not autorizado(update):

        if update.effective_message:

            await update.effective_message.reply_text(
                "Acesso não autorizado."
            )

        return False

    return True


# =====================================================
# 7. CONEXÃO SHOPEE API
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
# 8. RELATÓRIOS DE CONVERSÃO
# =====================================================

def get_conversions(
    start_ts: int,
    end_ts: int,
    limit: int = 50,
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
                "Existem mais páginas no relatório "
                "de conversões. Paginação pendente."
            )

        return nodes, None

    except Exception as e:

        logger.exception(
            "Erro ao consultar conversões."
        )

        return None, str(e)


# =====================================================
# 9. RESUMO DE VENDAS
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
                    item.get("itemName")
                    or "Produto"
                )[:50]

                commission = float(
                    item.get("itemTotalCommission") or 0
                )

                qty = int(
                    item.get("qty") or 1
                )

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
# 10. FORMATAÇÃO DOS RELATÓRIOS
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
# 11. COMANDOS DO TELEGRAM
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
        "/garimpo — Garimpo inteligente V2\n\n"

        "⚙️ SISTEMA\n"
        "/status — Status do bot\n"
        "/ajuda — Lista de comandos"
    )


async def ajuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await start(update, context)


# =====================================================
# 12. COMANDO ONTEM
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

        await update.message.reply_text(
            f"❌ Erro na API:\n{err[:2500]}"
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
# 13. COMANDO MÊS
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

    if now.day == 1:

        await update.message.reply_text(
            "Ainda não existem dias anteriores "
            "neste mês para consultar."
        )

        return

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
            f"❌ Erro na API:\n{err[:2500]}"
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
# 14. COMANDO PERÍODO
# =====================================================

async def periodo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "Use:\n/periodo 01/11 22/11"
        )

        return

    try:

        now = datetime.now(BR_TZ)

        start_raw = datetime.strptime(
            context.args[0],
            "%d/%m",
        )

        end_raw = datetime.strptime(
            context.args[1],
            "%d/%m",
        )

        start_year = now.year
        end_year = now.year

        if start_raw.month > end_raw.month:
            end_year += 1

        start_dt = start_raw.replace(
            year=start_year,
            hour=0,
            minute=0,
            second=0,
        ).replace(tzinfo=BR_TZ)

        end_dt = end_raw.replace(
            year=end_year,
            hour=23,
            minute=59,
            second=59,
        ).replace(tzinfo=BR_TZ)

        if end_dt < start_dt:
            raise ValueError("Período inválido.")

        if end_dt > now:
            raise ValueError("Data futura.")

    except ValueError:

        await update.message.reply_text(
            "Data inválida.\n"
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
            f"❌ Erro na API:\n{err[:2500]}"
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
# 15. GARIMPO V2 — CONSULTA DE PRODUTOS
# =====================================================

def fetch_products(keyword, sort_type):

    safe_keyword = json.dumps(
        keyword,
        ensure_ascii=False,
    )

    query = f"""
    {{
      productOfferV2(
        keyword: {safe_keyword},
        listType: 0,
        sortType: {sort_type},
        page: 1,
        limit: {GARIMPO_CONFIG["limit"]}
      ) {{
        nodes {{
          itemId
          productName

          commission
          commissionRate
          sellerCommissionRate
          shopeeCommissionRate

          priceMin
          priceMax
          sales
          ratingStar

          shopName
          shopId

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

    return result.get("nodes", [])


# =====================================================
# 16. GARIMPO V2 — FILTROS COMERCIAIS
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

    min_rate = GARIMPO_CONFIG[
        "min_commission_rate"
    ]

    min_price = GARIMPO_CONFIG[
        "min_price"
    ]

    min_sales = GARIMPO_CONFIG[
        "min_sales"
    ]

    return (
        rate >= min_rate
        and price >= min_price
        and sales >= min_sales
    )


# =====================================================
# 17. GARIMPO V2 — CÁLCULO DA COMISSÃO
# =====================================================

def estimate_commission(product):

    api_commission = to_decimal(
        product.get("commission")
    )

    if api_commission > 0:

        return api_commission

    price = to_decimal(
        product.get("priceMin")
    )

    rate = to_decimal(
        product.get("commissionRate")
    )

    return price * rate


# =====================================================
# 18. GARIMPO V2 — DEDUPLICAÇÃO
# =====================================================

def collect_unique_products(raw_results):

    unique = {}

    for result in raw_results:

        product = result["product"]

        category = result["category"]

        item_id = str(
            product.get("itemId") or ""
        )

        if not item_id:
            continue

        if not filter_product(product):
            continue

        product["price_value"] = float(
            to_decimal(product.get("priceMin"))
        )

        product["sales_value"] = int(
            product.get("sales") or 0
        )

        product["rate_value"] = float(
            to_decimal(product.get("commissionRate"))
        )

        product["commission_value"] = float(
            estimate_commission(product)
        )

        product["rating_value"] = float(
            to_decimal(product.get("ratingStar"))
        )

        if item_id not in unique:

            product["_category"] = category

            product["_categories_found"] = [
                category
            ]

            unique[item_id] = product

        else:

            existing = unique[item_id]

            if category not in existing["_categories_found"]:

                existing["_categories_found"].append(
                    category
                )

            # Preserva o registro com maior comissão
            # quando o mesmo produto aparece novamente.

            if (
                product["commission_value"]
                > existing["commission_value"]
            ):

                product["_category"] = category

                product["_categories_found"] = (
                    existing["_categories_found"]
                )

                unique[item_id] = product

    return list(unique.values())


# =====================================================
# 19. GARIMPO V2 — PONTUAÇÃO
# =====================================================

def calculate_scores(products):

    if not products:
        return []

    max_sales = max(
        p["sales_value"]
        for p in products
    )

    max_commission = max(
        p["commission_value"]
        for p in products
    )

    for product in products:

        sales = product["sales_value"]

        commission = product["commission_value"]

        rating = product["rating_value"]

        # DEMANDA: 35%

        demand_score = (
            math.log1p(sales)
            / math.log1p(max_sales)
            * 100
            if max_sales > 0
            else 0
        )

        # COMISSÃO ABSOLUTA: 40%

        commission_score = (
            math.log1p(commission)
            / math.log1p(max_commission)
            * 100
            if max_commission > 0
            else 0
        )

        # AVALIAÇÃO: 25%
        # Se não existir avaliação, utiliza 50 pontos
        # como valor neutro, sem excluir o produto.

        if rating > 0:

            rating_score = (
                min(rating, 5) / 5
            ) * 100

        else:

            rating_score = 50

        score = (
            demand_score * 0.35
            + commission_score * 0.40
            + rating_score * 0.25
        )

        product["score"] = round(
            score,
            1,
        )

    return sorted(
        products,
        key=lambda p: (
            p["score"],
            p["commission_value"],
        ),
        reverse=True,
    )


# =====================================================
# 20. GARIMPO V2 — DISTRIBUIÇÃO DE RESULTADOS
# =====================================================

def select_diverse_products(products):

    selected = []

    selected_ids = set()

    category_counts = {}

    # PRIMEIRA ETAPA:
    # Preenche as vagas reservadas por categoria.

    for category, config in GARIMPO_CATEGORIES.items():

        quota = config["quota"]

        category_products = [
            p for p in products
            if p["_category"] == category
        ]

        for product in category_products:

            if len(selected) >= GARIMPO_CONFIG["max_results"]:
                break

            item_id = str(product["itemId"])

            if item_id in selected_ids:
                continue

            selected.append(product)

            selected_ids.add(item_id)

            category_counts[category] = (
                category_counts.get(category, 0) + 1
            )

            if category_counts[category] >= quota:
                break

    # SEGUNDA ETAPA:
    # Redistribui vagas não preenchidas.
    # Limita a concentração de uma categoria.

    for product in products:

        if len(selected) >= GARIMPO_CONFIG["max_results"]:
            break

        item_id = str(product["itemId"])

        if item_id in selected_ids:
            continue

        category = product["_category"]

        current_count = category_counts.get(
            category,
            0,
        )

        max_category = GARIMPO_CONFIG[
            "fallback_category_limit"
        ]

        if current_count >= max_category:
            continue

        selected.append(product)

        selected_ids.add(item_id)

        category_counts[category] = (
            current_count + 1
        )

    return selected


# =====================================================
# 21. GARIMPO V2 — PESQUISA COMPLETA
# =====================================================

def execute_garimpo():

    raw_results = []

    total_searches = 0

    errors = []

    logger.info(
        "Iniciando Garimpo Inteligente V2."
    )

    for category, config in GARIMPO_CATEGORIES.items():

        keywords = config["keywords"]

        for keyword in keywords:

            for sort_type in GARIMPO_SORT_TYPES:

                total_searches += 1

                try:

                    logger.info(
                        "Categoria: %s | "
                        "Palavra: %s | "
                        "Ordenação: %s",
                        category,
                        keyword,
                        sort_type,
                    )

                    products = fetch_products(
                        keyword,
                        sort_type,
                    )

                    for product in products:

                        raw_results.append({
                            "product": product,
                            "category": category,
                        })

                except Exception as e:

                    logger.exception(
                        "Erro na pesquisa %s",
                        keyword,
                    )

                    errors.append(
                        f"{keyword}: {str(e)[:120]}"
                    )

                time.sleep(
                    GARIMPO_CONFIG["request_delay"]
                )

    # REMOVE DUPLICADOS

    unique_products = collect_unique_products(
        raw_results
    )

    # CALCULA PONTUAÇÃO

    ranked_products = calculate_scores(
        unique_products
    )

    # DISTRIBUI OS RESULTADOS

    selected = select_diverse_products(
        ranked_products
    )

    logger.info(
        "Garimpo finalizado: "
        "%s pesquisas, %s resultados brutos, "
        "%s aprovados e %s selecionados.",
        total_searches,
        len(raw_results),
        len(unique_products),
        len(selected),
    )

    return {
        "products": selected,
        "total_raw": len(raw_results),
        "total_unique": len(unique_products),
        "total_searches": total_searches,
        "errors": errors,
        "category_counts": {
            category: sum(
                1 for p in selected
                if p["_category"] == category
            )
            for category in GARIMPO_CATEGORIES
        },
    }


# =====================================================
# 22. GARIMPO V2 — ENVIO AO TELEGRAM
# =====================================================

async def garimpo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    status_message = await update.message.reply_text(
        "🔎 LOOTZONE GARIMPO V2\n\n"
        "Iniciando pesquisa em 7 categorias...\n\n"
        "Analisando vendas, comissões, preços "
        "e avaliações.\n\n"
        "Aguarde."
    )

    try:

        result = await asyncio.to_thread(
            execute_garimpo
        )

    except Exception as e:

        logger.exception(
            "Erro geral no garimpo."
        )

        await status_message.edit_text(
            "❌ Falha ao executar o garimpo.\n"
            f"{str(e)[:1000]}"
        )

        return

    products = result["products"]

    if not products:

        await status_message.edit_text(
            "Nenhum produto passou pelos filtros.\n\n"
            f"Pesquisas: {result['total_searches']}\n"
            f"Resultados brutos: {result['total_raw']}\n"
            f"Produtos únicos aprovados: "
            f"{result['total_unique']}\n\n"
            "Consulte os logs do Railway "
            "caso alguma pesquisa tenha falhado."
        )

        return

    # RESUMO

    summary = (
        "✅ GARIMPO LOOTZONE V2 CONCLUÍDO\n\n"

        f"🔎 Pesquisas realizadas: "
        f"{result['total_searches']}\n"

        f"📦 Resultados brutos: "
        f"{result['total_raw']}\n"

        f"♻️ Produtos únicos aprovados: "
        f"{result['total_unique']}\n"

        f"🏆 Produtos selecionados: "
        f"{len(products)}\n\n"

        "FILTROS:\n"
        "• Comissão mínima: 8%\n"
        "• Vendas mínimas: 100\n"
        "• Preço mínimo: R$ 50\n\n"

        "DISTRIBUIÇÃO:\n"
    )

    for category, count in result[
        "category_counts"
    ].items():

        if count > 0:

            summary += (
                f"• {category}: {count}\n"
            )

    if result["errors"]:

        summary += (
            "\n⚠️ Algumas pesquisas falharam. "
            "Os resultados podem estar incompletos."
        )

    await status_message.edit_text(summary)

    # ENVIA OS PRODUTOS

    for index, product in enumerate(products, 1):

        name = (
            product.get("productName")
            or "Produto sem nome"
        )

        category = product["_category"]

        shop = (
            product.get("shopName")
            or "Não informada"
        )

        link = (
            product.get("offerLink")
            or ""
        )

        image = product.get("imageUrl")

        price = product["price_value"]

        rate = product["rate_value"] * 100

        commission = product["commission_value"]

        sales = product["sales_value"]

        rating = product["rating_value"]

        score = product["score"]

        caption = (
            f"#{index} — {name[:65]}\n\n"

            f"🏷️ Categoria: {category}\n"

            f"🏆 Pontuação: {score}/100\n\n"

            f"💰 Comissão: {rate:.2f}%\n"

            f"💵 Ganho estimado: "
            f"{format_money(commission)}\n"

            f"🏷️ Preço: {format_money(price)}\n"

            f"📦 Vendas informadas: {sales}\n"

            f"⭐ Avaliação: "
            f"{rating:.2f}/5"
            if rating > 0
            else
            f"#{index} — {name[:65]}\n\n"
            f"🏷️ Categoria: {category}\n"
            f"🏆 Pontuação: {score}/100\n\n"
            f"💰 Comissão: {rate:.2f}%\n"
            f"💵 Ganho estimado: {format_money(commission)}\n"
            f"🏷️ Preço: {format_money(price)}\n"
            f"📦 Vendas informadas: {sales}\n"
            "⭐ Avaliação: Não informada"
        )

        caption += (
            f"\n🏪 Loja: {shop[:35]}"
        )

        caption += (
            "\n\n⚠️ Concorrência entre afiliados "
            "não disponibilizada pela API."
        )

        # Envia foto sem ultrapassar o limite
        # de legenda do Telegram.

        if image:

            try:

                await context.bot.send_photo(
                    chat_id=CHAT_ID,
                    photo=image,
                    caption=caption[:1000],
                )

            except Exception:

                logger.exception(
                    "Falha ao enviar imagem do produto."
                )

                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=caption[:4000],
                )

        else:

            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=caption[:4000],
            )

        # Link separado para não exceder
        # o limite da legenda.

        if link:

            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🔗 Link de afiliado:\n{link}",
            )

        await asyncio.sleep(0.8)


# =====================================================
# 23. STATUS DO BOT
# =====================================================

async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await verificar_acesso(update):
        return

    await update.message.reply_text(
        "🟢 LOOTZONE ANALYTICS V2\n\n"

        "Bot: Online\n"
        "API: Configurada\n"
        "Fuso: Brasília\n\n"

        "GARIMPO:\n"
        "Categorias: 7\n"
        "Pesquisas por execução: 28\n"
        "Produtos por consulta: 30\n"
        "Resultados desejados: 15\n\n"

        "FILTROS:\n"
        f"Comissão: "
        f"{GARIMPO_CONFIG['min_commission_rate'] * 100}%\n"

        f"Vendas: "
        f"{GARIMPO_CONFIG['min_sales']}\n"

        f"Preço: "
        f"{format_money(GARIMPO_CONFIG['min_price'])}\n"
    )


# =====================================================
# 24. RELATÓRIO AUTOMÁTICO DIÁRIO
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

        report = (
            "⚠️ Não foi possível gerar "
            "o relatório automático.\n\n"
            f"{err[:1000]}"
        )

    else:

        report = make_report(
            "Relatório Automático — Ontem",
            nodes,
            start_dt.strftime("%d/%m/%Y"),
            end_dt.strftime("%d/%m/%Y"),
        )

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=report,
        parse_mode="Markdown",
    )


# =====================================================
# 25. TRATAMENTO DE ERROS
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
# 26. INICIALIZAÇÃO
# =====================================================

def main():

    logger.info(
        "Inicializando LootZone Analytics V2..."
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

    # RELATÓRIO AUTOMÁTICO ÀS 10H

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
        "Bot iniciado com sucesso. "
        "Relatório diário às 10h de Brasília."
    )

    app.run_polling()


if __name__ == "__main__":
    main()
