#!/usr/bin/env python3
"""
CornerBot - Versão profissional, assíncrona e robusta.
Substitua o main.py existente por este arquivo.

Dependências:
    pip install aiohttp python-telegram-bot==20.8

Configure (opcional) via variáveis de ambiente:
    API_KEY, TELEGRAM_TOKEN, CHAT_ID

Ou edite diretamente as variáveis abaixo.
"""

import os
import asyncio
import logging
import math
import random
from typing import Dict, List, Optional

import aiohttp
from telegram import Bot
from telegram.error import TelegramError

# -------------------------
# Configurações (edite se quiser)
# -------------------------
API_KEY = os.getenv("API_KEY", "74e372055593a55e7cbcc79df1097907")
BASE = "https://v3.football.api-sports.io"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8239858396:AAEohsJJcgJwaCC4ioG1ZEek4HesI3NhwQ8")
CHAT_ID = int(os.getenv("CHAT_ID", "441778236"))

# Ajustáveis
POLL_INTERVAL = 20                # segundos entre verificações
CONCURRENT_REQUESTS = 6           # concorrência para chamadas à API
STAT_TTL = 8                      # segundos de cache para estatísticas por jogo
REQUEST_TIMEOUT = 12              # timeout para requests HTTP
MAX_RETRIES = 3                   # tentativas em chamadas externas
BACKOFF_FACTOR = 1.2              # fator para backoff exponencial

LOG_LEVEL = logging.INFO

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cornerbot")

# -------------------------
# Telegram bot (sync client que possui métodos awaitable via asyncio)
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)

# -------------------------
# Pequeno utilitário de escape para HTML (evita caracteres perigosos)
# -------------------------
def esc_html(s: str) -> str:
    if s is None:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))

# -------------------------
# Cache simples de estatísticas (fixture_id -> (timestamp, corners_total))
# -------------------------
class StatsCache:
    def __init__(self):
        self._cache: Dict[int, (float, int)] = {}

    def get(self, fixture_id: int) -> Optional[int]:
        entry = self._cache.get(fixture_id)
        if not entry:
            return None
        ts, val = entry
        now = asyncio.get_event_loop().time()
        if (now - ts) > STAT_TTL:
            del self._cache[fixture_id]
            return None
        return val

    def set(self, fixture_id: int, value: int):
        self._cache[fixture_id] = (asyncio.get_event_loop().time(), value)


stats_cache = StatsCache()


# -------------------------
# API Client assíncrono
# -------------------------
class ApiClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = session
        self.headers = {"x-apisports-key": api_key}
        self.semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

    async def _fetch_json(self, url: str, params: dict = None) -> Optional[dict]:
        """
        Requisição com retries e backoff.
        Retorna dict JSON ou None em erro.
        """
        params = params or {}
        attempt = 0
        while attempt <= MAX_RETRIES:
            try:
                async with self.semaphore:
                    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    async with self.session.get(url, headers=self.headers, params=params, timeout=timeout) as resp:
                        # Resposta 429 ou 5xx => tratar como exceção para retry
                        if resp.status >= 500 or resp.status == 429:
                            text = await resp.text()
                            raise aiohttp.ClientResponseError(
                                status=resp.status, request_info=resp.request_info, history=resp.history,
                                message=f"HTTP {resp.status} - {text[:200]}"
                            )
                        resp.raise_for_status()
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                attempt += 1
                if attempt > MAX_RETRIES:
                    logger.exception("Erro definitivo ao acessar %s (params=%s): %s", url, params, e)
                    return None
                backoff = (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5)
                logger.warning("Erro ao acessar %s (tentativa %s/%s). Backoff %.2fs. Erro: %s",
                               url, attempt, MAX_RETRIES, backoff, e)
                await asyncio.sleep(backoff)
        return None

    async def get_live(self) -> List[dict]:
        url = f"{BASE}/fixtures"
        params = {"live": "all"}
        j = await self._fetch_json(url, params=params)
        if not j:
            return []
        return j.get("response", []) or []

    async def get_corners_for_fixture(self, fixture_id: int) -> int:
        """
        Chama /fixtures/statistics?fixture=fixture_id e soma os cantos.
        Usa cache para reduzir chamadas.
        """
        cached = stats_cache.get(fixture_id)
        if cached is not None:
            return cached

        url = f"{BASE}/fixtures/statistics"
        params = {"fixture": fixture_id}
        j = await self._fetch_json(url, params=params)
        if not j:
            # Em caso de erro, devolve 0 (para não travar o loop)
            return 0

        total = 0
        resp = j.get("response", []) or []
        # A resposta costuma ser lista com 2 objetos (home, away)
        for team in resp:
            # team pode ter "statistics": [...]
            stats = team.get("statistics") or []
            # Percorre itens de estatísticas
            for s in stats:
                typ = (s.get("type") or "").lower()
                # aceita variações como "Corner Kicks", "Corners", "Corner"
                if "corner" in typ:
                    try:
                        val = s.get("value")
                        if val is None:
                            continue
                        total += int(val)
                    except Exception:
                        # ignora valores estranhos
                        continue

        # salva em cache
        stats_cache.set(fixture_id, total)
        return total


