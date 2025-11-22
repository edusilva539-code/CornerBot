import asyncio
import requests
from telegram import Bot

# ==========================
# CONFIGURAÇÕES
# ==========================
API_KEY = "74e372055593a55e7cbcc79df1097907"   # SUA KEY NOVA
BASE_URL = "https://v3.football.api-sports.io"
TELEGRAM_TOKEN = "8239858396:AAEohsJJcgJwaCC4ioG1ZEek4HesI3NhwQ8"
CHAT_ID = 441778236

bot = Bot(token=TELEGRAM_TOKEN)

HEADERS = {
    "x-apisports-key": API_KEY,
}

# ==========================
# FUNÇÃO: PEGAR JOGOS AO VIVO
# ==========================
def get_live_matches():
    url = f"{BASE_URL}/fixtures"
    params = {"live": "all"}
    r = requests.get(url, headers=HEADERS, params=params)
    return r.json().get("response", [])


# ==========================
# REGRA 1 – Over HT Corners
# ==========================
def rule_over_ht(match):
    stats = match.get("statistics", [])
    minute = match["fixture"]["status"]["elapsed"]
    if minute is None or minute > 45:
        return None

    total = get_corners(match)
    if total >= 5:
        return f"🔥 Over HT 4.5 – {total} cantos aos {minute}min"
    return None


# ==========================
# REGRA 2 – Over FT Corners
# ==========================
def rule_over_ft(match):
    stats = match.get("statistics", [])
    minute = match["fixture"]["status"]["elapsed"]
    total = get_corners(match)

    if minute >= 70 and total >= 9:
        return f"🔥 Over FT 9.5 – {total} cantos aos {minute}min"
    return None


# ==========================
# REGRA 3 – Próximo Escanteio
# ==========================
def rule_next_corner(match):
    pressure = detect_pressure(match)
    if pressure >= 70:
        return f"⚡ Próximo escanteio provável – pressão {pressure}%"
    return None


# ==========================
# REGRA 4 – Asiático de Escanteios
# ==========================
def rule_asian(match):
    total = get_corners(match)
    minute = match["fixture"]["status"]["elapsed"]

    if minute >= 60 and total in [7, 8]:
        return f"📘 AH +1 Corner – total atual {total}"
    return None


# ==========================
# REGRA 5 – Escanteios por equipe
# ==========================
def rule_team_corner(match):
    hc, ac = get_team_corners(match)
    minute = match["fixture"]["status"]["elapsed"]

    if minute >= 30 and hc >= 4:
        return f"🟦 Casa > 4 cantos – {hc} até agora"

    if minute >= 30 and ac >= 4:
        return f"🟥 Fora > 4 cantos – {ac} até agora"

    return None


# ==========================
# REGRA 6 – Ambos Times Cantos
# ==========================
def rule_btts_corners(match):
    hc, ac = get_team_corners(match)
    minute = match["fixture"]["status"]["elapsed"]

    if minute >= 35 and hc >= 2 and ac >= 2:
        return f"🔰 Ambos Teams Cantos OK – {hc}/{ac}"
    return None


# ==========================
# REGRA 7 – Bot de Pressão Alta
# ==========================
def rule_high_pressure(match):
    pressure = detect_pressure(match)
    if pressure >= 75:
        return f"🔥⚡ Pressão MUITO ALTA: {pressure}%"
    return None


# ==========================
# FUNÇÕES AUXILIARES
# ==========================
def get_corners(match):
    try:
        return match["statistics"][0]["statistics"][6]["value"] + match["statistics"][1]["statistics"][6]["value"]
    except:
        return 0

def get_team_corners(match):
    try:
        home = match["statistics"][0]["statistics"][6]["value"]
        away = match["statistics"][1]["statistics"][6]["value"]
        return home, away
    except:
        return 0, 0

def detect_pressure(match):
    try:
        attacks = match["statistics"][0]["statistics"][12]["value"] + match["statistics"][1]["statistics"][12]["value"]
        dangerous = match["statistics"][0]["statistics"][13]["value"] + match["statistics"][1]["statistics"][13]["value"]
        total = attacks + dangerous
        return int((dangerous / total) * 100)
    except:
        return 0


# ==========================
# EXECUÇÃO PRINCIPAL
# ==========================
async def main():
    await bot.send_message(chat_id=CHAT_ID, text="⚽ CornerBot – API SPORTS iniciado!")

    while True:
        matches = get_live_matches()

        for match in matches:
            rules = [
                rule_over_ht(match),
                rule_over_ft(match),
                rule_next_corner(match),
                rule_asian(match),
                rule_team_corner(match),
                rule_btts_corners(match),
                rule_high_pressure(match),
            ]

            for alert in rules:
                if alert:
                    await bot.send_message(chat_id=CHAT_ID, text=alert)

        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
