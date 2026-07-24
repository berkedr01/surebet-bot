import os
import re
import json
import time
import hashlib


FIXED_LINKS = """https://heylink.me/sekaguncel_
http://dub.run/jojoyagit
http://dub.is/matguncel"""
CALC_LINK = "https://tr.apostasseguras.com/calculator?model=auto"
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = os.environ.get("SUREBET_URL", "https://tr.apostasseguras.com/surebets")

# Telegram'da en altta göstermek istediğin hesaplama linki
CALC_URL = "https://tr.apostasseguras.com/calculator?model=auto"

BASE_URL = "https://tr.apostasseguras.com"

# sayfa okuma aralığı
INTERVAL_SEC = int(os.environ.get("INTERVAL_SEC", "20"))

# Telegram komutlarını kontrol etme aralığı
COMMAND_POLL_SEC = float(os.environ.get("COMMAND_POLL_SEC", "1"))

# Vbetholi filtresi eşiği
VB_THRESHOLD = float(os.environ.get("VB_THRESHOLD", "10"))

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
CHAT_ID = os.environ.get("TG_CHAT_ID")

STATE_PATH = os.environ.get("STATE_PATH", "storage_state.json")
SEEN_FILE = os.environ.get("SEEN_FILE", "seen.json")

# Artık sadece Vbetholi filtresini kullanıyoruz.
FILTER_NAME = os.environ.get("FILTER_NAME", "Vbetholi")


def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TG_BOT_TOKEN veya TG_CHAT_ID ayarlı değil. CMD'de set etmelisin.")
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        api,
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


def send_telegram_photo(photo_path: str, caption: str | None = None) -> None:
    """Telegram'a fotoğraf gönder."""
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TG_BOT_TOKEN veya TG_CHAT_ID ayarlı değil. CMD'de set etmelisin.")

    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        files = {"photo": (os.path.basename(photo_path), f, "image/png")}
        data = {"chat_id": CHAT_ID}
        if caption:
            data["caption"] = caption
        r = requests.post(api, data=data, files=files, timeout=30)
        r.raise_for_status()


def fetch_telegram_updates(offset: int | None = None) -> list[dict]:
    """Telegram'dan gelen yeni mesajları al."""
    if not BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN ayarlı değil.")

    api = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = offset

    r = requests.get(api, params=params, timeout=10)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError("Telegram getUpdates isteği başarısız oldu.")
    return payload.get("result", [])


def initialize_telegram_offset() -> int | None:
    """Bot açılırken eski komutları atla; yalnızca yeni mesajları işle."""
    try:
        updates = fetch_telegram_updates()
    except Exception as e:
        print("[TELEGRAM] Komut bağlantısı kurulamadı:", e)
        return None

    if not updates:
        return 0
    return max(int(update["update_id"]) for update in updates) + 1


def poll_telegram_commands(offset: int) -> tuple[int, list[str]]:
    """Yalnızca ayarlı sohbetten gelen dur/devam/restart komutlarını döndür."""
    try:
        updates = fetch_telegram_updates(offset)
    except Exception as e:
        print("[TELEGRAM] Komutlar okunamadı:", e)
        return offset, []

    next_offset = offset
    commands = []

    for update in updates:
        next_offset = max(next_offset, int(update["update_id"]) + 1)
        message = update.get("message") or {}
        incoming_chat_id = str((message.get("chat") or {}).get("id", ""))
        if incoming_chat_id != str(CHAT_ID):
            continue

        text = (message.get("text") or "").strip()
        if not text:
            continue

        command = text.split()[0].lower().split("@", 1)[0].lstrip("/")
        if command in {"dur", "devam", "restart"}:
            commands.append(command)

    return next_offset, commands

