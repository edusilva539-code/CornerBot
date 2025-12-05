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
PEAK_HOURS = [(14, 17), (19, 23)]

# Intervalos inteligentes
POLL_INTERVAL_PEAK = 180      # 3 min nos horários de pico
POLL_INTERVAL_NORMAL = 600    # 10 min fora de pico
POLL_INTERVAL_LOW = 1800      # 30 min madrugada

CONCURRENT_REQUESTS = 2
STAT_TTL = 300  # 5 minutos de cache
REQUEST_TIMEOUT = 20
MAX_RETRIES = 2
BACKOFF_FACTOR = 2

# Ligas prioritárias
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
# ESTATÍSTICAS GLOBAIS
# =========================================================

class BotStats:
    def __init__(self):
        self.total_entries = 0
        self.total_greens = 0
        self.total_reds = 0
        self.active_entries = 0
        
    def add_entry(self):
        self.total_entries += 1
        self.active_entries += 1
    
    def add_result(self, is_green: bool):
        if is_green:
            self.total_greens += 1
        else:
            self.total_reds += 1
        self.active_entries -= 1
    
    def get_winrate(self) -> float:
        total = self.total_greens + self.total_reds
        if total == 0:
            return 0.0
        return (self.total_greens / total) * 100
    
    def get_summary(self) -> str:
        wr = self.get_winrate()
        return f"""
📊 <b>ESTATÍSTICAS DO BOT</b>
━━━━━━━━━━━━━━━━━━━━
✅ Greens: {self.total_greens}
❌ Reds: {self.total_reds}
📈 Win Rate: {wr:.1f}%
🎯 Entradas ativas: {self.active_entries}
📋 Total de entradas: {self.total_entries}
"""

bot_stats = BotStats()

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
        remaining = self.daily_limit - self.count
        if remaining <= 10:
            logger.warning(f"⚠️ ATENÇÃO: Apenas {remaining} requisições restantes!")
        else:
            logger.info(f"📊 Requisições: {self.count}/{self.daily_limit} ({remaining} restantes)")
    
    def _check_reset(self):
        today = datetime.now().date()
        if today > self.last_reset:
            logger.info(f"🔄 Reset diário: {self.count} requisições usadas ontem")
            self.count = 0
            self.last_reset = today
            self.history = []
    
    def get_stats(self) -> str:
        remaining = self.daily_limit - self.count
        return f"📊 {self.count}/{self.daily_limit} req ({remaining} restantes)"

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
    result: Optional[str] = None  # "GREEN", "RED", "PENDING"

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
    is_finished: bool = False
    half_time_corners: Optional[int] = None
    result_updated: bool = False

# =========================================================
# CACHE PERSISTENTE
# =========================================================

class SmartCache:
    def __init__(self):
        self._stats_cache: Dict[int, Tuple[float, Dict]] = {}
        self._live_cache: Optional[Tuple[float, List]] = None
        self._live_cache_ttl = 120
        
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
    now = datetime.now()
    hour = now.hour
    
    if 0 <= hour < 6:
        return POLL_INTERVAL_LOW
    
    for start, end in PEAK_HOURS:
        if start <= hour <= end:
            return POLL_INTERVAL_PEAK
    
    return POLL_INTERVAL_NORMAL

def is_priority_league(league_name: str) -> bool:
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
                        
                        req_counter.increment()
                        
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
        cached = smart_cache.get_live_matches()
        if cached:
            logger.info("✅ Cache de jogos ao vivo (economizou 1 req)")
            return cached
        
        url = f"{BASE}/fixtures"
        j = await self._fetch_json(url, {"live": "all"})
        
        if not j:
            return []
        
        matches = j.get("response", [])
        filtered = [m for m in matches if is_priority_league(m.get("league", {}).get("name", ""))]
        
        logger.info(f"🎯 {len(filtered)}/{len(matches)} jogos (ligas prioritárias)")
        
        smart_cache.set_live_matches(filtered)
        return filtered

    async def get_full_statistics(self, fixture_id: int):
        cached = smart_cache.get_stats(fixture_id)
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
# REGRAS
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
                predicted_next_corner=next_side,
                result="PENDING"
            ))

        if corners_home > corners_away:
            suggestions.append(BetSuggestion(
                bet_type="Cantos por equipe",
                side="Mandante",
                reason=f"{home} está melhor no jogo",
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away,
                result="PENDING"
            ))
        elif corners_away > corners_home:
            suggestions.append(BetSuggestion(
                bet_type="Cantos por equipe",
                side="Visitante",
                reason=f"{away} está melhor no jogo",
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away,
                result="PENDING"
            ))

        if minute <= 35 and total >= 4:
            suggestions.append(BetSuggestion(
                bet_type="Over HT 4.5",
                side=None,
                reason="Ritmo alto para bater +4.5 HT",
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away,
                result="PENDING"
            ))

        if minute <= 70 and total >= 6:
            suggestions.append(BetSuggestion(
                bet_type="Over FT 9.5",
                side=None,
                reason="Bom ritmo de cantos",
                odd=0.0,
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away,
                result="PENDING"
            ))

        return suggestions

