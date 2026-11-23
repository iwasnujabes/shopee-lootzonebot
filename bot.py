"""
LootZone Affiliate Bot
Python 3.11+
Dependências: python-telegram-bot[job-queue]==22.5, requests==2.32.5

Variáveis Railway:
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, SHOPEE_APP_ID, SHOPEE_SECRET

Opcionais:
SEEN_FILE=/data/seen_items.json
GARIMPO_MAX_PAGES=2
GARIMPO_REQUEST_DELAY=0.4
REPORT_HOUR=10

A API productOfferV2 não fornece a quantidade de afiliados promovendo
cada produto. Portanto, o bot não inventa uma métrica de saturação.
Confirme os campos GraphQL disponíveis na sua conta antes do deploy.
"""

import os
import json
import time
import html
import math
import hashlib
import asyncio
import logging
import tempfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from typing import Any

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes


# ============================================================
# CONFIGURAÇÃO
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID", "").strip()
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET", "").strip()

SHOPEE_URL = "https://open-api.affiliate.shopee.com.br/graphql"
BR_TZ = ZoneInfo("America/Sao_Paulo")

SEEN_FILE = Path(os.getenv("SEEN_FILE", "/data/seen_items.json"))
SEEN_TTL_DAYS = 7

MAX_PRODUCT_PAGES = max(
    1, min(int(os.getenv("GARIMPO_MAX_PAGES", "2")), 5)
)
REQUEST_DELAY = max(
    0.2, float(os.getenv("GARIMPO_REQUEST_DELAY", "0.4"))
)
REPORT_HOUR = max(
    0, min(int(os.getenv("REPORT_HOUR", "10")), 23)
)

GARIMPO_CONFIG = {
    "limit": 30,
    "max_results": 15,
    "min_rating": Decimal("4.6"),
    "min_sales": 50,
    "min_commission_rate": Decimal("0.08"),
    "request_delay": REQUEST_DELAY,
    "max_pages": MAX_PRODUCT_PAGES,
    "fallback_category_limit": 5,
    "history_days": SEEN_TTL_DAYS,
}

# Estratégia diversificada para a LootZone.
GARIMPO_CATEGORIES = {
    "Casa e decoração": {
        "quota": 4,
        "keywords": [
            "quadros decorativos",
            "bandeja decorativa",
            "espelho decorativo",
        ],
        "price_min": Decimal("25"),
        "price_max": Decimal("250"),
    },
    "Móveis e organização": {
        "quota": 3,
        "keywords": [
            "caixa organizadora",
            "mesa redonda",
            "organizador de casa",
        ],
        "price_min": Decimal("50"),
        "price_max": Decimal("500"),
    },
    "Cozinha e utilidades": {
        "quota": 2,
        "keywords": [
            "pote hermetico",
            "lixeira automatica",
            "fruteira cozinha",
        ],
        "price_min": Decimal("20"),
        "price_max": Decimal("250"),
    },
    "Iluminação": {
        "quota": 1,
        "keywords": ["luminaria", "abajur"],
        "price_min": Decimal("25"),
        "price_max": Decimal("250"),
    },
    "Geek e colecionáveis": {
        "quota": 2,
        "keywords": [
            "pelucia pokemon",
            "funko pop",
            "colecionavel geek",
        ],
        "price_min": Decimal("25"),
        "price_max": Decimal("350"),
    },
    "Tecnologia e gadgets": {
        "quota": 1,
        "keywords": [
            "suporte celular",
            "fone bluetooth",
            "acessorios tecnologia",
        ],
        "price_min": Decimal("25"),
        "price_max": Decimal("350"),
    },
    "Moda e vestuário": {
        "quota": 2,
        "keywords": [
            "camiseta streetwear",
            "moletom oversized",
            "roupa y2k",
        ],
        "price_min": Decimal("30"),
        "price_max": Decimal("250"),
    },
}

SORT_TYPES = (2, 5)  # 2 = vendas; 5 = comissão

SCORE_WEIGHTS = {
    "demand": 30,
    "profit": 30,
    "competition": 20,
    "quality": 10,
    "content": 10,
}


