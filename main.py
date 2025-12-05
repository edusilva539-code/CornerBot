#!/usr/bin/env python3
importar os
import asyncio
importação de registro
importar aleatório
importar json
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
Importar bot do Telegram

# =========================================================
# CONFIGURAÇÕES OTIMIZADAS
# =========================================================

API_KEY = os.getenv("API_KEY")
BASE = "https://v3.football.api-sports.io"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_ENV = os.getenv("CHAT_ID")

se não for API_KEY, TELEGRAM_TOKEN ou CHAT_ID_ENV:
    raise RuntimeError("Variáveis de ambiente não definidas")

CHAT_ID = int(CHAT_ID_ENV)

# ESTRATÉGIA: Dividir o dia em janelas de monitoramento
HORÁRIOS_DE_PICO = [(14, 17), (19, 23)]

# Intervalos altimetria
POLL_INTERVAL_PEAK = 180 # 3 min nos horários de pico
POLL_INTERVAL_NORMAL = 600 # 10 min fora do pico
POLL_INTERVAL_LOW = 1800 # 30 min madrugada

SOLICITAÇÕES_CONCORRENTES = 2
STAT_TTL = 300 # 5 minutos de cache
TEMPO LIMITE_DA_SOLICITAÇÃO = 20
MAX_RETRIES = 2
FATOR_DE_RECUO = 2

# Ligas prioritárias
LIGAS_PRIORIDADE = [
    "Premier League", "LaLiga", "Série A", "Bundesliga",
    "Ligue 1", "Liga dos Campeões", "Liga Europa",
    "Brasileirão Série A", "Campeonato", "Eredivisie"
]

LOG_LEVEL = logging.INFO
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cornerbot")

bot = Bot(token=TELEGRAM_TOKEN)

# =========================================================
# ESTATÍSTICAS GLOBAIS
# =========================================================

classe BotStats:
    def __init__(self):
        self.total_entries = 0
        self.total_verdes = 0
        self.total_vermelhos = 0
        self.entradas_ativas = 0
        
    def add_entry(self):
        self.total_entradas += 1
        self.entradas_ativas += 1
    
    def add_result(self, is_green: bool):
        se for verde:
            self.total_verdes += 1
        outro:
            self.total_vermelhos += 1
        self.entradas_ativas -= 1
    
    def get_winrate(self) -> float:
        total = self.total_verdes + self.total_vermelhos
        se total == 0:
            retornar 0.0
        retornar (self.total_greens / total) * 100
    
    def get_summary(self) -> str:
        wr = self.get_winrate()
        msg = "\nðŸ“Š <b>ESTAÇAS DO BOT</b>\n"
        msg += "â" -" -" -" -" -" -" -" -" -" -" -" -" -" -" -" -" -" -" \n"
        msg += f"âœ… Verduras: {self.total_greens}\n"
        msg += f"â Œ Vermelhos: {self.total_reds}\n"
        msg += f"ðŸ“ˆ Taxa de vitórias: {wr:.1f}%\n"
        msg += f"ðŸŽ¯ Entradas ativas: {self.active_entries}\n"
        msg += f"ðŸ“‹ Total de entradas: {self.total_entries}\n"
        mensagem de retorno

bot_stats = BotStats()

# =========================================================
#CONTADOR DE REQUISIÇÕES
# =========================================================

classe RequestCounter:
    def __init__(self, daily_limit=110):
        self.limite_diário = limite_diário
        self.count = 0
        self.last_reset = datetime.now().date()
        self.history = []
        
    def can_request(self) -> bool:
        self._check_reset()
        retornar self.count < self.daily_limit
    
    def increment(self):
        self._check_reset()
        self.count += 1
        self.history.append(datetime.now())
        restante = self.limite_diário - self.contagem
        se restar <= 10:
            logger.warning(f"âš ï¸ ATENÇÃO: Apenas {remaining} requisições restantes!")
        outro:
            logger.info(f"ðŸ“Š Requisitos: {self.count}/{self.daily_limit} ({remaining}restante)")
    
    def _check_reset(self):
        hoje = datetime.now().date()
        se hoje > self.last_reset:
            logger.info(f"ðŸ”„ Reset diário: {self.count} requisições usadas ontem")
            self.count = 0
            self.last_reset = hoje
            self.history = []
    
    def get_stats(self) -> str:
        restante = self.limite_diário - self.contagem
        retornar f"ðŸ“Š {self.count}/{self.daily_limit} req ({remaining}restante)"