def load_seen() -> set[str]:
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    # çok büyümesin diye son 3000 kaydı tut
    data = sorted(list(seen))[-3000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_percent(txt: str):
    # %11,3  |  11,3% | 11.3%  -> float
    if not txt:
        return None
    t = txt.strip()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%|%\s*(\d+(?:[.,]\d+)?)", t)
    if not m:
        return None
    s = (m.group(1) or m.group(2)).replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def normalize_row_text(t: str) -> str:
    """
    Event kimliği üretirken 'değişken/önemsiz' satırları yok sayıyoruz.
    Amaç: aynı fırsatın küçük UI değişimleri yüzünden tekrar bildirim atmasını engellemek.
    """
    t = (t or "").lower()

    # 10dk, 11 dk, 45s, 12sn, 3 min gibi dinamik süreleri sil
    t = re.sub(r"\b\d+\s*(sn|saniye|s|dk|dakika|d|min|minute|saat|hour|h|g|gun|gün)\b", " ", t, flags=re.I)

    # "bu etkinlik için +X kesin bahis" satırı değiştikçe bot tekrar bildirim atmasın
    # Satır bazlı ve metin-içi yakalama (UI bazen satırları birleştiriyor)
    t = re.sub(
        r"(?im)^\s*bu\s*etkinlik\s*için\s*\+?\s*\d+\s*kesin\s*bahis\s*$",
        " ",
        t,
    )
    t = re.sub(
        r"bu\s*etkinlik\s*için\s*\+?\s*\d+\s*kesin\s*bahis",
        " ",
        t,
        flags=re.I,
    )

    # fazla boşlukları toparla
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_event_identity(text: str) -> str:
    """
    Aynı etkinlik/fırsat için kimlik üretir:
    - 'kesin bahis' satırını, süreyi vs yok sayar
    - SADECE event kimliği olsun diye kâr(%) satırlarını da temizler
      (kâr değişimini ayrıca key'e ekleyeceğiz)
    """
    t = (text or "")
    # önce genel temizlik
    t = normalize_row_text(t)

    # sadece yüzde/kar satırı olan satırları çıkar:
    # örn: "%8,94" veya "8,94%" gibi
    # normalize_row_text'ten sonra metin tek satıra indiği için hem satır başı hem metin içi temizliyoruz.
    t = re.sub(r"(?i)\b%?\s*\d+(?:[.,]\d+)?\s*%?\b", lambda m: m.group(0) if False else " ", t)

    # ama yukarıdaki regex event içinde skor/tarih gibi sayıları da silebilir.
    # O yüzden daha güvenlisi: "kar satırı" formatını hedefleyelim:
    # "kâr: %8.94" gibi veya tek başına "%8,94" gibi satırlar.
    # Metin tek satıra indiği için, bu ikisini metin-içi yakalayıp siliyoruz:
    t = re.sub(r"(?i)k[aâ]r\s*:\s*%?\s*\d+(?:[.,]\d+)?", " ", t)
    t = re.sub(r"(?i)\b%\s*\d+(?:[.,]\d+)?\b", " ", t)

    t = re.sub(r"\s+", " ", t).strip()
    return t


def make_key(filter_name: str, profit: float, text: str) -> str:
    """
    Aynı event + aynı kâr (2 ondalık) => aynı key (tekrar mesaj yok)
    Event aynı ama kâr değişirse => yeni key (tekrar mesaj var)
    """
    identity = normalize_event_identity(text)
    profit_tag = f"{profit:.2f}"
    raw = f"{filter_name}|{identity}|{profit_tag}"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def pretty_text(t: str) -> str:
    t = (t or "").strip()
    t = t.replace("•", "").replace("○", "").replace("●", "").strip()
    t = re.sub(r"[ \t]+", " ", t)

    lines = [ln.strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]

    head = []
    piyasa = []
    oran = []
    rest = []

    def is_odds_line(s: str) -> bool:
        return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", s))

    def is_market_line(s: str) -> bool:
        s2 = s.lower()
        kws = ["üzer", "alt", "uzatma", "handikap", "raunt", "toplam", "1x2", "harita", "set", "gol"]
        return any(k in s2 for k in kws)

    for ln in lines:
        if parse_percent(ln) is not None or re.match(r"^%\s*\d", ln):
            head.append(ln)
            continue

        if re.fullmatch(r"\d+\s*(sn|s|dk|min|saat|h|d)", ln, flags=re.I):
            head.append(ln)
            continue

        if is_odds_line(ln):
            oran.append(ln.replace(",", "."))
            continue

        if is_market_line(ln):
            piyasa.append(ln)
            continue

        rest.append(ln)

    out_lines = []
    if head:
        out_lines += head
        out_lines.append("")

    out_lines += rest

    if piyasa:
        out_lines.append("")
        out_lines.append("📌 Piyasa")
        for x in piyasa:
            out_lines.append(f"- {x}")

    if oran:
        out_lines.append("")
        out_lines.append("💰 İkramiye oranı")
        for x in oran:
            out_lines.append(f"- {x}")

    out = "\n".join(out_lines)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def is_logged_in(page) -> bool:
    url = (page.url or "").lower()
    if "login" in url or "signin" in url:
        return False
    try:
        content = page.content()
        return ("Kâr" in content) or ("surebet" in content.lower())
    except Exception:
        return True


def ensure_scanning_on(page):
    def has_pause_hint() -> bool:
        try:
            body = page.locator("body").inner_text(timeout=5000).lower()
        except Exception:
            return False
        return ("shift+p" in body) and ("duraklat" in body)

    def press_shift_p():
        page.keyboard.down("Shift")
        page.keyboard.press("KeyP")
        page.keyboard.up("Shift")
        page.wait_for_timeout(1200)

    if has_pause_hint():
        return False

    press_shift_p()

    if not has_pause_hint():
        press_shift_p()

    return True


def set_site_filter(page, filter_name: str) -> bool:
    try:
        filter_label = page.locator("text=Filtre").first
        sel = filter_label.locator("xpath=following::select[1]")

        sel.select_option(label=filter_name)
        page.wait_for_timeout(800)

        selected = sel.locator("option:checked").inner_text()
        print(f"[FILTRE] secildi -> {selected}")

        return True
    except Exception as e:
        print(f"[FILTRE] Seçilemedi: {filter_name} | Hata: {e}")
        return False


def get_site_filter(page) -> str | None:
    """Sitede şu an seçili olan filtre label'ını döndür."""
    try:
        filter_label = page.locator("text=Filtre").first
        sel = filter_label.locator("xpath=following::select[1]")
        return sel.locator("option:checked").inner_text().strip()
    except Exception:
        return None


def get_calc_url_from_row(page, row):
    """Satırdaki hesap makinesi ikonunun açtığı calculator URL'sini bulur.

    Surebet'te ikon genelde şu yapıdadır:
      <form target="_blank" method="get" action="/calculator/show/<id>?model=surebet"> ... </form>

    Bu yüzden en sağlam yöntem: aynı satır içindeki form'un action attribute'unu okumaktır.
    """
    try:
        # 1) En sağlam: satır içindeki form[action*='/calculator/show/']
        form = row.locator("form[action*='/calculator/show/'], form[action*='calculator/show/']").first
        if form.count() > 0:
            action = form.get_attribute("action")
            if action:
                action = action.strip()
                if action.startswith("http"):
                    return action
                if action.startswith("/"):
                    return BASE_URL + action
                return BASE_URL + "/" + action.lstrip("/")

        # 2) Alternatif: <a href='...calculator/show...'>
        a = row.locator("a[href*='/calculator/show/'], a[href*='calculator/show/']").first
        if a.count() > 0:
            href = a.get_attribute("href")
            if href:
                href = href.strip()
                if href.startswith("http"):
                    return href
                if href.startswith("/"):
                    return BASE_URL + href
                return BASE_URL + "/" + href.lstrip("/")

        # 3) Son çare: eski sabit linke düşsün
        return None
    except Exception:
        return None

def scrape_rows(page):
    page.wait_for_timeout(1500)

    rows = page.locator("table tbody tr")
    n = rows.count()

    results = []

    i = 0
    while i < min(n, 1500):
        row = rows.nth(i)
        txt = row.inner_text().strip()

        if not txt:
            i += 1
            continue

        p = parse_percent(txt)
        if p is None:
            i += 1
            continue

        # ✅ Bu fırsatın hesap makinesi linkini ilk satırdan yakala
        calc_url = get_calc_url_from_row(page, row)

        group_lines = [txt]

        j = i + 1
        while j < min(n, 1500):
            nxt = rows.nth(j).inner_text().strip()
            if not nxt:
                j += 1
                continue

            if parse_percent(nxt) is not None:
                break

            group_lines.append(nxt)
            j += 1

        full_txt = "\n".join(group_lines)
        full_txt = re.sub(r"\s+\n", "\n", full_txt)
        full_txt = re.sub(r"\n{3,}", "\n\n", full_txt)

        # Bu fırsatın UI'daki hangi satırlardan oluştuğunu da saklıyoruz
        results.append(
            {
                "profit": p,
                "text": full_txt,
                "start_i": i,
                "end_i": j,
                "calc_url": calc_url or CALC_LINK,  # fallback: eski sabit link
            }
        )
        i = j

    return results


def screenshot_rows_group(page, start_i: int, end_i: int, out_path: str) -> bool:
    """table tbody tr satır aralığını (start_i dahil, end_i hariç) tek görsel olarak kaydet."""
    rows = page.locator("table tbody tr")

    pad_x = int(os.getenv("SS_PAD_X", "6"))
    pad_y = int(os.getenv("SS_PAD_Y", "6"))
    max_retry = int(os.getenv("SS_RETRY", "3"))

    for attempt in range(1, max_retry + 1):
        # İlk satırı görünür alana getir
        try:
            rows.nth(start_i).scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(200)
        except Exception:
            pass

        min_x = min_y = None
        max_x = max_y = None

        for k in range(start_i, end_i):
            loc = rows.nth(k)
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass

            try:
                bb = loc.bounding_box()
            except Exception:
                bb = None

            if not bb:
                continue

            x, y, w, h = bb["x"], bb["y"], bb["width"], bb["height"]
            if min_x is None:
                min_x, min_y = x, y
                max_x, max_y = x + w, y + h
            else:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x + w)
                max_y = max(max_y, y + h)

        # Bazı anlarda satırlar DOM'da yeniden çizilir; kısa bekleyip tekrar dene
        if min_x is None:
            if attempt < max_retry:
                page.wait_for_timeout(300)
                continue
            return False

        clip = {
            "x": max(0, min_x - pad_x),
            "y": max(0, min_y - pad_y),
            "width": max(2, (max_x - min_x) + pad_x * 2),
            "height": max(2, (max_y - min_y) + pad_y * 2),
        }

        try:
            page.screenshot(path=out_path, clip=clip)
            return True
        except Exception:
            if attempt < max_retry:
                page.wait_for_timeout(300)
                continue
            return False

    return False


