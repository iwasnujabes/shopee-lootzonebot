
import os
import json
import time
import html
import hashlib
import asyncio
import logging
import tempfile
import math

from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import requests

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# ============================================================
# CONFIGURAÇÕES
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID", "").strip()
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET", "").strip()

SHOPEE_URL = (
    "https://open-api.affiliate.shopee.com.br/graphql"
)

FUSO = ZoneInfo("America/Sao_Paulo")

SEEN_FILE = Path(
    os.getenv("SEEN_FILE", "/data/seen_items.json")
)

HORARIO_RELATORIO = 10
HISTORICO_DIAS = 7

# Garimpo otimizado
LIMITE_PRODUTOS = 30
MAX_RESULTADOS = 15
MIN_VENDAS = 50
MIN_AVALIACAO = Decimal("4.6")

INTERVALO_BUSCAS = 0.35
TIMEOUT_CONEXAO = 8
TIMEOUT_RESPOSTA = 20
TENTATIVAS_SHOPEE = 2

# Uma ordenação principal por vendas.
# Evita duplicar todas as consultas com várias ordenações.
ORDENACAO_PRINCIPAL = 2

CATEGORIAS = {
    "Casa e decoração": {
        "quota": 4,
        "palavras": [
            "quadros decorativos",
            "bandeja decorativa",
        ],
        "min": Decimal("25"),
        "max": Decimal("250"),
    },
    "Móveis e organização": {
        "quota": 3,
        "palavras": [
            "caixa organizadora",
            "mesa redonda",
        ],
        "min": Decimal("50"),
        "max": Decimal("500"),
    },
    "Cozinha e utilidades": {
        "quota": 2,
        "palavras": [
            "pote hermetico",
            "fruteira cozinha",
        ],
        "min": Decimal("20"),
        "max": Decimal("250"),
    },
    "Iluminação": {
        "quota": 1,
        "palavras": [
            "luminaria",
            "abajur",
        ],
        "min": Decimal("25"),
        "max": Decimal("250"),
    },
    "Geek e colecionáveis": {
        "quota": 2,
        "palavras": [
            "pelucia pokemon",
            "funko pop",
        ],
        "min": Decimal("25"),
        "max": Decimal("350"),
    },
    "Tecnologia e gadgets": {
        "quota": 1,
        "palavras": [
            "suporte celular",
            "fone bluetooth",
        ],
        "min": Decimal("25"),
        "max": Decimal("350"),
    },
    "Moda e vestuário": {
        "quota": 2,
        "palavras": [
            "camiseta streetwear",
            "moletom oversized",
        ],
        "min": Decimal("30"),
        "max": Decimal("250"),
    },
}


# ============================================================
# INICIALIZAÇÃO
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("lootzone")

HTTP = requests.Session()

LOCK_GARIMPO = asyncio.Lock()
LOCK_HISTORICO = asyncio.Lock()


def validar_ambiente():
    variaveis = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": CHAT_ID_RAW,
        "SHOPEE_APP_ID": SHOPEE_APP_ID,
        "SHOPEE_SECRET": SHOPEE_SECRET,
    }

    faltando = [
        nome for nome, valor in variaveis.items()
        if not valor
    ]

    if faltando:
        raise RuntimeError(
            "Variáveis ausentes: " + ", ".join(faltando)
        )

    try:
        return int(CHAT_ID_RAW)
    except ValueError as exc:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID precisa ser numérico."
        ) from exc


CHAT_ID = validar_ambiente()


def agora():
    return datetime.now(FUSO)


