import os
import html
import time
import json
import math
import hashlib
import asyncio
import logging
import requests

from pathlib import Path
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

SHOPEE_URL = "https://open-api.affiliate.shopee.com.br/graphql"
BR_TZ = ZoneInfo("America/Sao_Paulo")

SEEN_FILE = Path("seen_items.json")
SEEN_TTL_DAYS = 7

# =====================================================
# 2. CONFIGURAÇÕES DO GARIMPO V3 (LOOTZONE)
# =====================================================

GARIMPO_CONFIG = {
    "min_commission_rate": Decimal("0.08"),     # 8% mínimo
    "min_commission_value": Decimal("12.00"),   # R$ 12,00 mínimo por venda
    "min_sales": 50,                            # Demanda ativa sem saturar só no Top 10
    "min_price": Decimal("60.00"),              # Faixa de ticket rentável
    "max_price": Decimal("220.00"),             # Teto para compra por impulso/kits
    "min_rating": Decimal("4.6"),               # Qualidade mínima quando informada

    "limit": 30,
    "max_results": 15,
    "request_delay": 0.25,
    "fallback_category_limit": 5,
}

# Categorias, palavras-chave e vagas iniciais (alinhadas ao perfil LOOTZONE).
GARIMPO_CATEGORIES = {
    "Casa e decoração": {
        "quota": 4,
        "keywords": [
            "quadros decorativos",
            "bandeja decorativa",
            "espelho decorativo",
        ],
    },
    "Móveis e organização": {
        "quota": 4,
        "keywords": [
            "caixa organizadora",
            "mesa redonda",
            "organizador de casa",
        ],
    },
    "Cozinha e utilidades": {
        "quota": 3,
        "keywords": [
            "pote hermetico",
            "lixeira automatica",
        ],
    },
    "Iluminação": {
        "quota": 2,
        "keywords": [
            "luminaria",
            "abajur",
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
}

# Ordenações da Shopee:
# 2 = Mais vendidos | 5 = Maior comissão
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
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Variáveis ausentes no Railway: " + ", ".join(missing)
        )
    try:
        return int(CHAT_ID_RAW)
    except ValueError:
        raise RuntimeError("TELEGRAM_CHAT_ID inválido.")


CHAT_ID = validate_config()


# =====================================================
# 5. FUNÇÕES AUXILIARES & HISTÓRICO ANTI-REPETIÇÃO
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
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def load_seen_items():
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        cutoff = time.time() - (SEEN_TTL_DAYS * 86400)
        return {k: v for k, v in data.items() if v >= cutoff}
    except Exception:
        return {}


def save_seen_items(seen_dict):
    try:
        SEEN_FILE.write_text(
            json.dumps(seen_dict, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Não foi possível salvar seen_items.json.")


def clear_seen_items():
    try:
        if SEEN_FILE.exists():
            SEEN_FILE.unlink()
    except Exception:
        logger.exception("Não foi possível limpar seen_items.json.")


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
            await update.effective_message.reply_text("Acesso não autorizado.")
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
        (SHOPEE_APP_ID + timestamp + payload + SHOPEE_SECRET).encode("utf-8")
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
        raise RuntimeError(f"Erro GraphQL: {data['errors']}")

    return data


# =====================================================
# 8. RELATÓRIOS DE CONVERSÃO (COM PAGINAÇÃO AUTOMÁTICA)
# =====================================================

def get_conversions(
    start_ts: int,
    end_ts: int,
    limit: int = 100,
    max_pages: int = 10,
):
    all_nodes = []
    scroll_id = None

    try:
        for _ in range(max_pages):
            scroll_param = (
                f', scrollId: {json.dumps(scroll_id)}'
                if scroll_id
                else ""
            )

            query = f"""
            query {{
              conversionReport(
                purchaseTimeStart: {start_ts},
                purchaseTimeEnd: {end_ts},
                limit: {limit}{scroll_param}
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

            data = shopee_request(query)
            report = data.get("data", {}).get("conversionReport", {})
            nodes = report.get("nodes", [])
            all_nodes.extend(nodes)

            page_info = report.get("pageInfo", {})
            has_next = page_info.get("hasNextPage")
            scroll_id = page_info.get("scrollId")

            if not has_next or not scroll_id:
                break

            time.sleep(0.25)

        return all_nodes, None

    except Exception as e:
        logger.exception("Erro ao consultar conversões.")
        return None, str(e)


# =====================================================
# 9. RESUMO DE VENDAS
# =====================================================

def summarize(nodes):
    total_orders = 0
    total_commission = 0.0
    products = {}

    for node in nodes:
        total_commission += float(node.get("netCommission") or 0)

        for order in node.get("orders", []):
            total_orders += 1
            for item in order.get("items", []):
                name = (item.get("itemName") or "Produto")[:50]
                commission = float(item.get("itemTotalCommission") or 0)
                qty = int(item.get("qty") or 1)

                if name not in products:
                    products[name] = {"qty": 0, "commission": 0.0}

                products[name]["qty"] += qty
                products[name]["commission"] += commission

    top_products = sorted(
        products.items(),
        key=lambda x: x[1]["commission"],
        reverse=True,
    )[:5]

    return total_orders, total_commission, top_products


# =====================================================
# 10. FORMATAÇÃO DOS RELATÓRIOS (HTML SEGURO)
# =====================================================

def make_report(title, nodes, start_date, end_date):
    if nodes is None:
        return (
            "⚠️ Erro ao buscar dados da Shopee.\n"
            "Tente novamente em alguns minutos."
        )

    orders, commission, top = summarize(nodes)
    safe_title = html.escape(title)

    text = f"📊 <b>{safe_title}</b>\n"
    text += f"📅 Período: <code>{start_date}</code> → <code>{end_date}</code>\n\n"
    text += f"🛒 Pedidos: <b>{orders}</b>\n"
    text += f"💰 Comissão registrada: <b>{format_money(commission)}</b>\n\n"

    if top:
        text += "🔥 <b>Top produtos do período:</b>\n"
        for i, (name, data) in enumerate(top, 1):
            safe_name = html.escape(name)
            text += f"{i}. {safe_name}\n"
            text += (
                f"   └ {data['qty']} un | "
                f"{format_money(data['commission'])}\n"
            )
    else:
        text += "Nenhum pedido registrado neste período.\n"

    text += "\n——————————————\n"
    text += (
        "⚠️ Os dados da Shopee podem demorar "
        "algumas horas ou até 1 dia para atualizar."
    )
    return text


# =====================================================
# 11. COMANDOS DO TELEGRAM
# =====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acesso(update):
        return

    await update.message.reply_text(
        "Olá! Bem-vindo ao LootZone Analytics V3.\n\n"
        "📊 RELATÓRIOS\n"
        "/ontem — Resultado de ontem\n"
        "/mes — Resultado do mês (com paginação completa)\n"
        "/periodo 01/11 22/11 — Período personalizado\n\n"
        "🔎 GARIMPO\n"
        "/garimpo — Garimpo inteligente V3 (anti-repetição + alerta de variação)\n"
        "/limpar — Limpa o histórico de produtos já enviados\n\n"
        "⚙️ SISTEMA\n"
        "/status — Status do bot\n"
        "/ajuda — Lista de comandos"
    )


async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def limpar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acesso(update):
        return
    clear_seen_items()
    await update.message.reply_text(
        "🧹 Histórico de produtos enviados limpo! "
        "O próximo /garimpo vai considerar todos os anúncios novamente."
    )


# =====================================================
# 12. COMANDO ONTEM
# =====================================================

async def ontem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acesso(update):
        return

    await update.message.reply_text("🔎 Buscando dados de ontem...")

    yesterday = datetime.now(BR_TZ) - timedelta(days=1)
    start_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
    start_ts, end_ts = get_period_start_end(start_dt, end_dt)

    nodes, err = await asyncio.to_thread(get_conversions, start_ts, end_ts)

    if err:
        await update.message.reply_text(f"❌ Erro na API:\n{err[:2500]}")
        return

    report = make_report(
        "Relatório de Ontem",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await update.message.reply_text(report, parse_mode="HTML")


# =====================================================
# 13. COMANDO MÊS
# =====================================================

async def mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acesso(update):
        return

    await update.message.reply_text("🔎 Buscando dados do mês...")

    now = datetime.now(BR_TZ)
    if now.day == 1:
        await update.message.reply_text(
            "Ainda não existem dias anteriores neste mês para consultar."
        )
        return

    start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_dt = (now - timedelta(days=1)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    start_ts, end_ts = get_period_start_end(start_dt, end_dt)

    nodes, err = await asyncio.to_thread(
        get_conversions, start_ts, end_ts, 100
    )

    if err:
        await update.message.reply_text(f"❌ Erro na API:\n{err[:2500]}")
        return

    report = make_report(
        "Relatório do Mês (até ontem)",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await update.message.reply_text(report, parse_mode="HTML")


# =====================================================
# 14. COMANDO PERÍODO
# =====================================================

async def periodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acesso(update):
        return

    if len(context.args) != 2:
        await update.message.reply_text("Use:\n/periodo 01/11 22/11")
        return

    try:
        now = datetime.now(BR_TZ)
        start_raw = datetime.strptime(context.args[0], "%d/%m")
        end_raw = datetime.strptime(context.args[1], "%d/%m")

        start_year = now.year
        end_year = now.year
        if start_raw.month > end_raw.month:
            end_year += 1

        start_dt = start_raw.replace(
            year=start_year, hour=0, minute=0, second=0
        ).replace(tzinfo=BR_TZ)
        end_dt = end_raw.replace(
            year=end_year, hour=23, minute=59, second=59
        ).replace(tzinfo=BR_TZ)

        if end_dt < start_dt or end_dt > now:
            raise ValueError("Período inválido.")

    except ValueError:
        await update.message.reply_text(
            "Data inválida.\nExemplo: /periodo 01/11 22/11"
        )
        return

    await update.message.reply_text("🔎 Buscando período solicitado...")

    start_ts, end_ts = get_period_start_end(start_dt, end_dt)
    nodes, err = await asyncio.to_thread(
        get_conversions, start_ts, end_ts, 100
    )

    if err:
        await update.message.reply_text(f"❌ Erro na API:\n{err[:2500]}")
        return

    report = make_report(
        "Relatório Personalizado",
        nodes,
        start_dt.strftime("%d/%m/%Y"),
        end_dt.strftime("%d/%m/%Y"),
    )
    await update.message.reply_text(report, parse_mode="HTML")


# =====================================================
# 15. GARIMPO V3 — CONSULTA DE PRODUTOS
# =====================================================

def fetch_products(keyword, sort_type):
    safe_keyword = json.dumps(keyword, ensure_ascii=False)

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
      }}
    }}
    """

    data = shopee_request(query)
    result = data.get("data", {}).get("productOfferV2", {})
    return result.get("nodes", [])


# =====================================================
# 16. GARIMPO V3 — FILTROS & ANÁLISE DE VARIAÇÃO
# =====================================================

def estimate_commission(product):
    api_commission = to_decimal(product.get("commission"))
    if api_commission > 0:
        return api_commission
    price = to_decimal(product.get("priceMin"))
    rate = to_decimal(product.get("commissionRate"))
    return price * rate


def analyze_variation_risk(price_min, price_max):
    if price_min <= 0 or price_max <= price_min:
        return "single", "✅ Variação única (baixo risco)"
    ratio = price_max / price_min
    if ratio <= Decimal("1.05"):
        return "single", "✅ Variação única (baixo risco)"
    if ratio <= Decimal("1.40"):
        return "moderate", "🟡 Variações próximas (conferir capa)"
    return "high", "⚠️ Múltiplas variações (checar capa!)"


def filter_product(product):
    rate = to_decimal(product.get("commissionRate"))
    price_min = to_decimal(product.get("priceMin"))
    sales = int(product.get("sales") or 0)
    rating = to_decimal(product.get("ratingStar"))
    commission_val = estimate_commission(product)

    if rate < GARIMPO_CONFIG["min_commission_rate"]:
        return False
    if commission_val < GARIMPO_CONFIG["min_commission_value"]:
        return False
    if price_min < GARIMPO_CONFIG["min_price"]:
        return False
    if price_min > GARIMPO_CONFIG["max_price"]:
        return False
    if sales < GARIMPO_CONFIG["min_sales"]:
        return False
    if rating > 0 and rating < GARIMPO_CONFIG["min_rating"]:
        return False

    return True


# =====================================================
# 17. GARIMPO V3 — DEDUPLICAÇÃO
# =====================================================

def collect_unique_products(raw_results, seen_items):
    unique = {}

    for result in raw_results:
        product = result["product"]
        category = result["category"]
        item_id = str(product.get("itemId") or "")

        if not item_id or item_id in seen_items:
            continue

        if not filter_product(product):
            continue

        price_min = to_decimal(product.get("priceMin"))
        price_max = to_decimal(product.get("priceMax"))
        var_level, var_label = analyze_variation_risk(price_min, price_max)

        product["price_value"] = float(price_min)
        product["price_max_value"] = float(price_max)
        product["variation_level"] = var_level
        product["variation_label"] = var_label
        product["sales_value"] = int(product.get("sales") or 0)
        product["rate_value"] = float(to_decimal(product.get("commissionRate")))
        product["seller_rate_value"] = float(
            to_decimal(product.get("sellerCommissionRate"))
        )
        product["commission_value"] = float(estimate_commission(product))
        product["rating_value"] = float(to_decimal(product.get("ratingStar")))

        if item_id not in unique:
            product["_category"] = category
            unique[item_id] = product
        else:
            existing = unique[item_id]
            if product["commission_value"] > existing["commission_value"]:
                product["_category"] = category
                unique[item_id] = product

    return list(unique.values())


# =====================================================
# 18. GARIMPO V3 — PONTUAÇÃO
# =====================================================

def calculate_scores(products):
    if not products:
        return []

    max_sales = max(p["sales_value"] for p in products)
    max_commission = max(p["commission_value"] for p in products)

    for product in products:
        sales = product["sales_value"]
        commission = product["commission_value"]
        rating = product["rating_value"]

        demand_score = (
            math.log1p(sales) / math.log1p(max_sales) * 100
            if max_sales > 0
            else 0
        )
        commission_score = (
            math.log1p(commission) / math.log1p(max_commission) * 100
            if max_commission > 0
            else 0
        )
        rating_score = (min(rating, 5) / 5) * 100 if rating > 0 else 50

        score = (
            demand_score * 0.35
            + commission_score * 0.40
            + rating_score * 0.25
        )

        # Bônus LOOTZONE: variação única e comissão extra do vendedor
        if product["variation_level"] == "single":
            score += 4.0
        if product["seller_rate_value"] > 0:
            score += 3.0

        product["score"] = round(min(score, 100.0), 1)

    return sorted(
        products,
        key=lambda p: (p["score"], p["commission_value"]),
        reverse=True,
    )


# =====================================================
# 19. GARIMPO V3 — DISTRIBUIÇÃO DE RESULTADOS
# =====================================================

def select_diverse_products(products):
    selected = []
    selected_ids = set()
    category_counts = {}

    for category, config in GARIMPO_CATEGORIES.items():
        quota = config["quota"]
        category_products = [p for p in products if p["_category"] == category]

        for product in category_products:
            if len(selected) >= GARIMPO_CONFIG["max_results"]:
                break
            item_id = str(product["itemId"])
            if item_id in selected_ids:
                continue
            selected.append(product)
            selected_ids.add(item_id)
            category_counts[category] = category_counts.get(category, 0) + 1
            if category_counts[category] >= quota:
                break

    for product in products:
        if len(selected) >= GARIMPO_CONFIG["max_results"]:
            break
        item_id = str(product["itemId"])
        if item_id in selected_ids:
            continue
        category = product["_category"]
        if category_counts.get(category, 0) >= GARIMPO_CONFIG["fallback_category_limit"]:
            continue
        selected.append(product)
        selected_ids.add(item_id)
        category_counts[category] = category_counts.get(category, 0) + 1

    return selected


# =====================================================
# 20. GARIMPO V3 — EXECUÇÃO COMPLETA
# =====================================================

def execute_garimpo():
    seen_items = load_seen_items()
    raw_results = []
    total_searches = 0
    errors = []

    logger.info("Iniciando Garimpo Inteligente V3 (LOOTZONE).")

    for category, config in GARIMPO_CATEGORIES.items():
        for keyword in config["keywords"]:
            for sort_type in GARIMPO_SORT_TYPES:
                total_searches += 1
                try:
                    products = fetch_products(keyword, sort_type)
                    for product in products:
                        raw_results.append({
                            "product": product,
                            "category": category,
                        })
                except Exception as e:
                    logger.exception("Erro na pesquisa %s", keyword)
                    errors.append(f"{keyword}: {str(e)[:120]}")
                time.sleep(GARIMPO_CONFIG["request_delay"])

    unique_products = collect_unique_products(raw_results, seen_items)
    ranked_products = calculate_scores(unique_products)
    selected = select_diverse_products(ranked_products)

    # Salva os itens selecionados para não repetir por 7 dias
    now_ts = time.time()
    for p in selected:
        seen_items[str(p["itemId"])] = now_ts
    save_seen_items(seen_items)

    return {
        "products": selected,
        "total_raw": len(raw_results),
        "total_unique": len(unique_products),
        "total_searches": total_searches,
        "errors": errors,
        "category_counts": {
            cat: sum(1 for p in selected if p["_category"] == cat)
            for cat in GARIMPO_CATEGORIES
        },
    }


# =====================================================
# 21. GARIMPO V3 — ENVIO AO TELEGRAM
# =====================================================

async def garimpo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acesso(update):
        return

    status_message = await update.message.reply_text(
        "🔎 LOOTZONE GARIMPO V3\n\n"
        "Pesquisando produtos novos (R$ 60 a R$ 220 | Comissão ≥ R$ 12)...\n"
        "Aguarde."
    )

    try:
        result = await asyncio.to_thread(execute_garimpo)
    except Exception as e:
        logger.exception("Erro geral no garimpo.")
        await status_message.edit_text(
            f"❌ Falha ao executar o garimpo.\n{str(e)[:1000]}"
        )
        return

    products = result["products"]
    if not products:
        await status_message.edit_text(
            "Nenhum produto inédito passou pelos filtros.\n\n"
            f"Pesquisas: {result['total_searches']}\n"
            f"Resultados brutos: {result['total_raw']}\n"
            f"Aprovados inéditos: {result['total_unique']}\n\n"
            "Dica: use /limpar se quiser rever produtos dos últimos 7 dias."
        )
        return

    summary = (
        "✅ GARIMPO LOOTZONE V3 CONCLUÍDO\n\n"
        f"🔎 Pesquisas: {result['total_searches']}\n"
        f"📦 Brutos: {result['total_raw']} | Inéditos aprovados: {result['total_unique']}\n"
        f"🏆 Selecionados: {len(products)}\n\n"
        "DISTRIBUIÇÃO:\n"
    )
    for category, count in result["category_counts"].items():
        if count > 0:
            summary += f"• {category}: {count}\n"

    if result["errors"]:
        summary += "\n⚠️ Algumas pesquisas falharam."

    await status_message.edit_text(summary)

    for index, product in enumerate(products, 1):
        name = (product.get("productName") or "Produto sem nome")[:65]
        category = product["_category"]
        shop = (product.get("shopName") or "Não informada")[:35]
        link = product.get("offerLink") or ""
        image = product.get("imageUrl")

        price_min = product["price_value"]
        price_max = product["price_max_value"]
        price_str = (
            f"{format_money(price_min)} a {format_money(price_max)}"
            if price_max > price_min
            else format_money(price_min)
        )

        rate = product["rate_value"] * 100
        seller_rate = product["seller_rate_value"] * 100
        commission = product["commission_value"]
        sales = product["sales_value"]
        rating = product["rating_value"]
        score = product["score"]
        var_label = product["variation_label"]

        extra_tag = f" (Extra loja: {seller_rate:.1f}%)" if seller_rate > 0 else ""
        rating_str = f"{rating:.2f}/5" if rating > 0 else "Não informada"

        caption = (
            f"#{index} — {name}\n\n"
            f"🏷️ Categoria: {category}\n"
            f"🏆 Pontuação: {score}/100\n"
            f"{var_label}\n\n"
            f"💰 Comissão: {rate:.2f}%{extra_tag}\n"
            f"💵 Ganho est.: {format_money(commission)}\n"
            f"🏷️ Preço: {price_str}\n"
            f"📦 Vendas: {sales} | ⭐ {rating_str}\n"
            f"🏪 Loja: {shop}"
        )

        if image:
            try:
                await context.bot.send_photo(
                    chat_id=CHAT_ID,
                    photo=image,
                    caption=caption[:1000],
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=caption[:4000],
                )
        else:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=caption[:4000],
            )

        if link:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🔗 Link de afiliado:\n{link}",
            )

        await asyncio.sleep(0.8)


# =====================================================
# 22. STATUS DO BOT
# =====================================================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await verificar_acesso(update):
        return

    seen_count = len(load_seen_items())
    await update.message.reply_text(
        "🟢 LOOTZONE ANALYTICS V3\n\n"
        "Bot: Online | API: Configurada | Fuso: Brasília\n"
        f"Histórico anti-repetição: {seen_count} itens ({SEEN_TTL_DAYS} dias)\n\n"
        "FILTROS ATIVOS:\n"
        f"• Comissão: ≥ {GARIMPO_CONFIG['min_commission_rate'] * 100:.0f}% "
        f"e ≥ {format_money(GARIMPO_CONFIG['min_commission_value'])}\n"
        f"• Preço: {format_money(GARIMPO_CONFIG['min_price'])} a "
        f"{format_money(GARIMPO_CONFIG['max_price'])}\n"
        f"• Vendas: ≥ {GARIMPO_CONFIG['min_sales']} | "
        f"Nota: ≥ {GARIMPO_CONFIG['min_rating']}"
    )


# =====================================================
# 23. RELATÓRIO AUTOMÁTICO DIÁRIO
# =====================================================

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    yesterday = datetime.now(BR_TZ) - timedelta(days=1)
    start_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
    start_ts, end_ts = get_period_start_end(start_dt, end_dt)

    nodes, err = await asyncio.to_thread(get_conversions, start_ts, end_ts)

    if err:
        report = f"⚠️ Erro no relatório automático:\n{html.escape(err[:1000])}"
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
        parse_mode="HTML",
    )


# =====================================================
# 24. TRATAMENTO DE ERROS E INICIALIZAÇÃO
# =====================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Erro não tratado:", exc_info=context.error)


def main():
    logger.info("Inicializando LootZone Analytics V3...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("ontem", ontem))
    app.add_handler(CommandHandler("mes", mes))
    app.add_handler(CommandHandler("periodo", periodo))
    app.add_handler(CommandHandler("garimpo", garimpo))
    app.add_handler(CommandHandler("limpar", limpar))
    app.add_handler(CommandHandler("status", status))

    app.add_error_handler(error_handler)

    app.job_queue.run_daily(
        daily_report,
        time=dt_time(hour=10, minute=0, tzinfo=BR_TZ),
        name="relatorio_diario",
    )

    logger.info("Bot V3 iniciado. Relatório diário às 10h de Brasília.")
    app.run_polling()


if __name__ == "__main__":
    main()
