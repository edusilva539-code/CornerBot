import asyncio
import requests
from telegram import Bot

# =============================
# CONFIG
# =============================
API_KEY = "74e372055593a55e7cbcc79df1097907"
BASE_URL = "https://v3.football.api-sports.io"

TELEGRAM_TOKEN = "8239858396:AAEohsJJcgJwaCC4ioG1ZEek4HesI3NhwQ8"
CHAT_ID = 441778236

bot = Bot(token=TELEGRAM_TOKEN)

headers = {
    "x-apisports-key": API_KEY
}

# ==========================================
# BUSCA PARTIDAS AO VIVO (API SPORTS DIRETO)
# ==========================================
def get_live():
    url = f"{BASE_URL}/fixtures?live=all"
    r = requests.get(url, headers=headers)
    return r.json()

# ===============================
# REGRAS (A gente vai completar)
# ===============================

def rule_over_ht_corners(match):
    stats = match["statistics"]
    total = stats["corners"]
    minute = match["fixture"]["status"]["elapsed"]

    return minute >= 20 and total >= 4, f"OVER HT — {total} cantos aos {minute} min"

def rule_over_ft_corners(match):
    stats = match["statistics"]
    total = stats["corners"]
    minute = match["fixture"]["status"]["elapsed"]

    return minute >= 55 and total >= 7, f"OVER FT — {total} cantos aos {minute} min"

def rule_next_corner(match):
    pressure = match["pressure"]
    return pressure >= 70, f"PRÓXIMO CANTO — pressão {pressure}%"

def rule_asian_line(match):
    stats = match["statistics"]
    diff = abs(stats["home_corners"] - stats["away_corners"])
    return diff >= 2, "AH +1 Corner (desbalanceio detectado)"

def rule_team_corners(match):
    stats = match["statistics"]
    return stats["home_corners"] >= 4, "Cantos time da casa — 4 ou mais"

def rule_both_teams_corners(match):
    stats = match["statistics"]
    return stats["home_corners"] >= 2 and stats["away_corners"] >= 2, "Ambos Times Cantos"

def rule_high_pressure(match):
    pressure = match["pressure"]
    attacks = match["statistics"]["dangerous_attacks"]

    return pressure >= 60 and attacks >= 20, f"Pressão Alta — {attacks} ataques perigosos"


ALL_RULES = [
    rule_over_ht_corners,
    rule_over_ft_corners,
    rule_next_corner,
    rule_asian_line,
    rule_team_corners,
    rule_both_teams_corners,
    rule_high_pressure,
]

# ===============================
# LÓGICA PRINCIPAL
# ===============================
async def main():
    await bot.send_message(chat_id=CHAT_ID, text="CornerBot (API-SPORTS) Iniciado!")

    while True:
        data = get_live()
        matches = data.get("response", [])

        for match in matches:
            # MOCK da estrutura (vamos ajustar depois com dados reais)
            match["statistics"] = {
                "corners": 6,
                "home_corners": 3,
                "away_corners": 3,
                "dangerous_attacks": 22
            }
            match["pressure"] = 75

            for rule in ALL_RULES:
                ok, msg = rule(match)
                if ok:
                    await bot.send_message(chat_id=CHAT_ID, text="⚽ " + msg)

        await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
