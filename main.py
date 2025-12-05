#!/usr/bin/env python3
import os
import asyncio
import logging
import random
import json
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
from telegram import Bot

# =========================================================
# CONFIGURAÇÕES OTIMIZADAS
# =========================================================

API_KEY = os.getenv("API_KEY")
BASE = "https://v3.football.api-sports.io"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_ENV = os.getenv("CHAT_ID")

if not API_KEY or not TELEGRAM_TOKEN or not CHAT_ID_ENV:
    raise RuntimeError("Variáveis de ambiente não definidas")

CHAT_ID = int(CHAT_ID_ENV)

# ESTRATÉGIA: Dividir o dia em janelas de monitoramento
# Horários de pico de jogos: 14h-17h e 19h-23h (horário BR)
PEAK_HOURS = [(14, 17), (19, 23)]

# Intervalos inteligentes
POLL_INTERVAL_PEAK = 180      # 3 min nos horários de pico (20 req/hora máx)
POLL_INTERVAL_NORMAL = 600    # 10 min fora de pico (6 req/hora)
POLL_INTERVAL_LOW = 1800      # 30 min madrugada (2 req/hora)

CONCURRENT_REQUESTS = 2
STAT_TTL = 300  # 5 minutos de cache (era 15seg!)
REQUEST_TIMEOUT = 20
MAX_RETRIES = 2
BACKOFF_FACTOR = 2

# Ligas prioritárias (focar nas melhores)
PRIORITY_LEAGUES = [
    "Premier League", "LaLiga", "Serie A", "Bundesliga", 
    "Ligue 1", "Champions League", "Europa League",
    "Brasileirão Série A", "Championship", "Eredivisie"
]

LOG_LEVEL = logging.INFO
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cornerbot")

bot = Bot(token=TELEGRAM_TOKEN)

# =========================================================
# CONTADOR DE REQUISIÇÕES
# =========================================================

class RequestCounter:
    def __init__(self, daily_limit=110):
        self.daily_limit = daily_limit
        self.count = 0
        self.last_reset = datetime.now().date()
        self.history = []
        
    def can_request(self) -> bool:
        self._check_reset()
        return self.count < self.daily_limit
    
    def increment(self):
        self._check_reset()
        self.count += 1
        self.history.append(datetime.now())
        logger.info(f"📊 Requisições hoje: {self.count}/{self.daily_limit} ({self.daily_limit - self.count} restantes)")
    
    def _check_reset(self):
        today = datetime.now().date()
        if today > self.last_reset:
            logger.info(f"🔄 Reset diário: {self.count} requisições usadas ontem")
            self.count = 0
            self.last_reset = today
            self.history = []
    
    def get_stats(self) -> str:
        return f"📊 {self.count}/{self.daily_limit} req usadas hoje ({self.daily_limit - self.count} restantes)"

req_counter = RequestCounter()

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
    last_check: float = 0

# =========================================================
# CACHE PERSISTENTE
# =========================================================

class SmartCache:
    def __init__(self):
        self._stats_cache: Dict[int, Tuple[float, Dict]] = {}
        self._live_cache: Optional[Tuple[float, List]] = None
        self._live_cache_ttl = 120  # Cache de jogos ao vivo: 2 minutos
        
    def get_stats(self, fixture_id: int) -> Optional[Dict]:
        entry = self._stats_cache.get(fixture_id)
        if not entry:
            return None
        ts, val = entry
        if (asyncio.get_event_loop().time() - ts) > STAT_TTL:
            del self._stats_cache[fixture_id]
            return None
        return val
    
    def set_stats(self, fixture_id: int, value: Dict):
        self._stats_cache[fixture_id] = (asyncio.get_event_loop().time(), value)
    
    def get_live_matches(self) -> Optional[List]:
        if not self._live_cache:
            return None
        ts, matches = self._live_cache
        if (asyncio.get_event_loop().time() - ts) > self._live_cache_ttl:
            self._live_cache = None
            return None
        return matches
    
    def set_live_matches(self, matches: List):
        self._live_cache = (asyncio.get_event_loop().time(), matches)