contador_de_requisições = ContadorDeRequisições()

# =========================================================
# CLASSES DE DADOS
# =========================================================

@dataclass
classe Sugestão de Aposta:
    tipo_de_aposta: str
    lado: Opcional[str]
    motivo: str
    ímpar: flutuar
    cantos_na_entrada_da_casa: int
    cantos_na_entrada_distante: int
    próximo_canto_previsto: Opcional[str] = Nenhum
    resultado: Optional[str] = None # "VERDE", "VERMELHO", "PENDENTE"

@dataclass
classe MatchData:
    fixture_id: inteiro
    time_da_casa: str
    time_fora: str
    liga: str
    message_id: Opcional[int] = Nenhum
    minuto_de_entrada: Opcional[int] = Nenhum
    cantos_na_entrada_da_casa: int = 0
    cantos_na_entrada_distante: int = 0
    sugestões: List[BetSuggestion] = field(default_factory=list)
    next_corner_after_entry: Optional[str] = None
    cantos_final_casa: int = 0
    cantos_finais_distantes: int = 0
    última_verificação: float = 0
    is_finished: bool = False
    half_time_corners: Optional[int] = None
    resultado_atualizado: booleano = Falso

# =========================================================
# CACHE PERSISTENTE
# =========================================================

classe SmartCache:
    def __init__(self):
        self._stats_cache: Dict[int, Tuple[float, Dict]] = {}
        self._live_cache: Optional[Tuple[float, List]] = None
        self._live_cache_ttl = 120
        
    def get_stats(self, fixture_id: int) -> Optional[Dict]:
        entrada = self._stats_cache.get(fixture_id)
        se não houver entrada:
            retornar Nenhum
        ts, val = entrada
        if (asyncio.get_event_loop().time() - ts) > STAT_TTL:
            del self._stats_cache[fixture_id]
            retornar Nenhum
        retornar valor
    
    def set_stats(self, fixture_id: int, value: Dict):
        self._stats_cache[fixture_id] = (asyncio.get_event_loop().time(), value)
    
    def get_live_matches(self) -> Optional[List]:
        se não self._live_cache:
            retornar Nenhum
        ts, matches = self._live_cache
        if (asyncio.get_event_loop().time() - ts) > self._live_cache_ttl:
            self._live_cache = None
            retornar Nenhum
        retornar correspondências
    
    def set_live_matches(self, matches: List):
        self._live_cache = (asyncio.get_event_loop().time(), matches)

smart_cache = SmartCache()

# =========================================================
# GERÊNCIA DE HORÁRIOS
# =========================================================

def get_current_interval() -> int:
    agora = datetime.now()
    hora = agora.hora
    
    se 0 <= hora < 6:
        retornar POLL_INTERVAL_LOW
    
    Para começar, termine em HORÁRIO DE PICO:
        se início <= hora <= fim:
            retornar POLL_INTERVAL_PEAK
    
    retornar POLL_INTERVAL_NORMAL

def is_priority_league(league_name: str) -> bool:
    retornar qualquer(pl.lower() em league_name.lower() para pl em PRIORITY_LEAGUES)

# =========================================================
# UTIL
# =========================================================

def esc_html(s: str) -> str:
    se s for None:
        retornar ""
    retornar s.replace("&", "&").replace("<", "<").replace(">", ">")

# =========================================================
# CLIENTE API OTIMIZADO
# =========================================================

