#!/usr/bin/env python3
import os
import asyncio
import logging
import random
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web
from telegram import Bot

# =========================================================
# CONFIGURAÇÕES
# =========================================================

API_KEY = os.getenv("API_KEY")
BASE = "https://v3.football.api-sports.io"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_ENV = os.getenv("CHAT_ID")

if not API_KEY:
    raise RuntimeError("API_KEY não definido no ambiente")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN não definido no ambiente")

if not CHAT_ID_ENV:
    raise RuntimeError("CHAT_ID não definido no ambiente")

CHAT_ID = int(CHAT_ID_ENV)

POLL_INTERVAL = 20
CONCURRENT_REQUESTS = 3
STAT_TTL = 15
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
BACKOFF_FACTOR = 1.5

LOG_LEVEL = logging.INFO
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cornerbot")

bot = Bot(token=TELEGRAM_TOKEN)

# =========================================================
# DATA CLASSES
# =========================================================

@dataclass
class BetSuggestion:
    bet_type: str
    side: Optional[str]
    reason: str
    odd: float
    corners_at_entry_home: int
    corners_at_entry_away: int
    predicted_next_corner: Optional[str] = None
    result: Optional[str] = None

@dataclass
class MatchData:
    fixture_id: int
    home_team: str
    away_team: str
    league: str
    message_id: Optional[int] = None
    entry_minute: Optional[int] = None
    corners_at_entry_home: int = 0
    corners_at_entry_away: int = 0
    suggestions: List[BetSuggestion] = field(default_factory=list)
    next_corner_after_entry: Optional[str] = None
    final_corners_home: int = 0
    final_corners_away: int = 0

# =========================================================
# UTIL
# =========================================================

def esc_html(s: str) -> str:
    if s is None:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# =========================================================
# CACHE
# =========================================================

class StatsCache:
    def __init__(self):
        self._cache: Dict[int, Tuple[float, Dict]] = {}

    def get(self, fixture_id: int) -> Optional[Dict]:
        entry = self._cache.get(fixture_id)
        if not entry:
            return None
        ts, val = entry
        if (asyncio.get_event_loop().time() - ts) > STAT_TTL:
            del self._cache[fixture_id]
            return None
        return val

    def set(self, fixture_id: int, value: Dict):
        self._cache[fixture_id] = (asyncio.get_event_loop().time(), value)

stats_cache = StatsCache()

# =========================================================
# API CLIENT
# =========================================================

class ApiClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = session
        self.headers = {"x-apisports-key": api_key}
        self.semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

    async def _fetch_json(self, url: str, params: dict = None) -> Optional[dict]:
        params = params or {}
        attempt = 0

        while attempt <= MAX_RETRIES:
            try:
                async with self.semaphore:
                    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    async with self.session.get(
                        url, headers=self.headers, params=params, timeout=timeout
                    ) as resp:

                        if resp.status in (429, 500, 502, 503):
                            text = await resp.text()
                            raise aiohttp.ClientError(f"HTTP {resp.status}: {text}")

                        resp.raise_for_status()
                        return await resp.json()

            except Exception as e:
                attempt += 1
                if attempt > MAX_RETRIES:
                    logger.error(f"Erro definitivo ao acessar {url}: {e}")
                    return None

                backoff = (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5)
                logger.warning(f"Tentativa {attempt}/{MAX_RETRIES} falhou. Backoff {backoff:.2f}s")
                await asyncio.sleep(backoff)

        return None

    async def get_live(self):
        url = f"{BASE}/fixtures"
        j = await self._fetch_json(url, {"live": "all"})
        if not j:
            return []
        return j.get("response", [])

    async def get_full_statistics(self, fixture_id: int):
        cached = stats_cache.get(fixture_id)
        if cached:
            return cached

        url = f"{BASE}/fixtures/statistics"
        j = await self._fetch_json(url, {"fixture": fixture_id})

        result = {"corners_home": 0, "corners_away": 0, "corners_total": 0}

        if not j:
            return result

        resp = j.get("response", [])
        if not resp or len(resp) < 2:
            return result

        try:
            home_stats = resp[0]["statistics"]
            away_stats = resp[1]["statistics"]
        except Exception:
            return result

        def get_value(stats, name):
            for s in stats:
                if name.lower() in s.get("type", "").lower():
                    try:
                        return int(str(s.get("value", 0)).replace("%", ""))
                    except Exception:
                        return 0
            return 0

        result["corners_home"] = get_value(home_stats, "corner")
        result["corners_away"] = get_value(away_stats, "corner")
        result["corners_total"] = result["corners_home"] + result["corners_away"]

        stats_cache.set(fixture_id, result)
        return result