def decimal(valor):
    try:
        return Decimal(str(valor or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def inteiro(valor, padrao=0):
    try:
        return int(float(valor))
    except (ValueError, TypeError):
        return padrao


def dinheiro(valor):
    numero = f"{float(decimal(valor)):,.2f}"
    numero = numero.replace(",", "X")
    numero = numero.replace(".", ",")
    numero = numero.replace("X", ".")
    return f"R$ {numero}"


def autorizado(update):
    return bool(
        update.effective_chat
        and update.effective_chat.id == CHAT_ID
    )


async def verificar_acesso(update):
    if not autorizado(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Acesso não autorizado."
            )
        return False

    return True


# ============================================================
# HISTÓRICO PERSISTENTE
# ============================================================

def carregar_historico_sync():
    if not SEEN_FILE.exists():
        return {}

    try:
        dados = json.loads(
            SEEN_FILE.read_text(encoding="utf-8")
        )

        limite = time.time() - HISTORICO_DIAS * 86400

        return {
            str(chave): float(valor)
            for chave, valor in dados.items()
            if float(valor) >= limite
        }

    except Exception:
        logger.exception("Erro ao carregar histórico.")
        return {}


def salvar_historico_sync(dados):
    SEEN_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, nome_temp = tempfile.mkstemp(
        dir=str(SEEN_FILE.parent),
        suffix=".tmp",
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as arquivo:
            json.dump(
                dados,
                arquivo,
                ensure_ascii=False,
            )
            arquivo.flush()
            os.fsync(arquivo.fileno())

        os.replace(nome_temp, SEEN_FILE)

    finally:
        if os.path.exists(nome_temp):
            os.unlink(nome_temp)


async def carregar_historico():
    async with LOCK_HISTORICO:
        return await asyncio.to_thread(
            carregar_historico_sync
        )


async def marcar_como_visto(ids):
    async with LOCK_HISTORICO:
        dados = await asyncio.to_thread(
            carregar_historico_sync
        )

        for item_id in ids:
            dados[str(item_id)] = time.time()

        await asyncio.to_thread(
            salvar_historico_sync,
            dados,
        )


async def limpar_historico():
    async with LOCK_HISTORICO:
        await asyncio.to_thread(
            lambda: SEEN_FILE.unlink(missing_ok=True)
        )


# ============================================================
# SHOPEE — REQUISIÇÃO E BUSCA
# ============================================================

def requisicao_shopee(query):
    payload = json.dumps(
        {"query": query},
        separators=(",", ":"),
        ensure_ascii=False,
    )

    timestamp = str(int(time.time()))

    assinatura = hashlib.sha256(
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
            f"Signature={assinatura}"
        ),
        "Content-Type": "application/json",
    }

    ultimo_erro = None

    for tentativa in range(TENTATIVAS_SHOPEE):
        try:
            resposta = HTTP.post(
                SHOPEE_URL,
                data=payload.encode("utf-8"),
                headers=headers,
                timeout=(
                    TIMEOUT_CONEXAO,
                    TIMEOUT_RESPOSTA,
                ),
            )

            if (
                resposta.status_code == 429
                or resposta.status_code >= 500
            ):
                ultimo_erro = RuntimeError(
                    f"HTTP {resposta.status_code}"
                )

                if tentativa < TENTATIVAS_SHOPEE - 1:
                    time.sleep(2 ** tentativa)
                    continue

                break

            resposta.raise_for_status()
            dados = resposta.json()

            erros = dados.get("errors") or []

            if erros:
                mensagem = json.dumps(
                    erros,
                    ensure_ascii=False,
                )

                # System Error não é repetido várias vezes.
                if "System Error" in mensagem:
                    raise RuntimeError(
                        "A Shopee retornou um erro interno."
                    )

                raise RuntimeError(
                    "Erro GraphQL: " + mensagem[:700]
                )

            return dados

        except (
            requests.RequestException,
            ValueError,
            RuntimeError,
        ) as erro:

            ultimo_erro = erro

            if tentativa < TENTATIVAS_SHOPEE - 1:
                time.sleep(2 ** tentativa)

    raise RuntimeError(
        f"Consulta não concluída: {ultimo_erro}"
    )


def buscar_produtos(palavra):
    palavra_json = json.dumps(
        palavra,
        ensure_ascii=False,
    )

    query = f"""
    {{
      productOfferV2(
        keyword: {palavra_json},
        listType: 0,
        sortType: {ORDENACAO_PRINCIPAL},
        page: 1,
        limit: {LIMITE_PRODUTOS}
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

    resposta = requisicao_shopee(query)

    resultado = (
        resposta.get("data", {})
        .get("productOfferV2", {})
    ) or {}

    return resultado.get("nodes") or []


# ============================================================
# ANÁLISE DOS PRODUTOS
# ============================================================

def comissao_estimada(produto):
    valor = decimal(produto.get("commission"))

    if valor > 0:
        return valor

    return (
        decimal(produto.get("priceMin"))
        * decimal(produto.get("commissionRate"))
    )


def produto_elegivel(produto, categoria):
    config = CATEGORIAS[categoria]

    preco = decimal(produto.get("priceMin"))
    vendas = inteiro(produto.get("sales"))
    avaliacao = decimal(produto.get("ratingStar"))
    taxa = decimal(produto.get("commissionRate"))

    if preco <= 0:
        return False

    if not config["min"] <= preco <= config["max"]:
        return False

    if vendas < MIN_VENDAS:
        return False

    if avaliacao > 0 and avaliacao < MIN_AVALIACAO:
        return False

    # Filtro flexível para não eliminar produtos
    # com boa comissão absoluta e histórico de vendas.
    if (
        taxa < Decimal("0.08")
        and comissao_estimada(produto) < Decimal("5")
    ):
        return False

    return True


def pontuar_produtos(produtos):
    maior_venda = max(
        [inteiro(p.get("sales")) for p in produtos] or [1]
    )

    maior_comissao = max(
        [
            float(comissao_estimada(p))
            for p in produtos
        ] or [1]
    )

    termos_visuais = (
        "quadro", "espelho", "luminaria", "abajur",
        "pelucia", "funko", "fruteira", "mesa",
        "bandeja", "pote", "organizador",
        "streetwear", "moletom", "camiseta",
    )

    for produto in produtos:
        vendas = inteiro(produto.get("sales"))
        comissao = float(comissao_estimada(produto))
        avaliacao = decimal(produto.get("ratingStar"))

        demanda = (
            math.log1p(vendas)
            / math.log1p(max(maior_venda, 1))
        ) * 35

        rentabilidade = (
            math.log1p(comissao)
            / math.log1p(max(maior_comissao, 1))
        ) * 35

        qualidade = (
            float(min(avaliacao, Decimal("5"))) / 5 * 15
            if avaliacao > 0 else 7.5
        )

        texto = (
            produto["_categoria"]
            + " "
            + produto["_palavra"]
        ).lower()

        conteudo = (
            15
            if any(t in texto for t in termos_visuais)
            else 10
        )

        produto["_pontuacao"] = round(
            demanda + rentabilidade + qualidade + conteudo,
            1,
        )

        produto["_comissao"] = comissao
        produto["_preco"] = decimal(produto.get("priceMin"))
        produto["_vendas"] = vendas
        produto["_avaliacao"] = avaliacao

    return sorted(
        produtos,
        key=lambda p: p["_pontuacao"],
        reverse=True,
    )


# ============================================================
# GARIMPO OTIMIZADO
# ============================================================

def coletar_candidatos(historico, progresso=None):
    encontrados = {}
    erros = []
    total_buscas = sum(
        len(c["palavras"])
        for c in CATEGORIAS.values()
    )

    busca_atual = 0

    for categoria, config in CATEGORIAS.items():
        for palavra in config["palavras"]:

            busca_atual += 1

            try:
                produtos = buscar_produtos(palavra)

                for produto in produtos:
                    item_id = str(
                        produto.get("itemId") or ""
                    )

                    if not item_id or item_id in historico:
                        continue

                    if not produto_elegivel(
                        produto,
                        categoria,
                    ):
                        continue

                    produto["_categoria"] = categoria
                    produto["_palavra"] = palavra

                    anterior = encontrados.get(item_id)

                    if (
                        anterior is None
                        or comissao_estimada(produto)
                        > comissao_estimada(anterior)
                    ):
                        encontrados[item_id] = produto

            except Exception as erro:
                logger.warning(
                    "Busca ignorada: %s / %s | %s",
                    categoria,
                    palavra,
                    erro,
                )

                erros.append({
                    "categoria": categoria,
                    "palavra": palavra,
                    "erro": str(erro)[:150],
                })

            if progresso:
                progresso(
                    busca_atual,
                    total_buscas,
                    categoria,
                    len(encontrados),
                )

            time.sleep(INTERVALO_BUSCAS)

    return list(encontrados.values()), erros


def selecionar_produtos(produtos):
    selecionados = []
    ids = set()

    contagem = {
        categoria: 0
        for categoria in CATEGORIAS
    }

    # Primeira etapa: cumprir as quotas.
    for categoria, config in CATEGORIAS.items():
        candidatos = [
            p for p in produtos
            if p["_categoria"] == categoria
        ]

        for produto in candidatos:
            item_id = str(produto.get("itemId"))

            if contagem[categoria] >= config["quota"]:
                break

            if item_id not in ids:
                selecionados.append(produto)
                ids.add(item_id)
                contagem[categoria] += 1

    # Segunda etapa: preencher vagas restantes.
    for produto in produtos:
        if len(selecionados) >= MAX_RESULTADOS:
            break

        item_id = str(produto.get("itemId"))
        categoria = produto["_categoria"]

        if item_id in ids:
            continue

        if contagem[categoria] >= 5:
            continue

        selecionados.append(produto)
        ids.add(item_id)
        contagem[categoria] += 1

    return sorted(
        selecionados,
        key=lambda p: p["_pontuacao"],
        reverse=True,
    )


def formatar_produto(produto, numero):
    nome = html.escape(
        str(produto.get("productName") or "Produto")[:160]
    )

    loja = html.escape(
        str(produto.get("shopName") or "Não informada")[:70]
    )

    categoria = html.escape(produto["_categoria"])

    link = str(produto.get("offerLink") or "").strip()

    if link.startswith(("https://", "http://")):
        link_texto = (
            f'<a href="{html.escape(link, quote=True)}">'
            "🛒 Ver produto na Shopee</a>"
        )
    else:
        link_texto = "🔗 Link indisponível"

    avaliacao = produto["_avaliacao"]

    avaliacao_texto = (
        f"{avaliacao:.1f}/5"
        if avaliacao > 0
        else "Não informada"
    )

    taxa = (
        decimal(produto.get("commissionRate")) * 100
    )

    return (
        f"🏷️ <b>#{numero} — {nome}</b>\n\n"
        f"📂 {categoria}\n"
        f"💵 Preço: <b>{dinheiro(produto['_preco'])}</b>\n"
        f"💰 Comissão estimada: "
        f"<b>{dinheiro(produto['_comissao'])}</b>\n"
        f"📈 Taxa informada: {taxa:.1f}%\n"
        f"🛍️ Vendas informadas: {produto['_vendas']:,}\n"
        f"⭐ Avaliação: {avaliacao_texto}\n"
        f"🏪 Loja: {loja}\n"
        f"🎯 Pontuação interna: {produto['_pontuacao']}/100\n"
        f"👥 Concorrência entre afiliados: não disponível\n\n"
        f"{link_texto}"
    )


async def enviar_produto(bot, produto, numero):
    legenda = formatar_produto(produto, numero)
    imagem = str(produto.get("imageUrl") or "").strip()

    try:
        if imagem.startswith(("https://", "http://")):
            await bot.send_photo(
                chat_id=CHAT_ID,
                photo=imagem,
                caption=legenda[:1024],
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=legenda,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        return True

    except RetryAfter as erro:
        await asyncio.sleep(float(erro.retry_after) + 1)

        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=legenda,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return True

        except TelegramError:
            logger.exception("Falha no reenvio do produto.")
            return False

    except TelegramError:
        logger.exception("Falha no envio do produto.")
        return False


async def atualizar_progresso(
    mensagem,
    atual,
    total,
    categoria,
    encontrados,
):
    porcentagem = int(atual / max(total, 1) * 100)

    barras_preenchidas = int(porcentagem / 10)

    barra = (
        "🟩" * barras_preenchidas
        + "⬜" * (10 - barras_preenchidas)
    )

    texto = (
        "🔎 <b>Garimpo da LootZone</b>\n\n"
        f"{barra} {porcentagem}%\n\n"
        f"📂 Categoria: {html.escape(categoria)}\n"
        f"🔍 Buscas concluídas: {atual}/{total}\n"
        f"📦 Produtos encontrados: {encontrados}\n\n"
        "Estou analisando as oportunidades..."
    )

    try:
        await mensagem.edit_text(
            texto,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass


async def garimpo(update, context):
    if not await verificar_acesso(update):
        return

    if LOCK_GARIMPO.locked():
        await update.effective_message.reply_text(
            "⏳ Já existe um garimpo em andamento."
        )
        return

    async with LOCK_GARIMPO:
        mensagem = await update.effective_message.reply_text(
            "🚀 <b>Garimpo iniciado!</b>\n\n"
            "Estou procurando oportunidades para a LootZone.",
            parse_mode=ParseMode.HTML,
        )

        historico = await carregar_historico()

        loop = asyncio.get_running_loop()

        def progresso(atual, total, categoria, encontrados):
            asyncio.run_coroutine_threadsafe(
                atualizar_progresso(
                    mensagem,
                    atual,
                    total,
                    categoria,
                    encontrados,
                ),
                loop,
            )

        candidatos, erros = await asyncio.to_thread(
            coletar_candidatos,
            historico,
            progresso,
        )

        if not candidatos:
            texto = (
                "🔎 <b>Garimpo finalizado</b>\n\n"
                "Não encontrei produtos que passaram pelos filtros.\n\n"
                "Tente novamente mais tarde."
            )

            if erros:
                texto += (
                    f"\n⚠️ Consultas com erro: {len(erros)}"
                )

            await mensagem.edit_text(
                texto,
                parse_mode=ParseMode.HTML,
            )
            return

        classificados = pontuar_produtos(candidatos)
        selecionados = selecionar_produtos(classificados)

        enviados = []
        quantidade = 0

        for numero, produto in enumerate(selecionados, 1):
            sucesso = await enviar_produto(
                context.bot,
                produto,
                numero,
            )

            if sucesso:
                quantidade += 1
                enviados.append(str(produto["itemId"]))

            await asyncio.sleep(0.4)

        if enviados:
            await marcar_como_visto(enviados)

        resumo = (
            "✅ <b>Garimpo concluído!</b>\n\n"
            f"🔍 Produtos encontrados: <b>{len(candidatos)}</b>\n"
            f"🏆 Produtos selecionados: <b>{len(selecionados)}</b>\n"
            f"📨 Produtos enviados: <b>{quantidade}</b>\n"
            f"⚠️ Consultas com erro: <b>{len(erros)}</b>\n\n"
            f"🗂️ Histórico de repetição: {HISTORICO_DIAS} dias\n\n"
            "💡 A pontuação é uma classificação interna. "
            "A API não informa quantos afiliados divulgam cada produto."
        )

        await mensagem.edit_text(
            resumo,
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# RELATÓRIOS DE CONVERSÃO
# ============================================================

STATUS_PT = {
    "UNPAID": "Não pagos",
    "PENDING": "Aguardando confirmação",
    "COMPLETED": "Concluídos",
    "CANCELLED": "Cancelados",
    "UNKNOWN": "Não identificados",
}


def buscar_conversoes(inicio, fim, limite=100, max_paginas=10):
    registros = []
    scroll_id = None

    for _ in range(max_paginas):
        scroll = (
            f", scrollId: {json.dumps(scroll_id)}"
            if scroll_id else ""
        )

        query = f"""
        {{
          conversionReport(
            purchaseTimeStart: {inicio},
            purchaseTimeEnd: {fim},
            limit: {limite}{scroll}
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
            pageInfo {{
              hasNextPage
              scrollId
            }}
          }}
        }}
        """

        resposta = requisicao_shopee(query)

        relatorio = (
            resposta.get("data", {})
            .get("conversionReport", {})
        ) or {}

        registros.extend(relatorio.get("nodes") or [])

        info = relatorio.get("pageInfo") or {}

        if not info.get("hasNextPage"):
            return registros

        scroll_id = info.get("scrollId")

        if not scroll_id:
            raise RuntimeError(
                "A Shopee não retornou o identificador da próxima página."
            )

    raise RuntimeError(
        "O relatório ultrapassou o limite de páginas."
    )


def resumir_conversoes(registros):
    pedidos = {}
    produtos = {}
    conversoes_vistas = set()
    comissao_total = Decimal("0")

    status = {
        "UNPAID": 0,
        "PENDING": 0,
        "COMPLETED": 0,
        "CANCELLED": 0,
        "UNKNOWN": 0,
    }

    for registro in registros:
        conversao_id = str(
            registro.get("conversionId") or ""
        )

        if (
            conversao_id
            and conversao_id in conversoes_vistas
        ):
            continue

        if conversao_id:
            conversoes_vistas.add(conversao_id)

        comissao_total += decimal(
            registro.get("netCommission")
        )

        for pedido in registro.get("orders") or []:
            pedido_id = str(pedido.get("orderId") or "")

            situacao = str(
                pedido.get("orderStatus") or "UNKNOWN"
            ).upper()

            if situacao not in status:
                situacao = "UNKNOWN"

            if pedido_id and pedido_id not in pedidos:
                pedidos[pedido_id] = situacao
                status[situacao] += 1

            for item in pedido.get("items") or []:
                item_id = str(
                    item.get("itemId")
                    or item.get("itemName")
                    or "Produto"
                )

                nome = str(
                    item.get("itemName") or "Produto"
                )

                if item_id not in produtos:
                    produtos[item_id] = {
                        "nome": nome,
                        "quantidade": 0,
                        "comissao": Decimal("0"),
                    }

                produtos[item_id]["quantidade"] += inteiro(
                    item.get("qty"),
                    1,
                )

                produtos[item_id]["comissao"] += decimal(
                    item.get("itemTotalCommission")
                )

    top = sorted(
        produtos.values(),
        key=lambda item: item["comissao"],
        reverse=True,
    )[:5]

    return {
        "pedidos": len(pedidos),
        "comissao": comissao_total,
        "status": status,
        "top": top,
    }


def montar_relatorio(titulo, registros, inicio, fim):
    resumo = resumir_conversoes(registros)

    linhas = [
        f"📊 <b>{html.escape(titulo)}</b>",
        f"📅 {inicio:%d/%m/%Y} até {fim:%d/%m/%Y}",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "💰 <b>RESULTADO</b>",
        "",
        f"🛒 Pedidos registrados: <b>{resumo['pedidos']}</b>",
        (
            "💵 Comissão informada pela Shopee: "
            f"<b>{dinheiro(resumo['comissao'])}</b>"
        ),
        "",
        "━━━━━━━━━━━━━━━━━━",
        "📦 <b>SITUAÇÃO DOS PEDIDOS</b>",
        "",
    ]

    for chave, nome in STATUS_PT.items():
        linhas.append(
            f"• {nome}: <b>{resumo['status'][chave]}</b>"
        )

    linhas.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🏆 <b>PRODUTOS EM DESTAQUE</b>",
        "",
    ])

    if resumo["top"]:
        for numero, item in enumerate(resumo["top"], 1):
            nome = html.escape(item["nome"][:75])

            linhas.append(
                f"<b>{numero}. {nome}</b>\n"
                f"   📦 {item['quantidade']} un. "
                f"• 💰 {dinheiro(item['comissao'])}"
            )
            linhas.append("")
    else:
        linhas.append("Nenhum produto encontrado neste período.")

    linhas.extend([
        "━━━━━━━━━━━━━━━━━━",
        "ℹ️ <i>Comissões informadas pela Shopee podem "
        "estar sujeitas à validação.</i>",
    ])

    texto = "\n".join(linhas)

    if len(texto) > 3900:
        texto = texto[:3800] + (
            "\n\n<i>Relatório resumido para caber na mensagem.</i>"
        )

    return texto


async def enviar_relatorio(update, titulo, inicio, fim):
    mensagem = await update.effective_message.reply_text(
        "📊 Preparando seu relatório..."
    )

    try:
        registros = await asyncio.to_thread(
            buscar_conversoes,
            int(inicio.timestamp()),
            int(fim.timestamp()),
        )

        texto = montar_relatorio(
            titulo,
            registros,
            inicio,
            fim,
        )

        await mensagem.edit_text(
            texto,
            parse_mode=ParseMode.HTML,
        )

    except Exception as erro:
        logger.exception("Falha no relatório.")

        await mensagem.edit_text(
            "⚠️ Não consegui consultar o relatório.\n\n"
            + html.escape(str(erro)[:500])
        )


# ============================================================
# COMANDOS DE RELATÓRIO
# ============================================================

async def ontem(update, context):
    if not await verificar_acesso(update):
        return

    dia = agora().date() - timedelta(days=1)

    inicio = datetime.combine(dia, dt_time.min, FUSO)
    fim = datetime.combine(dia, dt_time(23, 59, 59), FUSO)

    await enviar_relatorio(
        update,
        "Resumo de ontem",
        inicio,
        fim,
    )


async def mes(update, context):
    if not await verificar_acesso(update):
        return

    hoje = agora().date()

    if hoje.day == 1:
        await update.effective_message.reply_text(
            "Ainda não há dias anteriores neste mês."
        )
        return

    inicio = datetime.combine(
        hoje.replace(day=1),
        dt_time.min,
        FUSO,
    )

    fim = datetime.combine(
        hoje - timedelta(days=1),
        dt_time(23, 59, 59),
        FUSO,
    )

    await enviar_relatorio(
        update,
        "Resumo do mês",
        inicio,
        fim,
    )


async def periodo(update, context):
    if not await verificar_acesso(update):
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "📅 Use assim:\n"
            "/periodo 01/11/2026 22/11/2026"
        )
        return

    try:
        data_inicio = datetime.strptime(
            context.args[0],
            "%d/%m/%Y",
        ).date()

        data_fim = datetime.strptime(
            context.args[1],
            "%d/%m/%Y",
        ).date()

        if (
            data_fim < data_inicio
            or data_fim > agora().date()
        ):
            raise ValueError

        if (data_fim - data_inicio).days > 90:
            await update.effective_message.reply_text(
                "O período máximo é de 90 dias."
            )
            return

        inicio = datetime.combine(
            data_inicio,
            dt_time.min,
            FUSO,
        )

        fim = datetime.combine(
            data_fim,
            dt_time(23, 59, 59),
            FUSO,
        )

    except ValueError:
        await update.effective_message.reply_text(
            "Datas inválidas. Exemplo:\n"
            "/periodo 01/11/2026 22/11/2026"
        )
        return

    await enviar_relatorio(
        update,
        "Resumo do período",
        inicio,
        fim,
    )


async def relatorio_automatico(context):
    dia = agora().date() - timedelta(days=1)

    inicio = datetime.combine(dia, dt_time.min, FUSO)
    fim = datetime.combine(dia, dt_time(23, 59, 59), FUSO)

    try:
        registros = await asyncio.to_thread(
            buscar_conversoes,
            int(inicio.timestamp()),
            int(fim.timestamp()),
        )

        texto = montar_relatorio(
            "Resumo diário da LootZone",
            registros,
            inicio,
            fim,
        )

        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=texto,
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        logger.exception("Erro no relatório automático.")

        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                "⚠️ Não consegui gerar o resumo automático "
                "de ontem. Verifique os registros do bot."
            ),
        )


# ============================================================
# MENU
# ============================================================

TEXTO_INICIO = (
    "👋 <b>Bem-vindo ao painel da LootZone!</b>\n\n"
    "Aqui você acompanha seus resultados e encontra "
    "novas oportunidades para divulgar.\n\n"
    "📊 <b>SEUS RESULTADOS</b>\n"
    "/ontem — resumo de ontem\n"
    "/mes — resumo do mês\n"
    "/periodo — consultar datas específicas\n\n"
    "🔎 <b>OPORTUNIDADES</b>\n"
    "/garimpo — procurar produtos\n\n"
    "⚙️ <b>BOT</b>\n"
    "/status — verificar funcionamento\n"
    "/limpar — limpar histórico de produtos\n"
    "/ajuda — ver este menu"
)


async def inicio(update, context):
    if not await verificar_acesso(update):
        return

    await update.effective_message.reply_text(
        TEXTO_INICIO,
        parse_mode=ParseMode.HTML,
    )


async def ajuda(update, context):
    await inicio(update, context)


async def limpar(update, context):
    if not await verificar_acesso(update):
        return

    await limpar_historico()

    await update.effective_message.reply_text(
        "🧹 Histórico de produtos limpo!\n\n"
        "O próximo garimpo poderá encontrar produtos "
        "que já apareceram anteriormente."
    )


async def status(update, context):
    if not await verificar_acesso(update):
        return

    historico = await carregar_historico()

    await update.effective_message.reply_text(
        "🟢 <b>LootZone Bot</b>\n\n"
        "Situação: em execução\n"
        f"📦 Produtos no histórico: {len(historico)}\n"
        f"🗂️ Arquivo: {SEEN_FILE}\n"
        f"🔎 Categorias de busca: {len(CATEGORIAS)}\n"
        f"⏰ Resumo diário: {HORARIO_RELATORIO:02d}:00\n"
        "🇧🇷 Horário de Brasília\n\n"
        "O histórico depende de um Volume persistente "
        "configurado no Railway.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# INICIALIZAÇÃO
# ============================================================

async def ao_iniciar(application):
    if application.job_queue:
        application.job_queue.run_daily(
            relatorio_automatico,
            time=dt_time(
                hour=HORARIO_RELATORIO,
                minute=0,
                tzinfo=FUSO,
            ),
            name="relatorio_diario_lootzone",
        )

        logger.info(
            "Resumo diário agendado para %02d:00.",
            HORARIO_RELATORIO,
        )
    else:
        logger.error(
            "JobQueue indisponível. "
            "Relatório automático não agendado."
        )


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(ao_iniciar)
        .build()
    )

    app.add_handler(CommandHandler("start", inicio))
    app.add_handler(CommandHandler("inicio", inicio))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("ontem", ontem))
    app.add_handler(CommandHandler("mes", mes))
    app.add_handler(CommandHandler("periodo", periodo))
    app.add_handler(CommandHandler("garimpo", garimpo))
    app.add_handler(CommandHandler("limpar", limpar))
    app.add_handler(CommandHandler("status", status))

    logger.info("LootZone Bot iniciado.")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