classe OptimizedApiClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = sessão
        self.headers = {"x-apisports-key": api_key}
        self.semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

    async def _fetch_json(self, url: str, params: dict = None) -> Optional[dict]:
        se não req_counter.can_request():
            logger.warning("âš ï¸ LIMITE DIÃ RIO ATINGIDO! Aguardando reset...")
            retornar Nenhum
        
        params = params ou {}
        tentativa = 0

        enquanto a tentativa for menor ou igual ao número máximo de tentativas:
            tentar:
                assíncrono com self.semaphore:
                    tempo limite = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    async with self.session.get(url, headers=self.headers, params=params, timeout=timeout) as resp:
                        
                        contador_de_requisições.incrementar()
                        
                        se resp.status em (429, 500, 502, 503):
                            texto = aguarde resp.texto()
                            raise aiohttp.ClientError(f"HTTP {resp.status}: {text}")

                        resp.raise_for_status()
                        retornar aguardar resp.json()

            exceto Exception como e:
                tentativa += 1
                se a tentativa for maior que MAX_RETRIES:
                    logger.error(f"Erro definitivo ao acessar {url}: {e}")
                    retornar Nenhum

                backoff = (BACKOFF_FACTOR ** tentativa) + random.uniform(0, 1)
                logger.warning(f"Tentativa {attempt}/{MAX_RETRIES} falhou. Backoff {backoff:.2f}s")
                aguarde asyncio.sleep(backoff)

        retornar Nenhum

    async def get_live_smart(self):
        cache = smart_cache.get_live_matches()
        se estiver em cache:
            logger.info("âœ… Cache de jogos ao vivo (economizou 1 req)")
            retornar em cache
        
        url = f"{BASE}/fixtures"
        j = await self._fetch_json(url, {"live": "all"})
        
        se não j:
            retornar []
        
        correspondências = j.get("resposta", [])
        filtrado = [m para m em partidas se is_priority_league(m.get("league", {}).get("name", ""))]
        
        logger.info(f"ðŸŽ¯ {len(filtered)}/{len(matches)} jogos (ligas prioritárias)")
        
        smart_cache.set_live_matches(filtered)
        retorno filtrado

    async def get_full_statistics(self, fixture_id: int):
        cached = smart_cache.get_stats(fixture_id)
        se estiver em cache:
            retornar em cache

        url = f"{BASE}/fixtures/statistics"
        j = await self._fetch_json(url, {"fixture": fixture_id})

        resultado = {"cantos_casa": 0, "cantos_fora": 0, "cantos_total": 0}

        se não j:
            retornar resultado

        resp = j.get("response", [])
        se não resp ou len(resp) < 2:
            retornar resultado

        home_stats = resp[0]["estatísticas"]
        away_stats = resp[1]["statistics"]

        def obter_valor(estatísticas, nome):
            para s em estatísticas:
                se name.lower() estiver em s.get("type", "").lower():
                    tentar:
                        retornar int(str(s.get("value", 0)).replace("%", ""))
                    exceto Exceção:
                        retornar 0
            retornar 0

        resultado["corners_home"] = get_value(home_stats, "corner")
        resultado["corners_away"] = get_value(away_stats, "corner")
        resultado["cantos_total"] = resultado["cantos_casa"] + resultado["cantos_fora"]

        smart_cache.set_stats(fixture_id, result)
        retornar resultado

# =========================================================
# TELEGRAM
# =========================================================

async def safe_send(text: str):
    tentar:
        return await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
    exceto Exception como e:
        logger.error(f"Erro ao enviar mensagem: {e}")
        retornar Nenhum

async def safe_edit(message_id: int, text: str):
    tentar:
        await bot.edit_message_text(chat_id=CHAT_ID, message_id=message_id, text=text, parse_mode="HTML")
        retornar Verdadeiro
    exceto Exception como e:
        logger.error(f"Erro ao editar mensagem: {e}")
        retornar Falso

# =========================================================
# REGISTRO
# =========================================================

def aplicar_regras_a partir_de_valores(minuto: Optional[int], cantos: int, casa: int = None, fora: int = None) -> List[str]:
    verificações: List[str] = []
    Se o minuto for Nenhum:
        cheques de devolução

    se 15 <= minuto <= 35 e cantos == 4:
        checks.append("1ï¸ âƒ£ Acima de HT > 4.5")

    se 55 <= minuto <= 75 e cantos em (8, 9):
        checks.append("2ï¸ âƒ£ Over FT > 9.5")

    Se o tempo for maior ou igual a 12 minutos, e houver 3 ou mais escanteios, e o time da casa não for None, nem o time visitante também não será None:
        se abs(casa - fora) >= 3:
            checks.append("3ï¸ âƒ£ Próximo Escanteio")

    Se o minuto for maior ou igual a 30 e o estado "casa" não for None e o estado "fora de casa" não for None:
        se abs(casa - fora) >= 3 e cantos >= 6:
            checks.append("4ï¸ âƒ£ AH cantos asiáticos")

    Se o minuto for maior ou igual a 25 e o local de origem não for None e o local de destino não for None:
        se abs(casa - fora) >= 2 e cantos >= 5:
            checks.append("5ï¸ âƒ£ Cantos por equipe")

    Se o minuto for maior ou igual a 35 e o local de origem não for None e o local de destino não for None:
        Se em casa for maior ou igual a 3 e fora de casa for maior ou igual a 3:
            checks.append("6ï¸ âƒ£ Ambos Times Cantos")

    Se o minuto for maior ou igual a 15 e os cantos forem maiores ou iguais a 4:
        mídia = cantos / máximo(minuto, 1)
        se a média for >= 0,20:
            checks.append("7ï¸ âƒ£ Pressão para próximo canto")

    cheques de devolução

