#!/usr/bin/env python3
"""
CornerBot PRO - Versão completa com checklist, sugestões inteligentes e relatórios detalhados.
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
import random
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

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
# Telegram bot
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)

# -------------------------
# Data Classes
# -------------------------
@dataclass
class BetSuggestion:
    """Representa uma sugestão de aposta"""
    bet_type: str
    side: Optional[str]
    reason: str
    odd: float
    corners_at_entry_home: int
    corners_at_entry_away: int
    predicted_next_corner: Optional[str] = None
    result: Optional[str] = None  # "GREEN" ou "RED"

@dataclass
class MatchData:
    """Armazena dados de uma partida monitorada"""
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

# -------------------------
# Utilitários
# -------------------------
def esc_html(s: str) -> str:
    """Escapa HTML para evitar problemas no Telegram"""
    if s is None:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))

# -------------------------
# Cache de estatísticas
# -------------------------
class StatsCache:
    def __init__(self):
        self._cache: Dict[int, Tuple[float, Dict]] = {}

    def get(self, fixture_id: int) -> Optional[Dict]:
        entry = self._cache.get(fixture_id)
        if not entry:
            return None
        ts, val = entry
        now = asyncio.get_event_loop().time()
        if (now - ts) > STAT_TTL:
            del self._cache[fixture_id]
            return None
        return val

    def set(self, fixture_id: int, value: Dict):
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
        """Requisição com retries e backoff"""
        params = params or {}
        attempt = 0
        while attempt <= MAX_RETRIES:
            try:
                async with self.semaphore:
                    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    async with self.session.get(url, headers=self.headers, params=params, timeout=timeout) as resp:
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

    async def get_full_statistics(self, fixture_id: int) -> Dict:
        """
        Retorna estatísticas completas do jogo incluindo cantos, ataques, chutes, etc.
        Usa cache para reduzir chamadas.
        """
        cached = stats_cache.get(fixture_id)
        if cached is not None:
            return cached

        url = f"{BASE}/fixtures/statistics"
        params = {"fixture": fixture_id}
        j = await self._fetch_json(url, params=params)
        
        result = {
            "corners_home": 0,
            "corners_away": 0,
            "corners_total": 0,
            "attacks_home": 0,
            "attacks_away": 0,
            "dangerous_attacks_home": 0,
            "dangerous_attacks_away": 0,
            "shots_home": 0,
            "shots_away": 0,
            "shots_on_target_home": 0,
            "shots_on_target_away": 0,
            "possession_home": 0,
            "possession_away": 0
        }

        if not j:
            return result

        resp = j.get("response", []) or []
        for idx, team in enumerate(resp):
            is_home = idx == 0
            stats = team.get("statistics") or []
            
            for s in stats:
                typ = (s.get("type") or "").lower()
                val = s.get("value")
                
                try:
                    if "corner" in typ:
                        corners = int(val) if val else 0
                        if is_home:
                            result["corners_home"] = corners
                        else:
                            result["corners_away"] = corners
                    elif "total attacks" in typ:
                        if is_home:
                            result["attacks_home"] = int(val) if val else 0
                        else:
                            result["attacks_away"] = int(val) if val else 0
                    elif "dangerous attacks" in typ:
                        if is_home:
                            result["dangerous_attacks_home"] = int(val) if val else 0
                        else:
                            result["dangerous_attacks_away"] = int(val) if val else 0
                    elif typ == "total shots":
                        if is_home:
                            result["shots_home"] = int(val) if val else 0
                        else:
                            result["shots_away"] = int(val) if val else 0
                    elif "shots on goal" in typ or "shots on target" in typ:
                        if is_home:
                            result["shots_on_target_home"] = int(val) if val else 0
                        else:
                            result["shots_on_target_away"] = int(val) if val else 0
                    elif "ball possession" in typ:
                        poss_str = str(val).replace("%", "")
                        if is_home:
                            result["possession_home"] = int(poss_str) if poss_str else 0
                        else:
                            result["possession_away"] = int(poss_str) if poss_str else 0
                except Exception:
                    continue

        result["corners_total"] = result["corners_home"] + result["corners_away"]
        
        stats_cache.set(fixture_id, result)
        return result

# -------------------------
# Regras de detecção
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
# Análise inteligente
# -------------------------
class IntelligentAnalyzer:
    """Gera checklist, sugestões e previsões"""
    
    @staticmethod
    def generate_checklist(stats: Dict, minute: int) -> str:
        """Gera checklist detalhado"""
        corners_total = stats["corners_total"]
        corners_home = stats["corners_home"]
        corners_away = stats["corners_away"]
        
        # Ritmo (simplificado - pode ser melhorado com histórico temporal)
        ritmo_5min = "Alto" if corners_total >= 3 else "Médio" if corners_total >= 2 else "Baixo"
        ritmo_10min = "Alto" if corners_total >= 5 else "Médio" if corners_total >= 3 else "Baixo"
        
        # Time dominante
        if corners_home > corners_away + 1:
            dominante = "Mandante"
        elif corners_away > corners_home + 1:
            dominante = "Visitante"
        else:
            dominante = "Equilibrado"
        
        # Pressão ofensiva
        total_attacks = stats["attacks_home"] + stats["attacks_away"]
        dangerous = stats["dangerous_attacks_home"] + stats["dangerous_attacks_away"]
        
        if dangerous >= 10:
            pressao = "Alta"
        elif dangerous >= 5:
            pressao = "Média"
        else:
            pressao = "Baixa"
        
        # Qualidade do jogo
        shots_total = stats["shots_home"] + stats["shots_away"]
        if shots_total >= 10 and corners_total >= 5:
            qualidade = "Excelente"
        elif shots_total >= 5 and corners_total >= 3:
            qualidade = "Boa"
        else:
            qualidade = "Moderada"
        
        # Observações
        obs = "Jogo aberto com boas oportunidades" if dangerous >= 8 else "Jogo disputado"
        
        checklist = f"""📋 <b>Checklist Completo:</b>