# =========================================================
# AVALIADOR DE RESULTADOS
# =========================================================

class ResultEvaluator:
    @staticmethod
    def evaluate_suggestion(sug: BetSuggestion, md: MatchData, current_stats: Dict, minute: int) -> Optional[str]:
        """
        Retorna "GREEN", "RED" ou None (ainda pendente)
        """
        bet = sug.bet_type
        
        # Próximo Escanteio - avalia assim que acontecer
        if "Próximo" in bet:
            if md.next_corner_after_entry:
                if sug.predicted_next_corner == "Equilibrado":
                    return "GREEN"
                return "GREEN" if sug.predicted_next_corner == md.next_corner_after_entry else "RED"
            return None  # Ainda aguardando
        
        # Cantos por equipe - avalia no final do jogo
        if "Cantos por equipe" in bet:
            if not md.is_finished:
                return None
            if sug.side == "Mandante":
                return "GREEN" if md.final_corners_home > sug.corners_at_entry_home else "RED"
            if sug.side == "Visitante":
                return "GREEN" if md.final_corners_away > sug.corners_at_entry_away else "RED"
        
        # Over HT 4.5 - avalia no intervalo (minuto 45+)
        if "Over HT" in bet:
            if minute >= 45 and md.half_time_corners is not None:
                return "GREEN" if md.half_time_corners >= 5 else "RED"
            return None
        
        # Over FT 9.5 - avalia no final
        if "Over FT" in bet:
            if not md.is_finished:
                return None
            total = md.final_corners_home + md.final_corners_away
            return "GREEN" if total >= 10 else "RED"
        
        return None

    @staticmethod
    async def update_match_results(md: MatchData, current_stats: Dict, minute: int):
        """
        Avalia todas as sugestões e atualiza a mensagem
        """
        has_update = False
        greens = 0
        reds = 0
        pending = 0
        
        for sug in md.suggestions:
            if sug.result == "PENDING":
                result = ResultEvaluator.evaluate_suggestion(sug, md, current_stats, minute)
                if result:
                    sug.result = result
                    has_update = True
                    if result == "GREEN":
                        greens += 1
                        bot_stats.add_result(True)
                    else:
                        reds += 1
                        bot_stats.add_result(False)
                else:
                    pending += 1
            elif sug.result == "GREEN":
                greens += 1
            elif sug.result == "RED":
                reds += 1
        
        # Atualiza mensagem se houver mudanças
        if has_update and md.message_id:
            updated_msg = format_result_message(md, current_stats, minute, greens, reds, pending)
            await safe_edit(md.message_id, updated_msg)
            logger.info(f"✅ Resultados atualizados: {greens}G {reds}R {pending}P")
        
        # Marca como resultado atualizado se tudo foi avaliado
        if pending == 0 and not md.result_updated:
            md.result_updated = True
            logger.info(f"🏁 Jogo finalizado: {md.home_team} vs {md.away_team}")

# =========================================================
FORMATADORES DE MENSAGEM
# =========================================================

def format_entry_message(md: MatchData, stats: Dict, minute: int, rules: List[str], suggestions: List[BetSuggestion]) -> str:
    msg = f"""
ðŸš¨ <b>ENTRADA DETECTADA</b> ðŸš¨

âš½ <b>{esc_html(md.home_team)} vs {esc_html(md.away_team)}</b>
ðŸ † {esc_html(md.league)}
â ± Minuto: {minuto}'

ðŸ“Š <b>Escanteios na entrada:</b>
ðŸ Casa: {stats['corners_home']}
✓️ Fora: {stats['corners_away']}
ðŸ“ˆ Total: {stats['corners_total']}

âœ… <b>Regras ativadas:</b>
{chr(10).join(regras)}

ðŸ'¡ <b>Sugestões:</b>
"""
    para i, sug em enumerate(sugestões, 1):
        side_txt = f" ({sug.side})" se sug.side senão ""
        msg += f"\n{i}. {sug.bet_type}{side_txt}\n ðŸ“ {sug.reason}\n â ³ Status: AGUARDANDO..."
    
    mensagem de retorno