# ============================================================
# LOGS E ESTADO
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("lootzone_bot")

SEEN_LOCK = asyncio.Lock()
GARIMPO_LOCK = asyncio.Lock()
HTTP = requests.Session()


def validate_environment() -> int:
    missing = [
        name
        for name, value in {
            "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": CHAT_ID_RAW,
            "SHOPEE_APP_ID": SHOPEE_APP_ID,
            "SHOPEE_SECRET": SHOPEE_SECRET,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Variáveis de ambiente ausentes: " + ", ".join(missing)
        )

    try:
        return int(CHAT_ID_RAW)
    except ValueError as exc:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID precisa ser numérico."
        ) from exc


CHAT_ID = validate_environment()


def dec(value: Any) -> Decimal:
    try:
        return Decimal(
            str(value if value not in (None, "") else "0")
        )
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money(value: Any) -> str:
    number = float(dec(value))
    return (
        f"R$ {number:,.2f}"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def now_br() -> datetime:
    return datetime.now(BR_TZ)


def is_authorized(update: Update) -> bool:
    return bool(
        update.effective_chat
        and update.effective_chat.id == CHAT_ID
    )


async def guard(update: Update) -> bool:
    if not is_authorized(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Acesso não autorizado."
            )
        return False

    return True


# ============================================================
# PERSISTÊNCIA ANTI-REPETIÇÃO
# ============================================================

def _load_seen_sync() -> dict[str, float]:
    if not SEEN_FILE.exists():
        return {}

    try:
        raw = json.loads(
            SEEN_FILE.read_text(encoding="utf-8")
        )

        cutoff = time.time() - SEEN_TTL_DAYS * 86400
        clean = {}

        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    stamp = float(value)
                    if stamp >= cutoff:
                        clean[str(key)] = stamp
                except (ValueError, TypeError):
                    continue

        return clean

    except Exception:
        logger.exception(
            "Falha ao ler histórico; não será apagado automaticamente."
        )
        return {}


def _save_seen_sync(data: dict[str, float]) -> None:
    SEEN_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix="seen_items_",
        suffix=".tmp",
        dir=str(SEEN_FILE.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_name, SEEN_FILE)

    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


async def load_seen() -> dict[str, float]:
    async with SEEN_LOCK:
        return await asyncio.to_thread(_load_seen_sync)


async def mark_seen(item_ids: list[str]) -> None:
    async with SEEN_LOCK:
        data = await asyncio.to_thread(_load_seen_sync)
        stamp = time.time()

        for item_id in item_ids:
            data[str(item_id)] = stamp

        await asyncio.to_thread(_save_seen_sync, data)


async def clear_seen() -> None:
    async with SEEN_LOCK:
        await asyncio.to_thread(
            lambda: SEEN_FILE.unlink(missing_ok=True)
        )


# ============================================================
# SHOPEE GRAPHQL
# ============================================================

def shopee_request(
    query: str,
    max_attempts: int = 4,
) -> dict:

    payload = json.dumps(
        {"query": query},
        separators=(",", ":"),
        ensure_ascii=False,
    )

    timestamp = str(int(time.time()))

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

    last_error = None

    for attempt in range(max_attempts):
        try:
            response = HTTP.post(
                SHOPEE_URL,
                data=payload.encode("utf-8"),
                headers=headers,
                timeout=(10, 35),
            )

            if (
                response.status_code == 429
                or response.status_code >= 500
            ):
                wait = min(2 ** attempt, 12)

                logger.warning(
                    "Shopee HTTP %s; nova tentativa em %ss.",
                    response.status_code,
                    wait,
                )

                last_error = RuntimeError(
                    f"HTTP {response.status_code}"
                )

                time.sleep(wait)
                continue

            response.raise_for_status()
            body = response.json()

            errors = body.get("errors") or []

            if errors:
                if not body.get("data"):
                    raise RuntimeError(
                        "GraphQL: "
                        + json.dumps(
                            errors,
                            ensure_ascii=False,
                        )[:1200]
                    )

                logger.warning(
                    "GraphQL retornou dados parciais com erros: %s",
                    json.dumps(
                        errors,
                        ensure_ascii=False,
                    )[:600],
                )

            return body

        except (
            requests.RequestException,
            ValueError,
            RuntimeError,
        ) as exc:

            last_error = exc

            if attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 12))

    raise RuntimeError(
        f"Falha na comunicação com a Shopee: {last_error}"
    )