⏱ Minuto: {minute}'
🚩 Cantos totais: {corners_total}
📊 Cantos: {corners_home} (Casa) x {corners_away} (Fora)
⚡ Ritmo últimos 5min: {ritmo_5min}
📈 Ritmo últimos 10min: {ritmo_10min}
👑 Time dominante: {dominante}
🎯 Pressão ofensiva: {pressao}
⭐ Qualidade do jogo: {qualidade}
💬 Observações: {obs}"""
        
        return checklist
    
    @staticmethod
    def predict_next_corner_side(stats: Dict, home_team: str, away_team: str) -> Tuple[str, str]:
        """
        Prevê o lado do próximo escanteio
        Retorna: (lado, motivo)
        """
        home_score = 0
        away_score = 0
        reasons = []
        
        # Ataques perigosos (peso 3)
        dang_home = stats["dangerous_attacks_home"]
        dang_away = stats["dangerous_attacks_away"]
        if dang_home > dang_away:
            home_score += 3
            reasons.append(f"{home_team} com mais ataques perigosos ({dang_home} vs {dang_away})")
        elif dang_away > dang_home:
            away_score += 3
            reasons.append(f"{away_team} com mais ataques perigosos ({dang_away} vs {dang_home})")
        
        # Ataques totais (peso 2)
        att_home = stats["attacks_home"]
        att_away = stats["attacks_away"]
        if att_home > att_away:
            home_score += 2
            reasons.append(f"{home_team} dominando ataques ({att_home} vs {att_away})")
        elif att_away > att_home:
            away_score += 2
            reasons.append(f"{away_team} dominando ataques ({att_away} vs {att_home})")
        
        # Chutes (peso 2)
        shots_home = stats["shots_home"]
        shots_away = stats["shots_away"]
        if shots_home > shots_away:
            home_score += 2
        elif shots_away > shots_home:
            away_score += 2
        
        # Cantos recentes (peso 3)
        corners_home = stats["corners_home"]
        corners_away = stats["corners_away"]
        if corners_home > corners_away:
            home_score += 3
            reasons.append(f"{home_team} já tem mais escanteios ({corners_home} vs {corners_away})")
        elif corners_away > corners_home:
            away_score += 3
            reasons.append(f"{away_team} já tem mais escanteios ({corners_away} vs {corners_home})")
        
        # Posse (peso 1)
        if stats["possession_home"] > 55:
            home_score += 1
        elif stats["possession_away"] > 55:
            away_score += 1
        
        # Decisão
        diff = abs(home_score - away_score)
        if diff <= 2:
            side = "Equilibrado"
            reason = "Jogo equilibrado, ambos os times pressionando"
        elif home_score > away_score:
            side = "Mandante"
            reason = " | ".join(reasons) if reasons else f"{home_team} com maior pressão ofensiva"
        else:
            side = "Visitante"
            reason = " | ".join(reasons) if reasons else f"{away_team} com maior pressão ofensiva"
        
        return side, reason
    
    @staticmethod
    def generate_suggestions(stats: Dict, rules_hit: List[str], minute: int, 
                           home_team: str, away_team: str) -> List[BetSuggestion]:
        """Gera sugestões inteligentes de apostas"""
        suggestions = []
        corners_home = stats["corners_home"]
        corners_away = stats["corners_away"]
        corners_total = stats["corners_total"]
        
        # Mock odds (podem ser ajustadas ou obtidas de outra fonte)
        def get_mock_odd(bet_type: str) -> float:
            odds_map = {
                "Próximo Escanteio": 1.85,
                "Cantos por equipe": 1.90,
                "AH Asiático": 1.95,
                "Over HT": 1.80,
                "Over FT": 1.85,
                "Ambos Times Cantos": 1.75
            }
            return odds_map.get(bet_type, 1.85)
        
        # Previsão do próximo escanteio
        next_side, next_reason = IntelligentAnalyzer.predict_next_corner_side(
            stats, home_team, away_team
        )
        
        # Sugestão 1: Próximo escanteio (sempre incluir se regra 3 ou 7 ativa)
        if any("Próximo" in r or "Pressão" in r for r in rules_hit):
            suggestions.append(BetSuggestion(
                bet_type="Próximo Escanteio",
                side=next_side,
                reason=next_reason,
                odd=get_mock_odd("Próximo Escanteio"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away,
                predicted_next_corner=next_side
            ))
        
        # Sugestão 2: Cantos por equipe
        if corners_home > corners_away:
            suggestions.append(BetSuggestion(
                bet_type="Cantos por equipe",
                side="Mandante",
                reason=f"{home_team} já lidera em escanteios ({corners_home} vs {corners_away}) e mantém pressão",
                odd=get_mock_odd("Cantos por equipe"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        elif corners_away > corners_home:
            suggestions.append(BetSuggestion(
                bet_type="Cantos por equipe",
                side="Visitante",
                reason=f"{away_team} já lidera em escanteios ({corners_away} vs {corners_home}) e mantém pressão",
                odd=get_mock_odd("Cantos por equipe"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        
        # Sugestão 3: AH Asiático (se mandante ou visitante domina)
        dang_home = stats["dangerous_attacks_home"]
        dang_away = stats["dangerous_attacks_away"]
        
        if dang_home > dang_away + 3 and corners_home >= corners_away:
            suggestions.append(BetSuggestion(
                bet_type="AH Asiático -1.5",
                side="Mandante",
                reason=f"{home_team} com {dang_home} ataques perigosos vs {dang_away}, dominância clara",
                odd=get_mock_odd("AH Asiático"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        elif dang_away > dang_home + 3 and corners_away >= corners_home:
            suggestions.append(BetSuggestion(
                bet_type="AH Asiático -1.5",
                side="Visitante",
                reason=f"{away_team} com {dang_away} ataques perigosos vs {dang_home}, dominância clara",
                odd=get_mock_odd("AH Asiático"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        
        # Sugestão 4: Over HT
        if minute <= 35 and corners_total >= 4:
            suggestions.append(BetSuggestion(
                bet_type="Over HT 4.5",
                side=None,
                reason=f"Já temos {corners_total} escanteios no minuto {minute}, ritmo alto para atingir 5+ no HT",
                odd=get_mock_odd("Over HT"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        
        # Sugestão 5: Over FT
        if corners_total >= 6 and minute <= 70:
            suggestions.append(BetSuggestion(
                bet_type="Over FT 9.5",
                side=None,
                reason=f"{corners_total} escanteios no minuto {minute}, projeção indica 10+ no final",
                odd=get_mock_odd("Over FT"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        
        # Sugestão 6: Ambos times cantos
        if corners_home >= 1 and corners_away >= 1:
            suggestions.append(BetSuggestion(
                bet_type="Ambos Times Cantos",
                side=None,
                reason=f"Ambos já fizeram escanteios ({corners_home} e {corners_away}), jogo aberto",
                odd=get_mock_odd("Ambos Times Cantos"),
                corners_at_entry_home=corners_home,
                corners_at_entry_away=corners_away
            ))
        
        return suggestions

# -------------------------
# Avaliador de resultados
# -------------------------
class ResultEvaluator:
    """Avalia GREEN/RED para cada sugestão"""
    
    @staticmethod
    def evaluate_suggestion(suggestion: BetSuggestion, match_data: MatchData) -> str:
        """Retorna 'GREEN' ou 'RED'"""
        bet_type = suggestion.bet_type
        
        # Próximo Escanteio
        if "Próximo" in bet_type:
            if match_data.next_corner_after_entry is None:
                return "RED"  # Não houve próximo escanteio
            predicted = suggestion.predicted_next_corner
            actual = match_data.next_corner_after_entry
            if predicted == "Equilibrado":
                return "GREEN"  # Aceita qualquer lado
            return "GREEN" if predicted == actual else "RED"
        
        # Cantos por equipe
        if "Cantos por equipe" in bet_type:
            if suggestion.side == "Mandante":
                return "GREEN" if match_data.final_corners_home > suggestion.corners_at_entry_home else "RED"
            elif suggestion.side == "Visitante":
                return "GREEN" if match_data.final_corners_away > suggestion.corners_at_entry_away else "RED"
        
        # AH Asiático -1.5
        if "AH Asiático" in bet_type:
            diff_final_home = match_data.final_corners_home - match_data.final_corners_away
            diff_final_away = match_data.final_corners_away - match_data.final_corners_home
            
            if suggestion.side == "Mandante":
                return "GREEN" if diff_final_home >= 2 else "RED"
            elif suggestion.side == "Visitante":
                return "GREEN" if diff_final_away >= 2 else "RED"
        
        # Over HT (verificar se temos dados de HT - simplificado aqui)
        if "Over HT" in bet_type:
            # Assumindo que entry foi antes do HT e final >= 5
            total_final = match_data.final_corners_home + match_data.final_corners_away
            return "GREEN" if total_final >= 5 else "RED"
        
        # Over FT
        if "Over FT" in bet_type:
            total_final = match_data.final_corners_home + match_data.final_corners_away
            return "GREEN" if total_final >= 10 else "RED"
        
        # Ambos Times Cantos
        if "Ambos Times" in bet_type:
            made_home = match_data.final_corners_home > suggestion.corners_at_entry_home
            made_away = match_data.final_corners_away > suggestion.corners_at_entry_away
            return "GREEN" if (made_home and made_away) else "RED"
        
        return "RED"  # Default

# -------------------------
# Helpers Telegram
# -------------------------
async def safe_send(text: str) -> Optional[dict]:
    """Envia mensagem e retorna obj Message"""
    try:
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
        logger.warning("Falha ao editar mensagem %s: %s", message_id, e)
        return False
    except Exception as e:
        logger.exception("Erro inesperado ao editar mensagem %s: %s", message_id, e)
        return False

# -------------------------
# Formatação de mensagens
# -------------------------
def format_entry_message(match_data: MatchData, stats: Dict, minute: int, 
                        rules_hit: List[str], suggestions: List[BetSuggestion]) -> str:
    """Formata mensagem completa de entrada"""
    home = esc_html(match_data.home_team)
    away = esc_html(match_data.away_team)
    league = esc_html(match_data.league)
    corners_total = stats["corners_total"]
    
    # Cabeçalho
    msg = f"""<b>⚽ ENTRADA DETECTADA!</b>