# =========================================================
# ANALISTA
# =========================================================

classe IntelligentAnalyzer:
    @staticmethod
    def generate_checklist(stats: Dict, minute: int) -> str:
        total_de_cantos = estatísticas["total_de_cantos"]
        cantos_casa = estatísticas["cantos_casa"]
        cantos_distantes = estatísticas["cantos_distantes"]

        ritmo_5 = "Alto" if cantos_total >= 3 else "Médio" if cantos_total >= 2 else "Baixo"
        ritmo_10 = "Alto" if cantos_total >= 5 else "Médio" if cantos_total >= 3 else "Baixo"

        se cantos_casa > cantos_fora + 1:
            dominante = "Mandante"
        senão se cantos_fora > cantos_em_casa + 1:
            dominante = "Visitante"
        outro:
            dominante = "Equilibrado"

        msg = "\nðŸ“‹ <b>Lista de verificação completa:</b>\n"
        msg += f"â ± Minuto: {minuto}\n"
        msg += f"ðŸš© Cantos totais: {corners_total}\n"
        msg += f"ðŸ“Š Cantos: {corners_home} (Casa) x {corners_away} (Fora)\n"
        msg += f"âš¡ Ritmo últimos 5min: {ritmo_5}\n"
        msg += f"ðŸ“ˆ Ritmo Últimos 10min: {ritmo_10}\n"
        msg += f"ðŸ'' Tempo dominante: {dominante}\n"
        mensagem de retorno

    @staticmethod
    def predict_next_corner_side(stats: Dict, home: str, away: str):
        se stats["corners_home"] > stats["corners_away"]:
            return "Mandante", f"{home} tem mais cantos"
        elif stats["corners_away"] > stats["corners_home"]:
            return "Visitante", f"{away} tem mais cantos"
        return "Equilibrado", "Jogo equilibrado"

    @staticmethod
    def gerar_sugestões(estatísticas: Dict, regras_acertadas: List[str], minuto: int, casa: str, fora: str):
        sugestões = []
        cantos_casa = estatísticas["cantos_casa"]
        cantos_distantes = estatísticas["cantos_distantes"]
        total = estatísticas["total_de_cantos"]

        próximo_lado, motivo = IntelligentAnalyzer.predict_next_corner_side(stats, casa, fora)

        se algum("Próximo" em r para r em rules_hit):
            sugestões.append(SugestãoDeAposta(
                bet_type="Próximo Escanteio",
                lado=próximo_lado,
                motivo=motivo,
                ímpar=0,0,
                cantos_na_entrada_da_casa=cantos_da_casa,
                cantos_na_entrada_distante=cantos_distantes,
                próximo_canto_previsto=próximo_lado,
                resultado="PENDENTE"
            ))

        se cantos_casa > cantos_fora:
            sugestões.append(SugestãoDeAposta(
                bet_type="Cantos por equipe",
                lado="Mandante",
                reason=f"{home} é o melhor no jogo",
                ímpar=0,0,
                cantos_na_entrada_da_casa=cantos_da_casa,
                cantos_na_entrada_distante=cantos_distantes,
                resultado="PENDENTE"
            ))
        senão se cantos_fora > cantos_em_casa:
            sugestões.append(SugestãoDeAposta(
                bet_type="Cantos por equipe",
                lado="Visitante",
                reason=f"{away} é o melhor no jogo",
                ímpar=0,0,
                cantos_na_entrada_da_casa=cantos_da_casa,
                cantos_na_entrada_distante=cantos_distantes,
                resultado="PENDENTE"
            ))

        Se o minuto for menor ou igual a 35 e o total for maior ou igual a 4:
            sugestões.append(SugestãoDeAposta(
                bet_type="Mais de 4,5 no intervalo",
                lado=Nenhum,
                razão = "Ritmo alto para bater +4.5 HT",
                ímpar=0,0,
                cantos_na_entrada_da_casa=cantos_da_casa,
                cantos_na_entrada_distante=cantos_distantes,
                resultado="PENDENTE"
            ))

        Se o minuto for menor ou igual a 70 e o total for maior ou igual a 6:
            sugestões.append(SugestãoDeAposta(
                bet_type="Mais de 9,5 minutos finais",
                lado=Nenhum,
                razão="Bom ritmo de cantos",
                ímpar=0,0,
                cantos_na_entrada_da_casa=cantos_da_casa,
                cantos_na_entrada_distante=cantos_distantes,
                resultado="PENDENTE"
            ))

        sugestões de retorno