def format_result_message(md: MatchData, stats: Dict, minute: int, greens: int, reds: int, pending: int) -> str:
    msg = f"""
ðŸŽ¯ <b>ATUALIZAÇÃO DE RESULTADO</b>

âš½ <b>{esc_html(md.home_team)} vs {esc_html(md.away_team)}</b>
ðŸ † {esc_html(md.league)}
â ± Minuto atual: {minute}'

ðŸ“Š <b>Escanteios atuais:</b>
ðŸ Casa: {stats['corners_home']} (entrada: {md.corners_at_entry_home})
✓ Fora: {stats['corners_away']} (entrada: {md.corners_at_entry_away})
ðŸ“ˆ Total: {stats['corners_total']}

ðŸ'¡ <b>Resultados das Sugestões:</b>
"""
    
    para i, sug em enumerate(md.suggestions, 1):
        side_txt = f" ({sug.side})" se sug.side senão ""
        
        se sug.result == "VERDE":
            emoji = "âœ…"
            status = "VERDE âœ…"
        elif sug.result == "VERMELHO":
            emoji = "â Œ"
            status = "VERMELHO â Œ"
        outro:
            emoji = "â ³"
            status = "AGUARDANDO..."
        
        msg += f"\n{emoji} {i}. {sug.bet_type}{side_txt}\n ðŸ“ {sug.reason}\n ðŸŽ¯ Status: <b>{status}</b>\n"
    
    # Resumo
    msg += f"""
â” -” - ” -” -” -” -” -” -” -” -” -” -” -” -” -” -” -” -”
ðŸ“Š <b>Resumo:</b>
✓… Verdes: {verdes}
✓ Vermelhos: {vermelhos}
â ³ Pendentes: {pendente}
"""
    
    se pendente == 0:
        taxa de vitórias = (verdes / (verdes + vermelhos) * 100) se (verdes + vermelhos) > 0 senão 0
        msg += f"\nðŸ <b>JOGO FINALIZADO</b>\nðŸ“ˆ Taxa de acerto: {winrate:.1f}%"
    
    mensagem de retorno

# =========================================================
# LOOP PRINCIPAL OTIMIZADO
# =========================================================