def main():
    if not os.path.exists(STATE_PATH):
        print(f"❌ {STATE_PATH} bulunamadı. Önce python save_session.py ile login session kaydet.")
        return

    seen = set()  # her açılışta sıfırdan başla

    restart_requested = False

    with sync_playwright() as p:
        # Railway gibi sunucularda headless şart
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=STATE_PATH)
        page = context.new_page()

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        if not is_logged_in(page):
            send_telegram("❌ Oturum geçersiz görünüyor. save_session.py ile yeniden giriş yapıp session kaydetmen lazım.")
            browser.close()
            return

        send_telegram("✅ Fırsat bot başlatıldı")

        # Sadece tek filtre kullanıyoruz
        set_site_filter(page, FILTER_NAME)
        ensure_scanning_on(page)

        command_offset = initialize_telegram_offset()
        paused = False

        while True:
            try:
                if command_offset is None:
                    command_offset = initialize_telegram_offset()
                else:
                    command_offset, commands = poll_telegram_commands(command_offset)
                    for command in commands:
                        if command == "restart":
                            restart_requested = True
                            try:
                                send_telegram("♻️ Fırsat bot yeniden başlatılıyor")
                            except Exception:
                                pass
                            break
                        if command == "dur" and not paused:
                            paused = True
                            send_telegram("⏸ Bot duraklatıldı")
                        elif command == "devam" and paused:
                            paused = False
                            send_telegram("▶️ Bot devam ediyor")

                if restart_requested:
                    break

                if paused:
                    time.sleep(COMMAND_POLL_SEC)
                    continue

                ensure_scanning_on(page)
                if not is_logged_in(page):
                    send_telegram("⚠️ Oturum düşmüş görünüyor. save_session.py ile session'ı yenile.")
                    break

                items = scrape_rows(page)

                max_p = max([x["profit"] for x in items], default=0)
                print(f"Gorulen satir: {len(items)} | Max kar: {max_p} | Filtre: {FILTER_NAME}")

                thr = VB_THRESHOLD
                hits = [x for x in items if x["profit"] >= thr]
                print(f"Eşik üstü: {len(hits)} (Filtre: {FILTER_NAME} | thr={thr})")

                hits.sort(key=lambda x: x["profit"], reverse=True)

                for h in hits:
                    key = make_key(FILTER_NAME, h["profit"], h["text"])
                    if key in seen:
                        continue
                    seen.add(key)

                    # İstenen değişiklik: metin yerine, o fırsatın bulunduğu satırların ekran görüntüsünü gönder
                    ts = int(time.time())
                    shot_path = os.path.join("/tmp", f"shot_{h['profit']:.2f}_{ts}.png")
                    ok = screenshot_rows_group(page, h["start_i"], h["end_i"], shot_path)

                    # ✅ Burada artık her fırsat kendi calc_url'sini kullanıyor
                    calc = h.get("calc_url") or CALC_LINK
                    caption = f"🚨 Kar: %{h['profit']:.2f}"

                    if ok:
                        send_telegram_photo(shot_path, caption=caption)
                        try:
                            os.remove(shot_path)
                        except Exception:
                            pass
                    else:
                        # Screenshot alamazsak en azından metinle düşmesin
                        send_telegram(caption)

                save_seen(seen)

            except PWTimeout:
                print("Timeout oldu, tekrar denenecek...")
            except Exception as e:
                print("ERR:", e)

            time.sleep(INTERVAL_SEC)

        browser.close()

    if restart_requested:
        print("[RESTART] Bot yeniden başlatılıyor...")
        return True

    return False


if __name__ == "__main__":
    while main():
        time.sleep(1)