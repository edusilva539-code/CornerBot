import asyncio
import requests
from telegram import Bot

API_KEY = "f500ec36b5mshd40feb8f3fb438ap16eb00jsn71f83269e819"
HOST = "api-football-v1.p.rapidapi.com"
TELEGRAM_TOKEN = "8239858396:AAEohsJJcgJwaCC4ioG1ZEek4HesI3NhwQ8"
CHAT_ID = 441778236   # coloque SEM aspas aqui, PTB v20 exige int

bot = Bot(token=TELEGRAM_TOKEN)

# ------- API FUNCTION -------
def get_live_matches():
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    params = {"live": "all"}
    headers = {
        "X-RapidAPI-Key": API_KEY,
        "X-RapidAPI-Host": HOST
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ------ MAIN LOOP ------
async def main():
    await bot.send_message(chat_id=CHAT_ID, text="⚽ CornerBot v20 iniciado com sucesso!")

    while True:
        data = get_live_matches()

        if "response" in data:
            total = len(data["response"])
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"🔄 Rodada verificada — jogos ao vivo: {total}"
            )
        else:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"❌ Erro ao consultar API: {data}"
            )

        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
