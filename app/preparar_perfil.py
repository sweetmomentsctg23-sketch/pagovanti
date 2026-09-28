import os
from playwright.sync_api import sync_playwright

DATA_DIR = r"C:\vanti_chrome_data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")

def preparar_perfil():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

    with sync_playwright() as p:
        # Abrimos contexto limpio para capturar cookies
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        print("\n1. La página cargará. Haz la consulta manualmente.")
        print("2. Presiona ENTER en la consola cuando finalice exitosamente.\n")

        page.goto("https://pagosenlinea.grupovanti.com/", wait_until="networkidle")

        input("Presiona ENTER aquí cuando la consulta funcione en la pantalla...")

        # Guarda las cookies y almacenamiento local en un archivo JSON independiente
        context.storage_state(path=STATE_FILE)
        browser.close()
        print(f"¡Estado y cookies guardados con éxito en: {STATE_FILE}!")

if __name__ == "__main__":
    preparar_perfil()