async def main_loop():
    logger.info("ðŸš€ CornerBot PRO OTIMIZADO COM RESULTADOS iniciados")
    logger.info(f"ðŸ“Š Limite: 110 requisições/dia")
    logger.info(f"ðŸŽ¯ Ligas prioritárias: {len(PRIORITY_LEAGUES)}")

    active_matches: Dict[int, MatchData] = {}
    contagem_de_ciclos = 0

    assíncrono com aiohttp.ClientSession() como sessão:
        api = OptimizedApiClient(session, API_KEY)

        aguarde safe_end(f"""
<b>ðŸ”¥ CornerBot PRO - Sistema de Resultados Ativo</b>

âœ… Sistema iniciado
Limite: 110 req/dia
ðŸŽ¯ {len(PRIORITY_LEAGUES)} liga prioridades
â ° Intervalo dinâmico
ðŸŽ² Avaliação automática de resultados

<i>O bot agora mostra Verde/Vermelho automaticamente!</i>
"")

        enquanto Verdadeiro:
            tentar:
                contagem_de_ciclos += 1
                intervalo_atual = obter_intervalo_atual()
                
                logger.info(f"\n{'='*60}")
                logger.info(f"ðŸ”„ Ciclo #{cycles_count} - {datetime.now().strftime('%H:%M:%S')}")
                logger.info(f"â ° Próximo em {current_interval}s")
                logger.info(req_counter.get_stats())
                
                se não req_counter.can_request():
                    logger.warning("âš ï¸ Limite de dia atingido. Aguardando...")
                    aguarde asyncio.sleep(3600)
                    continuar

                # Busca jogos
                matches = await api.get_live_smart()
                
                se não houver correspondência:
                    logger.info("ðŸ“ Nenhum jogo nas ligas prioritárias")
                    
                    # Atualiza jogos ativos mesmo sem novos jogos
                    para fid, md em list(active_matches.items()):
                        se não md.is_finished e req_counter.can_request():
                            estatísticas = aguarde api.get_full_statistics(fid)
                            # Tente obter minuto atual (pode não estar mais ao vivo)
                            minuto = md.entry_minute ou 90
                            await ResultEvaluator.update_match_results(md, stats, minute)
                    
                    aguarde asyncio.sleep(intervalo_atual)
                    continuar
                
                logger.info(f"âš½ {len(matches)} jogos monitorados")
                
                # Processo jogos
                para m em partidas:
                    se não req_counter.can_request():
                        logger.warning("âš ï¸ Limite durante o ciclo")
                        quebrar
                    
                    fixture = m.get("fixture", {})
                    fid = fixture.get("id")
                    se não fid:
                        continuar

                    status = fixture.get("status", {})
                    status_curto = status.get("curto", "")
                    minuto = status.get("decorrido")
                    minuto = int(minuto) se minuto senão Nenhum
                    
                    # Detecta jogo finalizado
                    se status_short em ("FT", "AET", "PEN") e fid em active_matches:
                        md = correspondências_ativas[fid]
                        se não md.is_finished:
                            md.is_finished = True
                            estatísticas = aguarde api.get_full_statistics(fid)
                            md.final_corners_home = stats["corners_home"]
                            md.final_corners_away = stats["corners_away"]
                            await ResultEvaluator.update_match_results(md, stats, 90)
                            logger.info(f"ðŸ Jogo finalizado: {md.home_team} vs {md.away_team}")
                        continuar
                    
                    se não for minuto ou se for menos de 10 minutos:
                        continuar

                    # Estatísticas de Busca
                    estatísticas = aguarde api.get_full_statistics(fid)
                    
                    cantos_casa = estatísticas["cantos_casa"]
                    cantos_distantes = estatísticas["cantos_distantes"]
                    total_cantos = estatísticas["cantos_total"]
                    
                    # Detecta intervalo (HT)
                    se status_short == "HT" e fid em active_matches:
                        md = correspondências_ativas[fid]
                        Se md.half_time_corners for None:
                            md.cantos_no_meio_tempo = total_cantos
                            await ResultEvaluator.update_match_results(md, stats, 45)

                    # Aplicar regras para novas entradas
                    regras_acertadas = aplicar_regras_a partir_dos_valores(minuto, total_de_cantos, cantos_em_casa, cantos_fora)

                    # Nova entrada
                    Se rules_hit e fid não estiverem em active_matches:
                        casa = m["times"]["casa"]["nome"]
                        fora = m["times"]["fora"]["nome"]
                        liga = m["liga"]["nome"]

                        md = MatchData(fid, casa, fora, liga, None, minuto, escanteios_casa, escanteios_fora)
                        md.suggestions = IntelligentAnalyzer.generate_suggestions(
                            estatísticas, regras_acertadas, minuto, casa, fora
                        )

                        msg_text = format_entry_message(md, stats, minute, rules_hit, md.suggestions)
                        msg = await safe_send(msg_text)
                        
                        se msg:
                            md.message_id = msg.message_id
                            active_matches[fid] = md
                            bot_stats.add_entry()
                            logger.info(f"ðŸŽ¯ ENTRADA: {home} vs {away} ({minute}') - {len(rules_hit)} regras")

                    # Atualiza jogos ativo
                    se fid estiver em active_matches:
                        md = correspondências_ativas[fid]
                        
                        # Detecta próximo escanteio após entrada
                        Se md.next_corner_after_entry for None:
                            se corners_home > md.corners_at_entry_home:
                                md.next_corner_after_entry = "Mandante"
                                logger.info(f"ðŸš© Próximo escanteio: Mandante")
                            elif corners_away > md.corners_at_entry_away:
                                md.next_corner_after_entry = "Visitante"
                                logger.info(f"ðŸš© Próximo escanteio: Visitante")
                        
                        # Atualizar resultados
                        await ResultEvaluator.update_match_results(md, stats, minute)

                # Remove jogos já finalizados e avaliados (após 5 minutos)
                remover = []
                para fid, md em active_matches.items():
                    se md.result_updated:
                        para_remover.append(fid)
                
                para fid em to_remove:
                    deletar active_matches[fid]
                    logger.info(f"ðŸ—'ï¸ Removido jogo finalizado: {fid}")

                # Relatório periódico
                se cycles_count % 10 == 0:
                    relatório = f"""
{req_counter.get_stats()}
{bot_stats.get_summary()}
ðŸ”„ Ciclo #{cycles_count}
"""
                    aguardar safe_send(relatório)

                aguarde asyncio.sleep(intervalo_atual)

            exceto Exception como e:
                logger.error(f"â Œ Erro no loop principal: {e}", exc_info=True)
                aguarde asyncio.sleep(intervalo_atual)

# =========================================================
# MANTENHA-SE VIVO + INÍCIO
# =========================================================

async def handle(request):
    estatísticas = f"""CornerBot PRO Online
{req_counter.get_stats()}
Entradas: {bot_stats.total_entries}
Verdes: {bot_stats.total_greens}
Vermelhos: {bot_stats.total_reds}
Taxa de vitórias: {bot_stats.get_winrate():.1f}%
"""
    retornar web.Response(texto=estatísticas)

async def iniciar_servidor():
    aplicativo = web.Application()
    app.router.add_get("/", handle)
    porta = int(os.environ.get("PORTA", 3000))
    runner = web.AppRunner(app)
    aguarde runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    aguarde site.start()
    logger.info(f"ðŸŒ Servidor keep-alive na porta {port}")

async def main():
    aguarde iniciar_servidor()
    aguardar loop_principal()

se __name__ == "__main__":
    asyncio.run(main())