# =========================================================
# AVALIADOR DE RESULTADOS
# =========================================================

classe ResultEvaluator:
    @staticmethod
    def evaluate_suggestion(sug: BetSuggestion, md: MatchData, current_stats: Dict, minute: int) -> Optional[str]:
        """
        Retorno "GREEN", "RED" ou None (ainda pendente)
        """
        aposta = tipo_de_aposta_sugerido
        
        # Próximo Escanteio - avalia assim que acontecer
        se "Próximo" em aposta:
            se md.next_corner_after_entry:
                se sug.predicted_next_corner == "Equilibrado":
                    retornar "VERDE"
                Retorna "VERDE" se sug.predicted_next_corner == md.next_corner_after_entry senão "VERMELHO"
            return Nenhum # Ainda aguardando
        
        # Cantos por equipe - avalia no final do jogo
        se "Cantos por equipe" estiver apostado:
            se não md.is_finished:
                retornar Nenhum
            se sug.side == "Mandante":
                Retorna "VERDE" se md.final_corners_home > sug.corners_at_entry_home senão "VERMELHO"
            se sug.side == "Visitante":
                Retorna "VERDE" se md.final_corners_away > sug.corners_at_entry_away senão "VERMELHO"
        
        # Over HT 4.5 - avalia no intervalo (minuto 45+)
        Se a aposta for "Mais de HT":
            se minute >= 45 e md.half_time_corners não for None:
                Retorna "VERDE" se md.half_time_corners >= 5, caso contrário, retorna "VERMELHO".
            retornar Nenhum
        
        # Acima de FT 9.5 - avalia no final
        Se a aposta for "Mais de FT":
            se não md.is_finished:
                retornar Nenhum
            total = md.final_corners_home + md.final_corners_away
            Retorna "VERDE" se o total for maior ou igual a 10, caso contrário, retorna "VERMELHO".
        
        retornar Nenhum

    @staticmethod
    async def update_match_results(md: MatchData, current_stats: Dict, minute: int):
        """
        Avaliar todas as sugestões e atualizar a mensagem
        """
        has_update = False
        verdes = 0
        vermelhos = 0
        pendente = 0
        
        para sug em md.suggestions:
            se sug.result == "PENDENTE":
                resultado = ResultEvaluator.evaluate_suggestion(sug, md, current_stats, minute)
                se o resultado for:
                    resultado.sug = resultado
                    has_update = True
                    se o resultado for igual a "VERDE":
                        verdes += 1
                        bot_stats.add_result(True)
                    outro:
                        vermelhos += 1
                        bot_stats.add_result(False)
                outro:
                    pendente += 1
            elif sug.result == "VERDE":
                verdes += 1
            elif sug.result == "VERMELHO":
                vermelhos += 1
        
        # Atualiza mensagem se houver mudanças
        se has_update e md.message_id:
            mensagem_atualizada = formatar_mensagem_resultado(md, estatísticas_atuais, minuto, verdes, vermelhos, pendentes)
            aguardar safe_edit(md.message_id, updated_msg)
            logger.info(f"âœ… Resultados atualizados: {greens}G {reds}R {pending}P")
        
        # Marca como resultado atualizado se tudo foi avaliado
        se pending == 0 e não md.result_updated:
            md.result_updated = True
            logger.info(f"ðŸ Jogo finalizado: {md.home_team} vs {md.away_team}")