smart_cache = SmartCache()

# =========================================================
# GERENCIADOR DE HORÁRIOS
# =========================================================

def get_current_interval() -> int:
    """Retorna intervalo baseado no horário (UTC-4 Manaus)"""
    now = datetime.now()
    hour = now.hour
    
    # Madrugada (0h-6h): muito lento
    if 0 <= hour < 6:
        return POLL_INTERVAL_LOW
    
    # Horários de pico
    for start, end in PEAK_HOURS:
        if start <= hour <= end:
            return POLL_INTERVAL_PEAK
    
    # Horário normal
    return POLL_INTERVAL_NORMAL

def is_priority_league(league_name: str) -> bool:
    """Verifica se é liga prioritária"""
    return any(pl.lower() in league_name.lower() for pl in PRIORITY_LEAGUES)

# =========================================================
# UTIL
# =========================================================

def esc_html(s: str) -> str:
    if s is None:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# =========================================================
# API CLIENT OTIMIZADO
# =========================================================

class OptimizedApiClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = session
        self.headers = {"x-apisports-key": api_key}
        self.semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

    async def _fetch_json(self, url: str, params: dict = None) -> Optional[dict]:
        if not req_counter.can_request():
            logger.warning("⚠️ LIMITE DIÁRIO ATINGIDO! Aguardando reset...")
            return None
        
        params = params or {}
        attempt = 0

        while attempt <= MAX_RETRIES:
            try:
                async with self.semaphore:
                    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    async with self.session.get(url, headers=self.headers, params=params, timeout=timeout) as resp:
                        
                        req_counter.increment()  # Contabiliza requisição
                        
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

                backoff = (BACKOFF_FACTOR ** attempt) + random.uniform(0, 1)
                logger.warning(f"Tentativa {attempt}/{MAX_RETRIES} falhou. Backoff {backoff:.2f}s")
                await asyncio.sleep(backoff)

        return None

    async def get_live_smart(self):
        """Busca jogos ao vivo com cache e filtros"""
        # Tenta cache primeiro
        cached = smart_cache.get_live_matches()
        if cached:
            logger.info("✅ Usando cache de jogos ao vivo (economizou 1 requisição)")
            return cached
        
        url = f"{BASE}/fixtures"
        j = await self._fetch_json(url, {"live": "all"})
        
        if not j:
            return []
        
        matches = j.get("response", [])
        
        # Filtra apenas ligas prioritárias
        filtered = [m for m in matches if is_priority_league(m.get("league", {}).get("name", ""))]
        
        logger.info(f"🎯 {len(filtered)}/{len(matches)} jogos filtrados (ligas prioritárias)")
        
        smart_cache.set_live_matches(filtered)
        return filtered

    async def get_full_statistics(self, fixture_id: int):
        # Cache primeiro
        cached = smart_cache.get_stats(fixture_id)
        if cached:
            logger.info(f"✅ Stats em cache para fixture {fixture_id}")
            return cached

        url = f"{BASE}/fixtures/statistics"
        j = await self._fetch_json(url, {"fixture": fixture_id})

        result = {"corners_home": 0, "corners_away": 0, "corners_total": 0}

        if not j:
            return result

        resp = j.get("response", [])
        if not resp or len(resp) < 2:
            return result

        home_stats = resp[0]["statistics"]
        away_stats = resp[1]["statistics"]

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

        smart_cache.set_stats(fixture_id, result)
        return result

# =========================================================
# TELEGRAM
# =========================================================

async def safe_send(text: str):
    try:
        return await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem: {e}")
        return None