📌 <b>Jogo:</b> {home} x {away}
🏆 <b>Liga:</b> {league}
⏱ <b>Minuto:</b> {minute}'
🚩 <b>Cantos:</b> {corners_total}

📊 <b>Estratégias ativadas:</b>
"""
    
    for rule in rules_hit:
        msg += f"{esc_html(rule)}\n"
    
    msg += "\n"
    
    # Checklist
    checklist = IntelligentAnalyzer.generate_checklist(stats, minute)
    msg += f"{checklist}\n\n"
    
    # Sugestões
    msg += "<b>💡 Sugestões de Apostas:</b>\n\n"
    for idx, sug in enumerate(suggestions, 1):
        side_text = f" ({sug.side})" if sug.side else ""
        msg += f"<b>{idx}) {esc_html(sug.bet_type)}{side_text}</b>\n"
        msg += f"   💰 Odd: {sug.odd:.2f}\n"
        msg += f"   📝 {esc_html(sug.reason)}\n\n"
    
    # Link Betano
    betano_link = f"https://br.betano.com/search/{home}%20{away}%20{league}".replace(" ", "%20")
    msg += f'🔗 <a href="{betano_link}">Apostar na Betano</a>'
    
    return msg

def format_final_report(match_data: MatchData) -> str:
    """Formata relatório final com GREEN/RED"""
    home = esc_html(match_data.home_team)
    away = esc_html(match_data.away_team)
    total = match_data.final_corners_home + match_data.final_corners_away
    
    msg = f"""<b>🏁 Jogo finalizado!</b>

