
import requests
import time
from telegram import Bot

API_KEY = "f500ec36b5mshd40feb8f3fb438ap16eb00jsn71f83269e819"
HOST = "api-football-v1.p.rapidapi.com"
TELEGRAM_TOKEN = "8239858396:AAEohsJJcgJwaCC4ioG1ZEek4HesI3NhwQ8"
CHAT_ID = "441778236"

bot = Bot(token=TELEGRAM_TOKEN)

def get_live():
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    params = {"live": "all"}
    headers = {"X-RapidAPI-Key": API_KEY, "X-RapidAPI-Host": HOST}
    r = requests.get(url, headers=headers, params=params)
    return r.json()

def main():
    bot.send_message(chat_id=CHAT_ID, text="CornerBot iniciado.")
    while True:
        data = get_live()
        bot.send_message(chat_id=CHAT_ID, text=f"Live jogos: {len(data.get('response',[]))}")
        time.sleep(60)

if __name__ == "__main__":
    main()