def fetch_products(
    keyword: str,
    sort_type: int,
) -> list[dict]:

    safe_keyword = json.dumps(
        keyword,
        ensure_ascii=False,
    )

    products: list[dict] = []
    page = 1

    for page_index in range(
        GARIMPO_CONFIG["max_pages"]
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
              periodEndTime
              shopType
            }}
            pageInfo {{
              page
              limit
              hasNextPage
            }}
          }}
        }}
        """

        body = shopee_request(query)

        result = (
            body.get("data", {}).get("productOfferV2")
            or {}
        )

        products.extend(result.get("nodes") or [])

        page_info = result.get("pageInfo") or {}

        if not page_info.get("hasNextPage"):
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    return products


# ============================================================
# CRITÉRIOS E RANKING
# ============================================================

def estimated_commission(product: dict) -> Decimal:
    api_value = dec(product.get("commission"))

    if api_value > 0:
        return api_value

    return (
        dec(product.get("priceMin"))
        * dec(product.get("commissionRate"))
    )


def content_score(
    category: str,
    keyword: str,
) -> float:

    visual_terms = (
        "quadro",
        "espelho",
        "luminaria",
        "abajur",
        "pelucia",
        "funko",
        "fruteira",
        "mesa",
        "bandeja",
        "pote",
        "organizador",
        "streetwear",
        "moletom",
        "camiseta",
    )

    text = f"{category} {keyword}".lower()

    return (
        9.0
        if any(term in text for term in visual_terms)
        else 6.5
    )


def eligible(
    product: dict,
    category: str,
) -> tuple[bool, str]:

    price = dec(product.get("priceMin"))
    sales = safe_int(product.get("sales"))
    rating = dec(product.get("ratingStar"))
    rate = dec(product.get("commissionRate"))

    cfg = GARIMPO_CATEGORIES[category]

    if price <= 0:
        return False, "preço indisponível"

    if not (
        cfg["price_min"]
        <= price
        <= cfg["price_max"]
    ):
        return False, "fora da faixa de preço da categoria"

    if sales < GARIMPO_CONFIG["min_sales"]:
        return False, "demanda abaixo do mínimo exploratório"

    if (
        rating > 0
        and rating < GARIMPO_CONFIG["min_rating"]
    ):
        return False, "avaliação abaixo de 4,6"

    # Filtro de comissão suave:
    # uma comissão absoluta interessante pode compensar
    # uma taxa percentual inferior a 8%.
    commission = estimated_commission(product)

    if (
        rate < GARIMPO_CONFIG["min_commission_rate"]
        and commission < Decimal("5")
    ):
        return False, "comissão e taxa baixas"

    return True, ""


def normalize_log(
    value: float,
    maximum: float,
) -> float:

    if maximum <= 0:
        return 0.0

    return min(
        1.0,
        math.log1p(max(0.0, value))
        / math.log1p(maximum),
    )


def score_products(
    products: list[dict],
) -> list[dict]:

    max_sales = max(
        [safe_int(p.get("sales")) for p in products]
        or [1]
    )

    max_comm = max(
        [
            float(estimated_commission(p))
            for p in products
        ]
        or [1]
    )

    scored = []

    for product in products:

        category = product["_category"]
        keyword = product["_keyword"]

        sales = safe_int(product.get("sales"))
        commission = float(
            estimated_commission(product)
        )

        rating = dec(product.get("ratingStar"))

        demand_points = (
            normalize_log(sales, max_sales) * 30
        )

        profit_points = (
            normalize_log(commission, max_comm) * 30
        )

        quality_points = (
            float(min(rating, Decimal("5"))) / 5 * 10
            if rating > 0
            else 5.0
        )

        content_points = content_score(
            category,
            keyword,
        )

        # A API não fornece a quantidade de afiliados.
        # Não existe penalidade real de concorrência neste cálculo.
        competition_points = 10.0

        total = min(
            100.0,
            demand_points
            + profit_points
            + quality_points
            + content_points
            + competition_points,
        )

        pmin = dec(product.get("priceMin"))
        pmax = dec(product.get("priceMax"))

        if pmax <= 0:
            variation_label = (
                "Variações: dado indisponível"
            )

        elif pmax <= pmin:
            variation_label = (
                "Faixa de preço sem variação informada"
            )

        elif (
            pmax / max(pmin, Decimal("0.01"))
            <= Decimal("1.05")
        ):
            variation_label = "Faixa de preço estreita"

        elif (
            pmax / max(pmin, Decimal("0.01"))
            <= Decimal("1.40")
        ):
            variation_label = "Faixa de preço moderada"

        else:
            variation_label = (
                "Grande diferença entre preços; "
                "conferir variações"
            )

        product["_score"] = round(total, 1)
        product["_commission_est"] = commission
        product["_price_min"] = pmin
        product["_price_max"] = pmax
        product["_sales"] = sales
        product["_rating"] = rating
        product["_variation_label"] = variation_label

        product["_competition"] = (
            "Desconhecida (não fornecida pela API)"
        )

        product["_score_parts"] = {
            "demanda": round(demand_points, 1),
            "rentabilidade": round(profit_points, 1),
            "qualidade": round(quality_points, 1),
            "conteudo": round(content_points, 1),
            "concorrencia": round(competition_points, 1),
        }

        scored.append(product)

    return sorted(
        scored,
        key=lambda p: p["_score"],
        reverse=True,
    )


def collect_candidates(
    seen: dict[str, float],
) -> tuple[list[dict], list[str]]:

    raw: dict[str, dict] = {}
    errors: list[str] = []

    for category, config in GARIMPO_CATEGORIES.items():

        for keyword in config["keywords"]:

            for sort_type in SORT_TYPES:

                try:
                    results = fetch_products(
                        keyword,
                        sort_type,
                    )

                    for product in results:

                        item_id = str(
                            product.get("itemId") or ""
                        )

                        if not item_id or item_id in seen:
                            continue

                        ok, _reason = eligible(
                            product,
                            category,
                        )

                        if not ok:
                            continue

                        old = raw.get(item_id)

                        candidate = dict(product)
                        candidate["_category"] = category
                        candidate["_keyword"] = keyword

                        if (
                            old is None
                            or estimated_commission(candidate)
                            > estimated_commission(old)
                        ):
                            raw[item_id] = candidate

                except Exception as exc:

                    logger.exception(
                        "Falha no garimpo: %s / %s",
                        category,
                        keyword,
                    )

                    errors.append(
                        f"{category} — {keyword}: "
                        f"{str(exc)[:160]}"
                    )

                time.sleep(REQUEST_DELAY)

    return list(raw.values()), errors


def select_by_category(
    scored: list[dict],
) -> list[dict]:

    selected: list[dict] = []
    selected_ids: set[str] = set()

    category_counts = {
        name: 0
        for name in GARIMPO_CATEGORIES
    }

    # Primeira passada: respeitar quotas por categoria.
    for category, config in GARIMPO_CATEGORIES.items():

        candidates = [
            p
            for p in scored
            if p["_category"] == category
        ]

        for product in candidates:

            item_id = str(product.get("itemId"))

            if (
                category_counts[category]
                >= config["quota"]
            ):
                break

            if item_id not in selected_ids:

                selected.append(product)
                selected_ids.add(item_id)
                category_counts[category] += 1

    # Segunda passada: redistribuir vagas disponíveis.
    for product in scored:

        if (
            len(selected)
            >= GARIMPO_CONFIG["max_results"]
        ):
            break

        item_id = str(product.get("itemId"))
        category = product["_category"]

        if item_id in selected_ids:
            continue

        if (
            category_counts[category]
            >= GARIMPO_CONFIG["fallback_category_limit"]
        ):
            continue

        selected.append(product)
        selected_ids.add(item_id)
        category_counts[category] += 1

    return sorted(
        selected,
        key=lambda p: p["_score"],
        reverse=True,
    )


# ============================================================
# TELEGRAM — APRESENTAÇÃO DOS PRODUTOS
# ============================================================

def product_caption(
    product: dict,
    index: int,
) -> str:

    name = html.escape(
        str(
            product.get("productName")
            or "Produto sem nome"
        )[:180]
    )

    shop = html.escape(
        str(
            product.get("shopName")
            or "Loja não informada"
        )[:80]
    )

    category = html.escape(
        product["_category"]
    )

    link = str(
        product.get("offerLink") or ""
    ).strip()

    commission_rate = (
        dec(product.get("commissionRate")) * 100
    )

    rating = product["_rating"]

    rating_text = (
        f"{rating:.1f}/5"
        if rating > 0
        else "Não informada"
    )

    url_line = (
        f'<a href="{html.escape(link, quote=True)}">'
        "Ver oferta na Shopee</a>"
        if link.startswith("http")
        else "Link de oferta indisponível"
    )

    return (
        f"<b>#{index} · {name}</b>\n"
        f"Categoria: {category}\n"
        f"Preço: <b>{money(product['_price_min'])}</b>\n"
        f"Comissão estimada: "
        f"<b>{money(product['_commission_est'])}</b> "
        f"({commission_rate:.1f}%)\n"
        f"Vendas informadas: "
        f"{product['_sales']:,}\n"
        f"Avaliação: {rating_text}\n"
        f"Loja: {shop}\n"
        f"Pontuação: <b>{product['_score']}/100</b>\n"
        f"Concorrência: {product['_competition']}\n"
        f"{html.escape(product['_variation_label'])}\n"
        f"{url_line}"
    )


async def send_product(
    bot,
    product: dict,
    index: int,
) -> bool:

    caption = product_caption(
        product,
        index,
    )

    image_url = str(
        product.get("imageUrl") or ""
    ).strip()

    try:

        if (
            image_url.startswith("https://")
            or image_url.startswith("http://")
        ):

            await bot.send_photo(
                chat_id=CHAT_ID,
                photo=image_url,
                caption=caption[:1024],
                parse_mode=ParseMode.HTML,
            )

        else:

            await bot.send_message(
                chat_id=CHAT_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        return True

    except RetryAfter as exc:

        await asyncio.sleep(
            float(exc.retry_after) + 1
        )

        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

            return True

        except TelegramError:

            logger.exception(
                "Falha ao reenviar produto %s.",
                product.get("itemId"),
            )

            return False

    except TelegramError:

        logger.exception(
            "Falha ao enviar produto %s.",
            product.get("itemId"),
        )

        return False


async def garimpo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await guard(update):
        return

    if GARIMPO_LOCK.locked():

        await update.effective_message.reply_text(
            "Um garimpo já está em andamento. Aguarde terminar."
        )

        return

    async with GARIMPO_LOCK:

        status_msg = (
            await update.effective_message.reply_text(
                "🔎 Iniciando garimpo da LootZone. "
                "Isso pode levar alguns minutos..."
            )
        )

        seen = await load_seen()

        candidates, errors = await asyncio.to_thread(
            collect_candidates,
            seen,
        )

        if not candidates:

            await status_msg.edit_text(
                "Não encontrei produtos elegíveis nesta execução. "
                "Tente novamente mais tarde ou revise os filtros."
            )

            if errors:
                logger.warning(
                    "Erros de consulta: %s",
                    errors[:8],
                )

            return

        scored = score_products(candidates)
        selected = select_by_category(scored)

        sent_ids = []
        sent_count = 0

        for index, product in enumerate(selected, 1):

            success = await send_product(
                context.bot,
                product,
                index,
            )

            if success:
                sent_count += 1
                sent_ids.append(
                    str(product["itemId"])
                )

            await asyncio.sleep(0.45)

        # Só marca como visto o produto enviado com sucesso.
        if sent_ids:
            await mark_seen(sent_ids)

        summary = (
            f"✅ Garimpo concluído.\n"
            f"Produtos encontrados: {len(candidates)}\n"
            f"Selecionados: {len(selected)}\n"
            f"Enviados: {sent_count}\n"
            f"Histórico: {SEEN_TTL_DAYS} dias\n"
            f"Concorrência de afiliados: indisponível na API; "
            f"não estimada."
        )

        if errors:
            summary += (
                f"\nConsultas com erro: {len(errors)} "
                "(detalhes no log)."
            )

        await status_msg.edit_text(summary)


# ============================================================
# RELATÓRIO DE CONVERSÕES
# ============================================================

def get_conversions(
    start_ts: int,
    end_ts: int,
    limit: int = 100,
    max_pages: int = 10,
):

    nodes: list[dict] = []
    scroll_id = None

    for page_num in range(max_pages):

        scroll_arg = (
            f", scrollId: {json.dumps(scroll_id)}"
            if scroll_id
            else ""
        )

        query = f"""
        {{
          conversionReport(
            purchaseTimeStart: {start_ts},
            purchaseTimeEnd: {end_ts},
            limit: {limit}{scroll_arg}
          ) {{
            nodes {{
              conversionId
              purchaseTime
              clickTime
              totalCommission
              netCommission
              sellerCommission
              orders {{
                orderId
                orderStatus
                items {{
                  itemId
                  itemName
                  qty
                  actualAmount
                  itemTotalCommission
                }}
              }}
            }}
            pageInfo {{ hasNextPage scrollId }}
          }}
        }}
        """

        body = shopee_request(query)

        report = (
            body.get("data", {}).get("conversionReport")
            or {}
        )

        nodes.extend(
            report.get("nodes") or []
        )

        page_info = report.get("pageInfo") or {}

        if not page_info.get("hasNextPage"):
            return nodes, False

        scroll_id = page_info.get("scrollId")

        if not scroll_id:
            raise RuntimeError(
                "Shopee indicou próxima página, "
                "mas não retornou scrollId."
            )

        # O scrollId tem validade curta.
        # A próxima página é consultada imediatamente.

    raise RuntimeError(
        f"Relatório excedeu o limite de {max_pages} páginas. "
        "Resultado parcial não será apresentado como completo."
    )


def summarize_conversions(
    nodes: list[dict],
) -> dict:

    total_commission = Decimal("0")

    orders: dict[str, dict] = {}
    products: dict[str, dict] = {}

    statuses = {
        "UNPAID": 0,
        "PENDING": 0,
        "COMPLETED": 0,
        "CANCELLED": 0,
        "UNKNOWN": 0,
    }

    seen_conversion_ids = set()

    for node in nodes:

        conversion_id = str(
            node.get("conversionId") or ""
        )

        # Evita somar a mesma conversão repetida.
        if (
            conversion_id
            and conversion_id in seen_conversion_ids
        ):
            continue

        if conversion_id:
            seen_conversion_ids.add(conversion_id)

        total_commission += dec(
            node.get("netCommission")
        )

        for order in node.get("orders") or []:

            order_id = str(
                order.get("orderId") or ""
            )

            status = str(
                order.get("orderStatus") or "UNKNOWN"
            ).upper()

            if status not in statuses:
                status = "UNKNOWN"

            if (
                order_id
                and order_id not in orders
            ):

                orders[order_id] = {
                    "status": status
                }

                statuses[status] += 1

            for item in order.get("items") or []:

                item_id = str(
                    item.get("itemId")
                    or item.get("itemName")
                    or "Produto"
                )

                name = str(
                    item.get("itemName")
                    or "Produto"
                )

                record = products.setdefault(
                    item_id,
                    {
                        "name": name,
                        "qty": 0,
                        "commission": Decimal("0"),
                    },
                )

                record["qty"] += safe_int(
                    item.get("qty"),
                    1,
                )

                record["commission"] += dec(
                    item.get("itemTotalCommission")
                )

    top = sorted(
        products.values(),
        key=lambda x: x["commission"],
        reverse=True,
    )[:5]

    return {
        "orders": len(orders),
        "commission": total_commission,
        "statuses": statuses,
        "top": top,
    }


def make_report(
    title: str,
    nodes: list[dict],
    start: datetime,
    end: datetime,
) -> str:

    summary = summarize_conversions(nodes)

    lines = [
        f"📊 <b>{html.escape(title)}</b>",
        (
            f"📅 <code>{start:%d/%m/%Y}</code> "
            f"→ <code>{end:%d/%m/%Y}</code>"
        ),
        "",
        (
            f"🛒 Pedidos únicos: "
            f"<b>{summary['orders']}</b>"
        ),
        (
            f"💰 Comissão líquida reportada: "
            f"<b>{money(summary['commission'])}</b>"
        ),
        "",
        "<b>Status dos pedidos:</b>",
    ]

    for status, count in summary["statuses"].items():
        lines.append(
            f"• {status}: {count}"
        )

    lines.append("")
    lines.append(
        "<b>Top produtos por comissão de item:</b>"
    )

    if summary["top"]:

        for idx, item in enumerate(
            summary["top"],
            1,
        ):

            lines.append(
                f"{idx}. "
                f"{html.escape(item['name'][:65])} — "
                f"{item['qty']} un. — "
                f"{money(item['commission'])}"
            )

    else:
        lines.append("Nenhum item encontrado.")

    lines.extend([
        "",
        "⚠️ Conversion Report contém valores estimados; "
        "validação final deve ser conferida no painel/"
        "Validated Report.",
    ])

    return "\n".join(lines)


async def send_report(
    update: Update,
    title: str,
    start: datetime,
    end: datetime,
):

    msg = await update.effective_message.reply_text(
        "Consultando relatório da Shopee..."
    )

    try:

        nodes, _ = await asyncio.to_thread(
            get_conversions,
            int(start.timestamp()),
            int(end.timestamp()),
        )

        report = make_report(
            title,
            nodes,
            start,
            end,
        )

        await msg.edit_text(
            report,
            parse_mode=ParseMode.HTML,
        )

    except Exception as exc:

        logger.exception(
            "Erro no relatório."
        )

        await msg.edit_text(
            "Não foi possível concluir o relatório: "
            + html.escape(str(exc)[:900])
        )


# ============================================================
# COMANDOS DE RELATÓRIO
# ============================================================

async def ontem(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await guard(update):
        return

    day = now_br().date() - timedelta(days=1)

    start = datetime.combine(
        day,
        dt_time.min,
        BR_TZ,
    )

    end = datetime.combine(
        day,
        dt_time(23, 59, 59),
        BR_TZ,
    )

    await send_report(
        update,
        "Relatório de ontem",
        start,
        end,
    )


async def mes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await guard(update):
        return

    today = now_br().date()

    if today.day == 1:

        await update.effective_message.reply_text(
            "Ainda não há dias anteriores neste mês."
        )

        return

    start = datetime.combine(
        today.replace(day=1),
        dt_time.min,
        BR_TZ,
    )

    end = datetime.combine(
        today - timedelta(days=1),
        dt_time(23, 59, 59),
        BR_TZ,
    )

    await send_report(
        update,
        "Relatório do mês até ontem",
        start,
        end,
    )


async def periodo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await guard(update):
        return

    if len(context.args) != 2:

        await update.effective_message.reply_text(
            "Uso: /periodo DD/MM/AAAA DD/MM/AAAA"
        )

        return

    try:

        start_date = datetime.strptime(
            context.args[0],
            "%d/%m/%Y",
        ).date()

        end_date = datetime.strptime(
            context.args[1],
            "%d/%m/%Y",
        ).date()

        if (
            end_date < start_date
            or end_date > now_br().date()
        ):
            raise ValueError

        if (
            end_date - start_date
        ).days > 90:

            await update.effective_message.reply_text(
                "Por segurança, consulte períodos de até 90 dias."
            )

            return

        start = datetime.combine(
            start_date,
            dt_time.min,
            BR_TZ,
        )

        end = datetime.combine(
            end_date,
            dt_time(23, 59, 59),
            BR_TZ,
        )

    except ValueError:

        await update.effective_message.reply_text(
            "Datas inválidas. Exemplo: "
            "/periodo 01/11/2026 22/11/2026"
        )

        return

    await send_report(
        update,
        "Relatório personalizado",
        start,
        end,
    )


async def daily_report(
    context: ContextTypes.DEFAULT_TYPE,
):

    yesterday = (
        now_br().date() - timedelta(days=1)
    )

    start = datetime.combine(
        yesterday,
        dt_time.min,
        BR_TZ,
    )

    end = datetime.combine(
        yesterday,
        dt_time(23, 59, 59),
        BR_TZ,
    )

    try:

        nodes, _ = await asyncio.to_thread(
            get_conversions,
            int(start.timestamp()),
            int(end.timestamp()),
        )

        report = make_report(
            "Relatório automático — ontem",
            nodes,
            start,
            end,
        )

        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=report,
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        logger.exception(
            "Falha no relatório automático."
        )


# ============================================================
# COMANDOS AUXILIARES
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await guard(update):
        return

    await update.effective_message.reply_text(
        "🤖 <b>LootZone Affiliate Bot</b>\n\n"
        "<b>Relatórios</b>\n"
        "/ontem — relatório de ontem\n"
        "/mes — mês até ontem\n"
        "/periodo DD/MM/AAAA DD/MM/AAAA — "
        "período personalizado\n\n"
        "<b>Garimpo</b>\n"
        "/garimpo — buscar oportunidades\n"
        "/limpar — limpar histórico anti-repetição\n\n"
        "<b>Sistema</b>\n"
        "/status — estado do bot\n"
        "/ajuda — ajuda",
        parse_mode=ParseMode.HTML,
    )


async def ajuda(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await start(update, context)


async def limpar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await guard(update):
        return

    await clear_seen()

    await update.effective_message.reply_text(
        "Histórico anti-repetição limpo."
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await guard(update):
        return

    seen = await load_seen()

    volume_hint = (
        "caminho /data — confirme se há Volume montado no Railway"
        if str(SEEN_FILE).startswith("/data/")
        else "caminho local; confirme persistência no Railway"
    )

    await update.effective_message.reply_text(
        "🟢 LootZone Bot operacional\n"
        f"Fuso: {BR_TZ}\n"
        f"Produtos no histórico: {len(seen)}\n"
        f"Arquivo: {SEEN_FILE}\n"
        f"Armazenamento: {volume_hint}\n"
        f"Páginas por consulta: {MAX_PRODUCT_PAGES}\n"
        f"Categorias: {len(GARIMPO_CATEGORIES)}"
    )


# ============================================================
# INICIALIZAÇÃO
# ============================================================

async def post_init(
    application: Application,
):

    if application.job_queue:

        application.job_queue.run_daily(
            daily_report,
            time=dt_time(
                hour=REPORT_HOUR,
                minute=0,
                tzinfo=BR_TZ,
            ),
            name="lootzone_daily_report",
        )

        logger.info(
            "Relatório diário agendado para %02d:00 (Brasília).",
            REPORT_HOUR,
        )

    else:

        logger.warning(
            "JobQueue indisponível; "
            "relatório diário não foi agendado."
        )


def main():

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("ajuda", ajuda)
    )

    application.add_handler(
        CommandHandler("ontem", ontem)
    )

    application.add_handler(
        CommandHandler("mes", mes)
    )

    application.add_handler(
        CommandHandler("periodo", periodo)
    )

    application.add_handler(
        CommandHandler("garimpo", garimpo)
    )

    application.add_handler(
        CommandHandler("limpar", limpar)
    )

    application.add_handler(
        CommandHandler("status", status)
    )

    logger.info(
        "LootZone Affiliate Bot iniciado."
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
