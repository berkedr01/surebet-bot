import os
from playwright.sync_api import sync_playwright

URL = os.environ.get("SUREBET_URL", "https://tr.apostasseguras.com/surebets")
STATE_PATH = os.environ.get("STATE_PATH", "storage_state.json")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # görünür
        context = browser.new_context()
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        print("\n1) Sitede giriş yap (login).")
        print("2) Giriş tamamlanınca buraya dön ve ENTER'a bas.\n")
        input("ENTER -> session kaydet: ")

        context.storage_state(path=STATE_PATH)
        print(f"✅ Session kaydedildi: {STATE_PATH}")

        browser.close()

if __name__ == "__main__":
    main()
