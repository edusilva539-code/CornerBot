import asyncio
import requests
from telegram import Bot

# ----------------------------
# CONFIGURAÇÕES DO SEU BOT
# ----------------------------
API_KEY = "f500ec36b5mshd40feb8f3fb438ap16eb00jsn71f83269e819"
HOST = "api-football-v1.p.rapidapi.com"

TELEGRAM_TOKEN = "8239858396:AAEohsJJcgJwaCC4ioG1ZEek4HesI3NhwQ8"
CHAT_ID = 441778236  # número, não string

bot = Bot(token=TELEGRAM_TOKEN)


# ----------------------------
# FUNÇÃO: busca jogos ao vivo
# ----------------------------
def get_live_matches():
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    params = {"live": "all"}
    headers = {
        "X-RapidAPI-Key": API_KEY,
        "X-RapidAPI-Host": HOST
    }

    try:
        response = requests.get(url, headers=headers, params=params)
        return response.json()
    except Exception as e:
        return {"response": [], "error": str(e)}


# ----------------------------
# LOOP PRINCIPAL
# ----------------------------
async def bot_loop():
    await bot.send_message(chat_id=CHAT_ID, text="🔥 CornerBot v20 iniciado com sucesso!")

    while True:
        data = get_live_matches()
        qtd = len(data.get("response", []))

        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"📊 Jogos ao vivo detectados: {qtd}"
        )

        await asyncio.sleep(60)  # 1 minuto


# ----------------------------
# INICIALIZAÇÃO
# ----------------------------
if __name__ == "__main__":
    asyncio.run(bot_loop())