# =========================================================
# TELEGRAM
# =========================================================

async def safe_send(text: str):
    try:
        return await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem: {e}")
        return None

async def safe_edit(message_id: int, text: str):
    try:
        await bot.edit_message_text(
            chat_id=CHAT_ID,
            message_id=message_id,
            text=text,
            parse_mode="HTML"
        )
        return True
    except Exception as e:
        logger.error(f"Erro ao editar mensagem: {e}")
        return False

# =========================================================
# REGRAS
# =========================================================

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

# =========================================================
# ANALISADOR
# =========================================================

class IntelligentAnalyzer:

    @staticmethod
    def generate_checklist(stats: Dict, minute: int) -> str:
        corners_total = stats["corners_total"]
        corners_home = stats["corners_home"]
        corners_away = stats["corners_away"]

        ritmo_5 = "Alto" if corners_total >= 3 else "Médio" if corners_total >= 2 else "Baixo"
        ritmo_10 = "Alto" if corners_total >= 5 else "Médio" if corners_total >= 3 else "Baixo"

        if corners_home > corners_away + 1:
            dominante = "Mandante"
        elif corners_away > corners_home + 1:
            dominante = "Visitante"
        else:
            dominante = "Equilibrado"

        checklist = f"""
📋 <b>Checklist Completo:</b>
⏱ Minuto: {minute}
🚩 Cantos totais: {corners_total}
📊 Cantos: {corners_home} (Casa) x {corners_away} (Fora)
⚡ Ritmo últimos 5min: {ritmo_5}
📈 Ritmo últimos 10min: {ritmo_10}
👑 Time dominante: {dominante}
"""
        return checklist

    @staticmethod
    def predict_next_corner_side(stats: Dict, home: str, away: str):
        home_score = 0
        away_score = 0
        reasons = []

        if stats["corners_home"] > stats["corners_away"]:
            home_score += 3
            reasons.append(f"{home} tem mais cantos")
        elif stats["corners_away"] > stats["corners_home"]:
            away_score += 3
            reasons.append(f"{away} tem mais cantos")

        if abs(home_score - away_score) <= 1:
            return "Equilibrado", "Jogo equilibrado"

        if home_score > away_score:
            return "Mandante", " | ".join(reasons)
        else:
            return "Visitante", " | ".join(reasons)

    @staticmethod
    def generate_suggestions(stats: Dict, rules_hit: List[str], minute: int, home: str, away: str):
        suggestions = []
        corners_home = stats["corners_home"]
        corners_away = stats["corners_away"]
        total = stats["corners_total"]

        def odd(t):
            return {
                "Próximo Escanteio": 1.85,
                "Cantos por equipe": 1.90,
                "Over HT": 1.80,
                "Over FT": 1.85,
                "Ambos": 1.75
            }.get(t, 1.85)

        next_side, reason = IntelligentAnalyzer.predict_next_corner_side(stats, home, away)

        if any("Próximo" in r for r in rules_hit):
            suggestions.append(BetSuggestion(
                bet_type="Próximo Escanteio",
                side=next_side,
                reason=reason,
                odd=odd("Próximo Escanteio"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away,
                predicted_next_corner=next_side
            ))

        if corners_home > corners_away:
            suggestions.append(BetSuggestion(
                bet_type="Cantos por equipe",
                side="Mandante",
                reason=f"{home} está melhor no jogo",
                odd=odd("Cantos por equipe"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        elif corners_away > corners_home:
            suggestions.append(BetSuggestion(
                bet_type="Cantos por equipe",
                side="Visitante",
                reason=f"{away} está melhor no jogo",
                odd=odd("Cantos por equipe"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))

        if minute <= 35 and total >= 4:
            suggestions.append(BetSuggestion(
                bet_type="Over HT 4.5",
                side=None,
                reason="Ritmo alto para bater +4.5 HT",
                odd=odd("Over HT"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))

        if minute <= 70 and total >= 6:
            suggestions.append(BetSuggestion(
                bet_type="Over FT 9.5",
                side=None,
                reason="Bom ritmo de cantos",
                odd=odd("Over FT"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))

        return suggestions

# =========================================================
# AVALIADOR
# =========================================================

class ResultEvaluator:

    @staticmethod
    def evaluate_suggestion(sug: BetSuggestion, md: MatchData) -> str:
        bet = sug.bet_type

        if "Próximo" in bet:
            if md.next_corner_after_entry is None:
                return "RED"
            if sug.predicted_next_corner == "Equilibrado":
                return "GREEN"
            return "GREEN" if sug.predicted_next_corner == md.next_corner_after_entry else "RED"

        if "Cantos por equipe" in bet:
            if sug.side == "Mandante":
                return "GREEN" if md.final_corners_home > sug.corners_at_entry_home else "RED"
            if sug.side == "Visitante":
                return "GREEN" if md.final_corners_away > sug.corners_at_entry_away else "RED"

        if "Over HT" in bet:
            total = md.final_corners_home + md.final_corners_away
            return "GREEN" if total >= 5 else "RED"

        if "Over FT" in bet:
            total = md.final_corners_home + md.final_corners_away
            return "GREEN" if total >= 10 else "RED"

        return "RED"

# =========================================================
# MENSAGENS
# =========================================================

def format_entry_message(md: MatchData, stats: Dict, minute: int, rules: List[str], suggestions: List[BetSuggestion]) -> str:
    home = esc_html(md.home_team)
    away = esc_html(md.away_team)
    league = esc_html(md.league)

    msg = f"""<b>⚽ ENTRADA DETECTADA!</b>

📌 <b>Jogo:</b> {home} x {away}
🏆 <b>Liga:</b> {league}
⏱ <b>Minuto:</b> {minute}
🚩 <b>Total Cantos:</b> {stats['corners_total']}

<b>📊 Estratégias ativadas:</b>
"""

    for r in rules:
        msg += f"• {esc_html(r)}\n"

    msg += "\n"
    msg += IntelligentAnalyzer.generate_checklist(stats, minute)
    msg += "\n\n<b>💡 Sugestões:</b>\n\n"

    for i, sug in enumerate(suggestions, 1):
        side = f" ({sug.side})" if sug.side else ""
        msg += f"<b>{i}) {esc_html(sug.bet_type)}{side}</b>\n"
        msg += f"   💰 Odd: {sug.odd:.2f}\n"
        msg += f"   📝 {esc_html(sug.reason)}\n\n"

    search = f"{home}%20{away}".replace(" ", "%20")
    msg += f'🔗 <a href="https://br.betano.com/search/{search}">Apostar na Betano</a>'

    return msg

def format_final_report(md: MatchData) -> str:
    home = esc_html(md.home_team)
    away = esc_html(md.away_team)
    total = md.final_corners_home + md.final_corners_away

    msg = f"""<b>🏁 Jogo finalizado!</b>

📌 <b>{home} x {away}</b>
🚩 <b>Total de Cantos:</b> {total} ({md.final_corners_home} x {md.final_corners_away})

<b>📊 Resultados:</b>
"""

    for i, sug in enumerate(md.suggestions, 1):
        side = f" ({sug.side})" if sug.side else ""
        r = "✅ GREEN" if sug.result == "GREEN" else "❌ RED"
        msg += f"<b>{i}) {esc_html(sug.bet_type)}{side}</b> — {r}\n"

    return msg

# =========================================================
# LOOP PRINCIPAL
# =========================================================

async def main_loop():
    logger.info("CornerBot PRO iniciado — monitorando jogos ao vivo...")

    active_matches: Dict[int, MatchData] = {}

    async with aiohttp.ClientSession() as session:
        api = ApiClient(session, API_KEY)

        await safe_send("<b>🔥 CornerBot PRO INICIADO</b>\nMonitorando jogos ao vivo...")

        while True:
            try:
                matches = await api.get_live()

                for m in matches:
                    try:
                        fixture = m.get("fixture", {})
                        fid = fixture.get("id")
                        if not fid:
                            continue

                        status = fixture.get("status", {})
                        minute_raw = status.get("elapsed")

                        try:
                            minute = int(minute_raw) if minute_raw is not None else None
                        except Exception:
                            minute = None

                        status_short = status.get("short", "")

                        stats = await api.get_full_statistics(fid)

                        corners_home = stats["corners_home"]
                        corners_away = stats["corners_away"]
                        total_corners = stats["corners_total"]

                        rules_hit = apply_rules_from_values(minute, total_corners)

                        # ---------- ENTRADA ----------
                        if rules_hit and fid not in active_matches:
                            home = m["teams"]["home"]["name"]
                            away = m["teams"]["away"]["name"]
                            league = m["league"]["name"]

                            md = MatchData(
                                fixture_id=fid,
                                home_team=home,
                                away_team=away,
                                league=league,
                                entry_minute=minute,
                                corners_at_entry_home=corners_home,
                                corners_at_entry_away=corners_away
                            )

                            md.suggestions = IntelligentAnalyzer.generate_suggestions(
                                stats, rules_hit, minute or 0, home, away
                            )

                            text = format_entry_message(md, stats, minute or 0, rules_hit, md.suggestions)

                            msg = await safe_send(text)
                            if msg:
                                md.message_id = msg.message_id
                                active_matches[fid] = md

                        # ---------- PRÓXIMO CANTO ----------
                        if fid in active_matches:
                            md = active_matches[fid]
                            if md.next_corner_after_entry is None:
                                if corners_home > md.corners_at_entry_home:
                                    md.next_corner_after_entry = "Mandante"
                                    md.corners_at_entry_home = corners_home
                                elif corners_away > md.corners_at_entry_away:
                                    md.next_corner_after_entry = "Visitante"
                                    md.corners_at_entry_away = corners_away

                        # ---------- FINALIZAÇÃO ----------
                        if fid in active_matches and status_short in ("FT", "AET", "PEN", "FT_PEN"):
                            md = active_matches[fid]

                            await asyncio.sleep(15)
                            stats_cache._cache.pop(fid, None)

                            stats = await api.get_full_statistics(fid)
                            md.final_corners_home = stats["corners_home"]
                            md.final_corners_away = stats["corners_away"]

                            for sug in md.suggestions:
                                sug.result = ResultEvaluator.evaluate_suggestion(sug, md)

                            final_msg = format_final_report(md)

                            if md.message_id:
                                ok = await safe_edit(md.message_id, final_msg)
                                if not ok:
                                    await safe_send(final_msg)
                            else:
                                await safe_send(final_msg)

                            del active_matches[fid]

                    except Exception as e:
                        logger.error(f"Erro ao processar fixture {fid}: {e}", exc_info=True)

                await asyncio.sleep(POLL_INTERVAL)

            except Exception as e:
                logger.error(f"Erro no loop principal: {e}", exc_info=True)
                await asyncio.sleep(POLL_INTERVAL)

# =========================================================
# KEEP-ALIVE
# =========================================================

async def handle(request):
    re