# -------------------------
# Regras (mantive exatamente as 7 checagens)
# -------------------------
def apply_rules_from_values(minute: Optional[int], corners: int) -> List[str]:
    checks: List[str] = []
    if minute is None:
        return checks

    if minute >= 20 and corners >= 5:
        checks.append("1️⃣ Over HT > 4.5")

    if minute >= 60 and corners >= 9:
        checks.append("2️⃣ Over FT > 9.5")

    if minute >= 10 and corners >= 3:
        checks.append("3️⃣ Próximo Escanteio")

    if minute >= 30 and corners >= 6:
        checks.append("4️⃣ AH asiático cantos")

    if minute >= 25 and corners >= 4:
        checks.append("5️⃣ Cantos por equipe")

    if minute >= 35 and corners >= 7:
        checks.append("6️⃣ Ambos Times Cantos")

    if minute >= 15 and corners >= 4:
        checks.append("7️⃣ Pressão para próximo canto")

    return checks


# -------------------------
# Helpers Telegram (envia/edita com proteção)
# -------------------------
async def safe_send(text: str) -> Optional[dict]:
    """
    Envia mensagem e retorna obj Message (ou None em erro).
    Usa parse_mode HTML (mais simples para escapar).
    """
    try:
        # Envia em HTML; certifique-se de escapar antes de montar a string
        msg = await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
        return msg
    except TelegramError as e:
        logger.exception("Erro ao enviar mensagem Telegram: %s", e)
        return None
    except Exception as e:
        logger.exception("Erro inesperado ao enviar mensagem Telegram: %s", e)
        return None


async def safe_edit(message_id: int, text: str) -> bool:
    try:
        await bot.edit_message_text(chat_id=CHAT_ID, message_id=message_id, text=text, parse_mode="HTML")
        return True
    except TelegramError as e:
        # Pode ser: mensagem muito antiga, já removida, limite de edição, etc.
        logger.warning("Falha ao editar mensagem %s: %s", message_id, e)
        return False
    except Exception as e:
        logger.exception("Erro inesperado ao editar mensagem %s: %s", message_id, e)
        return False


# -------------------------
# Main loop
# -------------------------
async def main_loop():
    logger.info("CornerBot INICIADO - monitorando jogos ao vivo...")
    sent: Dict[int, int] = {}  # fixture_id -> message_id

    # Sessão HTTP compartilhada
    async with aiohttp.ClientSession() as session:
        api = ApiClient(session, API_KEY)

        # Primeiro aviso de inicialização (não mais de uma vez)
        startup_text = "<b>🔥 CornerBot INICIADO</b> – monitorando jogos ao vivo..."
        await safe_send(startup_text)

        while True:
            try:
                matches = await api.get_live()
                if not matches:
                    logger.debug("Nenhum jogo ao vivo no momento.")
                # processa cada partida
                for m in matches:
                    try:
                        # pega fixture id e status
                        fid = m.get("fixture", {}).get("id")
                        if fid is None:
                            continue

                        # obtém minuto de forma segura
                        minute_raw = m.get("fixture", {}).get("status", {}).get("elapsed")
                        try:
                            minute = int(minute_raw) if minute_raw is not None else None
                        except Exception:
                            minute = None

                        # pega cantos via endpoint statistics (com cache)
                        corners = await api.get_corners_for_fixture(fid)

                        # calcula regras
                        rules_hit = apply_rules_from_values(minute, corners)

                        # se regras ativas e não enviado, envia
                        if rules_hit and fid not in sent:
                            home = esc_html(m.get("teams", {}).get("home", {}).get("name", "—"))
                            away = esc_html(m.get("teams", {}).get("away", {}).get("name", "—"))
                            minute_display = str(minute) if minute is not None else "—"
                            text = (
                                f"<b>⚽ Entrada Detectada!</b>\n"
                                f"📌 Jogo: {home} x {away}\n"
                                f"⏱ Minuto: {minute_display}\n"
                                f"🚩 Cantos: {corners}\n\n"
                                f"📊 Estratégias ativadas:\n" + "\n".join(esc_html(r) for r in rules_hit)
                            )
                            msg = await safe_send(text)
                            if msg:
                                sent[fid] = msg.message_id
                                logger.info("Entrada enviada (fixture=%s, message_id=%s)", fid, msg.message_id)

                        # verifica se já enviamos e o jogo terminou -> editar mensagem
                        if fid in sent:
                            status_short = m.get("fixture", {}).get("status", {}).get("short")
                            if status_short == "FT":
                                total_corners = await api.get_corners_for_fixture(fid)
                                tag = "✅ GREEN" if total_corners >= 10 else "❌ RED"
                                final_msg = (
                                    f"<b>🏁 Jogo finalizado</b>\n"
                                    f"Total de escanteios: {total_corners}\n"
                                    f"Resultado: {esc_html(tag)}"
                                )
                                edited = await safe_edit(sent[fid], final_msg)
                                if edited:
                                    logger.info("Mensagem final editada (fixture=%s).", fid)
                                else:
                                    logger.warning("Não foi possível editar mensagem final para fixture=%s.", fid)
                                # remove do dicionário (independente de ter editado)
                                try:
                                    del sent[fid]
                                except KeyError:
                                    pass

                    except Exception as e:
                        # erro isolado para essa partida não deve derrubar todo o loop
                        logger.exception("Erro processando partida: %s", e)

                # aguarda próximo ciclo
                await asyncio.sleep(POLL_INTERVAL)

            except Exception as e:
                # Erro no loop principal: loga e aguarda um pouco antes de continuar
                logger.exception("Erro no loop principal: %s", e)
                await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("CornerBot interrompido pelo usuário.")
    except Exception:
        logger.exception("CornerBot caiu por erro inesperado.")