async def safe_edit(message_id: int, text: str):
    try:
        await bot.edit_message_text(chat_id=CHAT_ID, message_id=message_id, text=text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.error(f"Erro ao editar mensagem: {e}")
        return False

# =========================================================
# REGRAS (SEM ALTERAÇÃO)
# =========================================================

def apply_rules_from_values(minute: Optional[int], corners: int, home: int = None, away: int = None) -> List[str]:
    checks: List[str] = []
    if minute is None:
        return checks

    if 15 <= minute <= 35 and corners == 4:
        checks.append("1️⃣ Over HT > 4.5")

    if 55 <= minute <= 75 and corners in (8, 9):
        checks.append("2️⃣ Over FT > 9.5")

    if minute >= 12 and corners >= 3 and home is not None and away is not None:
        if abs(home - away) >= 3:
            checks.append("3️⃣ Próximo Escanteio")

    if minute >= 30 and home is not None and away is not None:
        if abs(home - away) >= 3 and corners >= 6:
            checks.append("4️⃣ AH asiático cantos")

    if minute >= 25 and home is not None and away is not None:
        if abs(home - away) >= 2 and corners >= 5:
            checks.append("5️⃣ Cantos por equipe")

    if minute >= 35 and home is not None and away is not None:
        if home >= 3 and away >= 3:
            checks.append("6️⃣ Ambos Times Cantos")

    if minute >= 15 and corners >= 4:
        media = corners / max(minute, 1)
        if media >= 0.20:
            checks.append("7️⃣ Pressão para próximo canto")

    return checks

# =========================================================
# ANALISADOR (SEM ALTERAÇÃO)
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

        return f"""
📋 <b>Checklist Completo:</b>
⏱ Minuto: {minute}
🚩 Cantos totais: {corners_total}
📊 Cantos: {corners_home} (Casa) x {corners_away} (Fora)
⚡ Ritmo últimos 5min: {ritmo_5}
📈 Ritmo últimos 10min: {ritmo_10}
👑 Time dominante: {dominante}
"""

    @staticmethod
    def predict_next_corner_side(stats: Dict, home: str, away: str):
        if stats["corners_home"] > stats["corners_away"]:
            return "Mandante", f"{home} tem mais cantos"
        elif stats["corners_away"] > stats["corners_home"]:
            return "Visitante", f"{away} tem mais cantos"
        return "Equilibrado", "Jogo equilibrado"

    @staticmethod
    def generate_suggestions(stats: Dict, rules_hit: List[str], minute: int, home: str, away: str):
        suggestions = []
        corners_home = stats["corners_home"]
        corners_away = stats["corners_away"]
        total = stats["corners_total"]

        next_side, reason = IntelligentAnalyzer.predict_next_corner_side(stats, home, away)

        if any("Próximo" in r for r in rules_hit):
            suggestions.append(BetSuggestion(
                bet_type="Próximo Escanteio",
                side=next_side,
                reason=reason,
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away,
                predicted_next_corner=next_side
            ))

        if corners_home > corners_away:
            suggestions.append(BetSuggestion(
                bet_type="Cantos por equipe",
                side="Mandante",
                reason=f"{home} está melhor no jogo",
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        elif corners_away > corners_home:
            suggestions.append(BetSuggestion(
                bet_type="Cantos por equipe",
                side="Visitante",
                reason=f"{away} está melhor no jogo",
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))

        if minute <= 35 and total >= 4:
            suggestions.append(BetSuggestion(
                bet_type="Over HT 4.5",
                side=None,
                reason="Ritmo alto para bater +4.5 HT",
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))

        if minute <= 70 and total >= 6:
            suggestions.append(BetSuggestion(
                bet_type="Over FT 9.5",
                side=None,
                reason="Bom ritmo de cantos",
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))

        return suggestions

# =========================================================
# FORMATADOR DE MENSAGEM
# =========================================================

def format_entry_message(md: MatchData, stats: Dict, minute: int, rules: List[str], suggestions: List[BetSuggestion]) -> str:
    msg = f"""
🚨 <b>ENTRADA DETECTADA</b> 🚨

⚽ <b>{esc_html(md.home_team)} vs {esc_html(md.away_team)}</b>
🏆 {esc_html(md.league)}
⏱ Minuto: {minute}'

📊 <b>Escanteios:</b>
🏠 Casa: {stats['corners_home']}
✈️ Fora: {stats['corners_away']}
📈 Total: {stats['corners_total']}

✅ <b>Regras ativadas:</b>
{chr(10).join(rules)}

💡 <b>Sugestões:</b>
"""
    for i, sug in enumerate(suggestions, 1):
        side_txt = f" ({sug.side})" if sug.side else ""
        msg += f"\n{i}. {sug.bet_type}{side_txt}\n   📝 {sug.reason}"
    
    return msg

# =========================================================
# LOOP PRINCIPAL OTIMIZADO
# =========================================================

async def main_loop():
    logger.info("🚀 CornerBot PRO OTIMIZADO iniciado")
    logger.info(f"📊 Limite: 110 requisições/dia")
    logger.info(f"🎯 Ligas prioritárias: {len(PRIORITY_LEAGUES)}")

    active_matches: Dict[int, MatchData] = {}
    cycles_count = 0

    async with aiohttp.ClientSession() as session:
        api = OptimizedApiClient(session, API_KEY)

        await safe_send(f"""
<b>🔥 CornerBot PRO OTIMIZADO</b>

✅ Sistema iniciado
📊 Limite: 110 req/dia
🎯 Focando em {len(PRIORITY_LEAGUES)} ligas prioritárias
⏰ Intervalo dinâmico por horário

<i>Economia inteligente de requisições ativa!</i>
""")

        while True:
            try:
                cycles_count += 1
                current_interval = get_current_interval()
                
                logger.info(f"\n{'='*60}")
                logger.info(f"🔄 Ciclo #{cycles_count} - {datetime.now().strftime('%H:%M:%S')}")
                logger.info(f"⏰ Próximo ciclo em {current_interval}s")
                logger.info(req_counter.get_stats())
                
                if not req_counter.can_request():
                    logger.warning("⚠️ Limite diário atingido. Aguardando reset...")
                    await asyncio.sleep(3600)  # Espera 1h
                    continue

                # Busca jogos (1 requisição, mas com cache de 2min)
                matches = await api.get_live_smart()
                
                if not matches:
                    logger.info("📭 Nenhum jogo ao vivo nas ligas prioritárias")
                    await asyncio.sleep(current_interval)
                    continue
                
                logger.info(f"⚽ {len(matches)} jogos ao vivo monitorados")
                
                # Processa apenas jogos promissores
                for m in matches:
                    if not req_counter.can_request():
                        logger.warning("⚠️ Limite atingido durante ciclo")
                        break
                    
                    fixture = m.get("fixture", {})
                    fid = fixture.get("id")
                    if not fid:
                        continue

                    status = fixture.get("status", {})
                    minute = status.get("elapsed")
                    minute = int(minute) if minute else None
                    
                    if not minute or minute < 10:  # Ignora início de jogo
                        continue

                    # Busca stats (com cache de 5min)
                    stats = await api.get_full_statistics(fid)
                    
                    corners_home = stats["corners_home"]
                    corners_away = stats["corners_away"]
                    total_corners = stats["corners_total"]

                    # Aplica regras
                    rules_hit = apply_rules_from_values(minute, total_corners, corners_home, corners_away)

                    # Nova entrada detectada
                    if rules_hit and fid not in active_matches:
                        home = m["teams"]["home"]["name"]
                        away = m["teams"]["away"]["name"]
                        league = m["league"]["name"]

                        md = MatchData(fid, home, away, league, None, minute, corners_home, corners_away)
                        md.suggestions = IntelligentAnalyzer.generate_suggestions(
                            stats, rules_hit
