# -*- coding: utf-8 -*-
"""
Scrapling OSINT — microserviço POC para o Sherlock (Recepta Plus / RPM-02).

Objetivo: dar ao Sherlock dados VERIFICADOS que a web_search do Opus não consegue
confirmar bem (página renderizada + anti-bot). Foco do POC:
  - POST /scrape      -> raspa qualquer URL (texto limpo + extração por CSS).
  - POST /ads/meta    -> abre a Meta Ad Library e devolve o texto renderizado
                         + heurística de "anuncia / nº de anúncios ativos".
  - GET  /health      -> liveness.

Roda como serviço separado (Railway, imagem Docker com browsers). O n8n chama via
HTTP, igual já faz com o Gotenberg. NÃO roda dentro do n8n (que é JS).

Os fetchers do Scrapling (browser/anti-bot) são importados DE FORMA TARDIA dentro
de cada rota, pra este arquivo importar mesmo sem o extra [fetchers] instalado
(útil pra testar o parsing localmente). Em produção, instale scrapling[fetchers].
"""
import os
import re
from typing import Optional, Dict, List

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from scrapling.parser import Selector  # parser puro — não exige browsers

API_TOKEN = os.environ.get("SCRAPLING_TOKEN", "")  # segredo no env do Railway
MAX_TEXT = int(os.environ.get("SCRAPLING_MAX_TEXT", "12000"))  # corta texto longo

app = FastAPI(title="Scrapling OSINT — Sherlock", version="0.1.0")


# ----------------------------- auth -----------------------------
def _auth(x_api_key: Optional[str]):
    """Exige o header x-api-key == SCRAPLING_TOKEN (se o token estiver setado)."""
    if API_TOKEN and x_api_key != API_TOKEN:
        raise HTTPException(status_code=401, detail="x-api-key inválido")


# ----------------------- helpers de parsing ---------------------
# (testáveis sem rede: recebem um Selector já montado de HTML)
def texto_limpo(page: Selector, limite: int = MAX_TEXT) -> str:
    """Texto visível da página, sem script/style, colapsando espaços."""
    try:
        body = page.css_first("body") if hasattr(page, "css_first") else None
    except Exception:
        body = None
    alvo = body or page
    txt = alvo.get_all_text(ignore_tags=("script", "style", "noscript")) \
        if hasattr(alvo, "get_all_text") else alvo.text
    txt = re.sub(r"\s+", " ", str(txt or "")).strip()
    return txt[:limite]


def extrair_campos(page: Selector, css_map: Dict[str, str]) -> Dict[str, Optional[str]]:
    """Para cada {campo: seletor_css}, devolve o 1º texto encontrado (ou None)."""
    out: Dict[str, Optional[str]] = {}
    for campo, sel in (css_map or {}).items():
        try:
            val = page.css(sel).get()
            out[campo] = re.sub(r"\s+", " ", val).strip() if val else None
        except Exception:
            out[campo] = None
    return out


def coletar_links(page: Selector, limite: int = 40) -> List[str]:
    try:
        hrefs = page.css("a::attr(href)").getall()
    except Exception:
        hrefs = []
    vistos, out = set(), []
    for h in hrefs:
        h = (h or "").strip()
        if h and h not in vistos and not h.startswith("javascript:"):
            vistos.add(h)
            out.append(h)
        if len(out) >= limite:
            break
    return out


def heuristica_meta_ads(texto: str) -> Dict[str, object]:
    """
    Best-effort sobre o texto renderizado da Meta Ad Library.
    Ela escreve algo como "~12 resultados" / "About 12 results".
    Não é fonte de verdade — o texto vai junto pro Opus decidir.
    """
    t = texto.lower()
    m = re.search(r"~?\s*([\d\.]+)\s*(resultado|resultados|result|results|an[uú]ncio)", t)
    ativos = None
    if m:
        try:
            ativos = int(m.group(1).replace(".", ""))
        except ValueError:
            ativos = None
    sem_anuncio = any(s in t for s in [
        "0 resultado", "nenhum resultado", "no results", "não encontramos anúncios",
        "no ads", "we couldn't find", "sem resultados",
    ])
    if sem_anuncio:
        anuncia = False
    elif ativos is not None and ativos > 0:
        anuncia = True
    else:
        anuncia = None  # indefinido -> Sherlock usa "A confirmar"
    return {"anuncia": anuncia, "ativos": ativos}