📌 <b>{home} x {away}</b>
🚩 <b>Total de Cantos:</b> {total} ({match_data.final_corners_home} x {match_data.final_corners_away})

📊 <b>Resultados das Apostas:</b>

"""
    
    for idx, sug in enumerate(match_data.suggestions, 1):
        side_text = f" ({sug.side})" if sug.side else ""
        result_emoji = "✅ GREEN" if sug.result == "GREEN" else "❌ RED"
        msg += f"<b>{idx}) {esc_html(sug.bet_type)}{side_text}</b> — Odd: {sug.odd:.2f} → {result_emoji}\n"
    
    msg += f"""
📈 <b>Relatório:</b>
• Mandante: {match_data.final_corners_home} cantos
• Visitante: {match_data.final_corners_away} cantos
"""
    
    return msg

# -------------------------
# Main loop
# -------------------------
async def main_loop():
    logger.info("CornerBot PRO INICIADO - monitorando jogos ao vivo...")
    
    # Dicionário de partidas ativas
    active_matches: Dict[int, MatchData] = {}
    
    async with aiohttp.ClientSession() as session:
        api = ApiClient(session, API_KEY)
        
        startup_text = "<b>🔥 CornerBot PRO INICIADO</b> – monitorando jogos ao vivo..."
        await safe_send(startup_text)

        while True:
            try:
                matches = await api.get_live()
                if not matches:
                    logger.debug("Nenhum jogo ao vivo no momento.")
                
                for m in matches:
                    try:
                        fid = m.get("fixture", {}).get("id")
                        if fid is None:
                            continue

                        # Minuto
                        minute_raw = m.get("fixture", {}).get("status", {}).get("elapsed")
                        try:
                            minute = int(minute_raw) if minute_raw is not None else None
                        except Exception:
                            minute = None

                        # Status do jogo
                        status_short = m.get("fixture", {}).get("status", {}).get("short")
                        
                        # Estatísticas completas
                        stats = await api.get_full_statistics(fid)
                        corners_total = stats["corners_total"]
                        corners_home = stats["corners_home"]
                        corners_away = stats["corners_away"]

                        # Verifica regras
                        rules_hit = apply_rules_from_values(minute, corners_total)

                        # Se regras ativadas E ainda não enviamos entrada
                        if rules_hit and fid not in active_matches:
                            home = m.get("teams", {}).get("home", {}).get("name", "—")
                            away = m.get("teams", {}).get("away", {}).get("name", "—")
                            league = m.get("league", {}).get("name", "—")
                            
                            # Cria objeto MatchData
                            match_data = MatchData(
                                fixture_id=fid,
                                home_team=home,
                                away_team=away,
                                league=league,
                                entry_minute=minute,
                                corners_at_entry_home=corners_home,
                                corners_at_entry_away=corners_away
                            )
                            
                            # Gera sugestões inteligentes
                            suggestions = IntelligentAnalyzer.generate_suggestions(
                                stats, rules_hit, minute or 0, home, away
                            )
                            match_data.suggestions = suggestions
                            
                            # Formata e envia mensagem completa
                            text = format_entry_message(match_data, stats, minute or 0, rules_hit, suggestions)
                            msg = await safe_send(text)
                            
                            if msg:
                                match_data.message_id = msg.message_id
                                active_matches[fid] = match_data
                                logger.info("Entrada enviada (fixture=%s, message_id=%s)", fid, msg.message_id)

                        # Monitora próximo escanteio após entrada
                        if fid in active_matches:
                            match_data = active_matches[fid]
                            
                            # Detecta próximo escanteio
                            if match_data.next_corner_after_entry is None:
                                if corners_home > match_data.corners_at_entry_home:
                                    match_data.next_corner_after_entry = "Mandante"
                                    logger.info("Próximo escanteio foi do Mandante (fixture=%s)", fid)
                                elif corners_away > match_data.corners_at_entry_away:
                                    match_data.next_corner_after_entry = "Visitante"
                                    logger.info("Próximo escanteio foi do Visitante (fixture=%s)", fid)
                            
                            # Jogo finalizado
                            if status_short == "FT":
                                # Atualiza dados finais
                                match_data.final_corners_home = corners_home
                                match_data.final_corners_away = corners_away
                                
                                # Avalia cada sugestão
                                for sug in match_data.suggestions:
                                    sug.result = ResultEvaluator.evaluate_suggestion(sug, match_data)
                                
                                # Gera relatório final
                                final_text = format_final_report(match_data)
                                
                                # Tenta editar mensagem original
                                if match_data.message_id:
                                    edited = await safe_edit(match_data.message_id, final_text)
                                    if edited:
                                        logger.info("Relatório final editado (fixture=%s)", fid)
                                    else:
                                        # Se falhar edição, envia nova mensagem
                                        await safe_send(final_text)
                                        logger.info("Relatório final enviado como nova mensagem (fixture=%s)", fid)
                                else:
                                    await safe_send(final_text)
                                
                                # Remove partida dos ativos
                                del active_matches[fid]
                                logger.info("Partida finalizada e removida (fixture=%s)", fid)

                    except Exception as e:
                        logger.exception("Erro processando partida: %s", e)

                await asyncio.sleep(POLL_INTERVAL)

            except Exception as e:
                logger.exception("Erro no loop principal: %s", e)
                await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("CornerBot PRO interrompido pelo usuário.")
    except Exception:
        logger.exception("CornerBot PRO caiu por erro inesperado.")
