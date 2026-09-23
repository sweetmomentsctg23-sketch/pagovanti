import requests

TELEGRAM_BOT_TOKEN = "8075556042:AAFoz2S2xiLqDV_gEm0qc-HsxdbSNFm-nIM"
TELEGRAM_CHAT_ID = "5352335307"

def enviar_mensaje_telegram(texto: str):
    if TELEGRAM_BOT_TOKEN == "TU_BOT_TOKEN_AQUI":
        # La consola cp1252 de Windows no puede imprimir emojis directamente.
        texto_consola = texto.encode("ascii", "backslashreplace").decode("ascii")
        print("[Telegram Simulado]:", texto_consola)
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=5)
        return r.status_code == 200
    except Exception as e:
        print("Error enviando Telegram:", e)
        return False