# --------------------------- schemas ----------------------------
class ScrapeIn(BaseModel):
    url: str
    render: bool = False                 # True => browser headless (JS + anti-bot)
    css: Optional[Dict[str, str]] = None # {campo: seletor}
    incluir_texto: bool = True
    incluir_links: bool = False


class MetaAdsIn(BaseModel):
    nome: str                            # nome da farmácia concorrente
    pais: str = "BR"


class MetaAdsBatchIn(BaseModel):
    nomes: List[str]                     # nomes dos concorrentes (e/ou da própria)
    pais: str = "BR"


# ----------------------------- fetch ----------------------------
def _fetch(url: str, render: bool):
    """Importa o fetcher só aqui (precisa de scrapling[fetchers] em produção)."""
    if render:
        from scrapling.fetchers import StealthyFetcher
        return StealthyFetcher.fetch(
            url, headless=True, network_idle=True, block_images=True, timeout=45000
        )
    from scrapling.fetchers import Fetcher
    return Fetcher.get(url, stealthy_headers=True, timeout=30000)


# ----------------------------- rotas ----------------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "scrapling-osint", "version": app.version}


@app.post("/scrape")
def scrape(body: ScrapeIn, x_api_key: Optional[str] = Header(default=None)):
    _auth(x_api_key)
    try:
        page = _fetch(body.url, body.render)
    except Exception as e:
        return {"ok": False, "url": body.url, "erro": f"fetch falhou: {e}"}
    resp = {"ok": True, "url": body.url, "status": getattr(page, "status", None)}
    if body.css:
        resp["campos"] = extrair_campos(page, body.css)
    if body.incluir_texto:
        resp["texto"] = texto_limpo(page)
    if body.incluir_links:
        resp["links"] = coletar_links(page)
    return resp


def _meta_ads_one(nome: str, pais: str, incluir_texto: bool = True) -> Dict[str, object]:
    """Núcleo compartilhado: abre a Meta Ad Library de UM termo e devolve o resultado."""
    pais = (pais or "BR").upper()
    termo = re.sub(r"\s+", "%20", nome.strip())
    url = (
        "https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country={pais}"
        f"&q={termo}&search_type=keyword_unordered&media_type=all"
    )
    try:
        page = _fetch(url, render=True)  # Ad Library é JS pesado -> sempre browser
    except Exception as e:
        return {"ok": False, "nome": nome, "url": url, "erro": f"fetch falhou: {e}"}
    texto = texto_limpo(page, limite=4000)
    heur = heuristica_meta_ads(texto)
    out = {"ok": True, "nome": nome, "url": url,
           "anuncia": heur["anuncia"], "ativos": heur["ativos"]}
    if incluir_texto:
        out["texto"] = texto
    return out


@app.post("/ads/meta")
def ads_meta(body: MetaAdsIn, x_api_key: Optional[str] = Header(default=None)):
    _auth(x_api_key)
    return _meta_ads_one(body.nome, body.pais, incluir_texto=True)


@app.post("/ads/meta/batch")
def ads_meta_batch(body: MetaAdsBatchIn, x_api_key: Optional[str] = Header(default=None)):
    """Vários concorrentes numa chamada só (o n8n manda os nomes do OSM de uma vez)."""
    _auth(x_api_key)
    resultados = [_meta_ads_one(n, body.pais, incluir_texto=False)
                  for n in (body.nomes or [])[:5] if n and n.strip()]
    return {"ok": True, "resultados": resultados}
