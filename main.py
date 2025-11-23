import asyncio
import requests
from telegram import Bot

API_KEY = "74e372055593a55e7cbcc79df1097907"   # SUA API SPORTS
BASE = "https://v3.football.api-sports.io"

TELEGRAM_TOKEN = "8239858396:AAEohsJJcgJwaCC4ioG1ZEek4HesI3NhwQ8"
CHAT_ID = 441778236

bot = Bot(token=TELEGRAM_TOKEN)


def get_live():
    url = f"{BASE}/fixtures"
    headers = {"x-apisports-key": API_KEY}
    params = {"live": "all"}
    r = requests.get(url, headers=headers, params=params)
    return r.json().get("response", [])


def get_corners(match):
    stats = match.get("statistics", [])
    total = 0
    for t in stats:
        for s in t.get("statistics", []):
            if s.get("type") == "Corner Kicks":
                total += s.get("value", 0)
    return total


def apply_rules(match):
    minute = match["fixture"]["status"]["elapsed"]
    corners = get_corners(match)

    checks = []

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


async def send(msg):
    return await bot.send_message(chat_id=CHAT_ID, text=msg)


async def edit(mid, text):
    await bot.edit_message_text(chat_id=CHAT_ID, message_id=mid, text=text)


async def main():
    await send("🔥 *CornerBot INICIADO* – monitorando jogos ao vivo...")

    sent = {}

    while True:
        matches = get_live()

        for m in matches:
            fid = m["fixture"]["id"]
            rules = apply_rules(m)

            # Detecta entrada
            if rules and fid not in sent:
                text = (
                    f"⚽ *Entrada Detectada!*\n"
                    f"📌 Jogo: {m['teams']['home']['name']} x {m['teams']['away']['name']}\n"
                    f"⏱ Minuto: {m['fixture']['status']['elapsed']}\n"
                    f"🚩 Cantos: {get_corners(m)}\n"
                    f"📊 Estratégias ativadas:\n" + "\n".join(rules)
                )

                msg = await send(text)
                sent[fid] = msg.message_id

            # Verifica fim do jogo
            if fid in sent:
                if m["fixture"]["status"]["short"] == "FT":
                    total_corners = get_corners(m)
                    tag = "✅ GREEN" if total_corners >= 10 else "❌ RED"

                    final_msg = (
                        f"🏁 *Jogo finalizado*\n"
                        f"Total de escanteios: {total_corners}\n"
                        f"Resultado: {tag}"
                    )

                    await edit(sent[fid], final_msg)
                    del sent[fid]

        await asyncio.sleep(20)


if __name__ == "__main__":
    asyncio.run(main())