# =========================================================
# FORMATADORES DE MENSAGEM
# =========================================================

def format_entry_message(md: MatchData, stats: Dict, minute: int, rules: List[str], suggestions: List[BetSuggestion]) -> str:
    rules_text = "\n".join(rules)
    msg = f"ðŸš¨ <b>ENTRADA DETECTADA</b> ðŸš¨\n\n"
    msg += f"âš½ <b>{esc_html(md.home_team)} vs {esc_html(md.away_team)}</b>\n"
    msg += f"ðŸ † {esc_html(md.league)}\n"
    msg += f"â ± Minuto: {minuto}'\n\n"
    msg += f"ðŸ“Š <b>Escanteios na entrada:</b>\n"
    msg += f"ðŸ Casa: {stats['corners_home']}\n"
    msg += f"âœˆï¸ Fora: {stats['corners_away']}\n"
    msg += f"ðŸ“ˆ Total: {stats['corners_total']}\n\n"
    msg += f"âœ… <b>Regras ativadas:</b>\n{rules_text}\n\n"
    msg += f"ðŸ'¡ <b>Sugestões:</b>\n"
    
    para i, sug em enumerate(sugestões, 1):
        side_txt = f" ({sug.side})" se sug.side senão ""
        msg += f"\n{i}. {sug.bet_type}{side_txt}\n"
        msg += f" ðŸ“ {sug.reason}\n"
        msg += f" â ³ Status: AGUARDANDO...\n"
    
    mensagem de retorno

def format_result_message(md: MatchData, stats: Dict, minute: int, greens: int, reds: int, pending: int) -> str:
    msg = f"ðŸŽ¯ <b>ATUALIZAÇÃO DE RESULTADO</b>\n\n"
    msg += f"âš½ <b>{esc_html(md.home_team)} vs {esc_html(md.away_team)}</b>\n"
    msg += f"ðŸ † {esc_html(md.league)}\n"
    msg += f"â ± Minuto atual: {minuto}'\n\n"
    msg += f"ðŸ“Š <b>Escanteios atuais:</b>\n"
    msg += f"ðŸ Casa: {stats['corners_home']} (entrada: {md.corners_at_entry_home})\n"
    msg += f"âœˆï¸ Fora: {stats['corners_away']} (entrada: {md.corners_at_entry_away})\n"
    msg += f"ðŸ“ˆ Total: {stats['corners_total']}\n\n"
    msg += f"ðŸ'¡ <b>Resultados das Sugestões:</b>\n"
    
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
        
        msg += f"\n{emoji} {i}. {sug.bet_type}{side_txt}\n"
        msg += f" ðŸ“ {sug.reason}\n"
        msg += f" ðŸŽ¯ Status: <b>{status}</b>\n"
    
    msg += f"\nâ” â” â” â” â” â” â” â” â” â” â” â” â” â” â” â” â” â” â” â” \n"
    msg += f"ðŸ“Š <b>Resumo:</b>\n"
    msg += f"âœ… Verduras: {verdes}\n"
    msg += f"â Œ Vermelhos: {vermelhos}\n"
    msg += f"â ³ Pendentes: {pendente}\n"
    
    se pendente == 0:
        taxa de vitórias = (verdes / (verdes + vermelhos) * 100) se (verdes + vermelhos) > 0 senão 0
        msg += f"\nðŸ <b>JOGO FINALIZADO</b>\n"
        msg += f"ðŸ“ˆ Taxa correta: {winrate:.1f}%"
    
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

        await safe_send(f"""<b>ðŸ”¥ CornerBot PRO - Sistema de Resultados Ativo</b>

âœ… Sistema iniciado
Limite: 110 req/dia
ðŸŽ¯ {len(PRIORITY_LEAGUES)} liga prioridades
â ° Intervalo dinâmico
ðŸŽ² Avaliação automática de resultados

<i>O bot agora mostra Verde/Vermelho automaticamente!</i>""")

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
                    relatório = f"{req_counter.get_stats()}\n{bot_stats.get_summary()}\nðŸ”„ Ciclo #{cycles_count}"
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
