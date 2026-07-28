import os
import re
import json
import time
import hashlib
from collections import deque


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
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "bot_settings.json")

LOG_BUFFER = deque(maxlen=200)
_ORIGINAL_PRINT = print


def bot_log(*values, sep=" ", end="\n", **kwargs) -> None:
    """Write to Docker stdout and keep a small in-memory Telegram log buffer."""
    message = sep.join(str(value) for value in values)
    for line in message.splitlines() or [""]:
        LOG_BUFFER.append(line)
    _ORIGINAL_PRINT(*values, sep=sep, end=end, **kwargs)


def get_recent_logs(limit: int = 10) -> str:
    lines = list(LOG_BUFFER)[-max(1, limit):]
    if not lines:
        return "Hen\u00fcz log olu\u015fmad\u0131."
    return "\n".join(lines)


def format_threshold(value: float) -> str:
    return f"{value:g}"


def parse_threshold_command(text: str) -> float | None:
    """Accept a plain number or 'limit NUMBER' from Telegram."""
    normalized = text.strip().lower().replace(",", ".")
    parts = normalized.split()
    if len(parts) == 2 and parts[0].lstrip("/").split("@", 1)[0] == "limit":
        normalized = parts[1]

    if not re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        return None

    value = float(normalized)
    if 0 <= value <= 100:
        return value
    return None


def load_runtime_threshold(default: float) -> float:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            value = float(json.load(f)["threshold"])
        if 0 <= value <= 100:
            return value
    except Exception:
        pass
    return default


def save_runtime_threshold(value: float) -> bool:
    temp_path = f"{SETTINGS_FILE}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({"threshold": value}, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, SETTINGS_FILE)
        return True
    except Exception as e:
        bot_log("[AYAR] Limit kaydedilemedi:", e)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return False


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
        bot_log("[TELEGRAM] Komut bağlantısı kurulamadı:", e)
        return None

    if not updates:
        return 0
    return max(int(update["update_id"]) for update in updates) + 1


def poll_telegram_commands(offset: int) -> tuple[int, list[str]]:
    """Return supported commands received from the configured Telegram chat."""
    try:
        updates = fetch_telegram_updates(offset)
    except Exception as e:
        bot_log("[TELEGRAM] Komutlar okunamadi:", e)
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

        normalized_text = re.sub(r"\s+", " ", text.casefold()).strip()
        normalized_text = normalized_text.lstrip("/")
        if normalized_text in {"ekran resmi", "ekranresmi", "screenshot"}:
            commands.append("screenshot")
            continue

        command = text.split()[0].lower().split("@", 1)[0].lstrip("/")
        if command in {"dur", "devam", "restart", "logs"}:
            commands.append(command)
            continue

        threshold = parse_threshold_command(text)
        if threshold is not None:
            commands.append(f"threshold:{threshold}")

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
    m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*%|%\s*(-?\d+(?:[.,]\d+)?)", t)
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


def make_key(
    filter_name: str,
    profit: float,
    text: str,
    record_id: str | None = None,
) -> str:
    """
    Aynı event + aynı kâr (2 ondalık) => aynı key (tekrar mesaj yok)
    Event aynı ama kâr değişirse => yeni key (tekrar mesaj var)
    """
    # The site embeds a stable opportunity id in every prong link.
    identity = record_id or normalize_event_identity(text)
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
    """Detect a real signed-in session instead of merely seeing surebet HTML."""
    url = (page.url or "").lower()
    if any(part in url for part in ("/users/sign_in", "login", "signin")):
        return False
    try:
        login_links = page.locator(
            'a[href*="/users/sign_in"], a[href*="/login"], a[href*="/signin"]'
        )
        for index in range(min(login_links.count(), 10)):
            if login_links.nth(index).is_visible():
                return False
        return True
    except Exception:
        return True


def get_reported_result_count(page) -> int | None:
    """Read the site's own '<number> kesin bahis bulundu' counter."""
    try:
        body = page.locator("body").inner_text(timeout=5000)
        match = re.search(r"([\d.\s]+)\s+kesin bahis bulundu", body, re.I)
        if not match:
            return None
        digits = re.sub(r"\D", "", match.group(1))
        return int(digits) if digits else None
    except Exception:
        return None


def get_page_diagnostics(page) -> dict:
    try:
        title = page.title()
    except Exception:
        title = "?"
    return {
        "url": page.url or "?",
        "title": title,
        "logged_in": is_logged_in(page),
        "filter": get_site_filter(page) or "?",
        "reported": get_reported_result_count(page),
        "records": page.locator("table tbody").count(),
        "surebet_records": page.locator("table tbody.surebet_record").count(),
    }


def send_page_screenshot(page, reason: str = "Manuel ekran goruntusu") -> None:
    """Send the viewport exactly as the headless bot currently sees it."""
    shot_path = os.path.join("/tmp", f"page_debug_{int(time.time())}.png")
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(120)
        details = get_page_diagnostics(page)
        page.screenshot(
            path=shot_path,
            full_page=False,
            animations="disabled",
            timeout=10000,
        )
        caption = (
            f"\U0001f4f8 {reason}\n"
            f"Filtre: {details['filter']} | "
            f"Sayac: {details['reported']} | "
            f"Blok: {details['surebet_records']}"
        )
        send_telegram_photo(shot_path, caption=caption)
        bot_log(f"[EKRAN] Gonderildi | {details}")
    finally:
        try:
            if os.path.exists(shot_path):
                os.remove(shot_path)
        except Exception:
            pass


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
        bot_log(f"[FILTRE] secildi -> {selected}")

        return True
    except Exception as e:
        bot_log(f"[FILTRE] Seçilemedi: {filter_name} | Hata: {e}")
        return False


def get_site_filter(page) -> str | None:
    """Sitede şu an seçili olan filtre label'ını döndür."""
    try:
        filter_label = page.locator("text=Filtre").first
        sel = filter_label.locator("xpath=following::select[1]")
        return sel.locator("option:checked").inner_text(timeout=2000).strip()
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


def get_record_id(record) -> str | None:
    """Return the stable surebet id embedded in calculator/prong links."""
    selectors = (
        "form[action*='/calculator/show/']",
        "a[href*='/calculator/show/']",
        "a[href*='/nav/surebet/prong/']",
    )
    for selector in selectors:
        try:
            node = record.locator(selector).first
            if node.count() == 0:
                continue
            attr = "action" if selector.startswith("form") else "href"
            value = (node.get_attribute(attr) or "").strip()
            match = re.search(r"/calculator/show/([^/?#]+)", value)
            if not match:
                match = re.search(r"/nav/surebet/prong/\d+/([^/?#]+)", value)
            if match:
                return match.group(1)
        except Exception:
            continue
    return None


def scrape_rows(page):
    page.wait_for_timeout(1500)

    # Every opportunity is one independent tbody. Do not depend on the
    # optional surebet_record class; the site can omit it after an AJAX update.
    records = page.locator("table tbody")
    results = []

    for i in range(min(records.count(), 500)):
        record = records.nth(i)
        first_row = record.locator("tr").first
        first_text = first_row.inner_text().strip()
        profit = parse_percent(first_text)
        if profit is None:
            continue

        full_text = record.inner_text().strip()
        full_text = re.sub(r"\s+\n", "\n", full_text)
        full_text = re.sub(r"\n{3,}", "\n\n", full_text)

        results.append(
            {
                "profit": profit,
                "text": full_text,
                "record_id": get_record_id(record),
                "calc_url": get_calc_url_from_row(page, first_row) or CALC_LINK,
            }
        )

    return results


def screenshot_surebet_record(
    page,
    record_id: str,
    out_path: str,
    expected_profit: float | None = None,
) -> bool:
    """Capture exactly one live tbody.surebet_record element."""
    max_retry = int(os.getenv("SS_RETRY", "3"))
    settle_ms = int(os.getenv("SS_SETTLE_MS", "80"))
    retry_wait_ms = int(os.getenv("SS_RETRY_WAIT_MS", "120"))

    for attempt in range(1, max_retry + 1):
        try:
            # Re-find by stable id on every attempt. Live table reordering no
            # longer changes which opportunity is captured.
            record = page.locator(
                "table tbody",
                has=page.locator(f'a[href*="/{record_id}/"]'),
            ).first
            if record.count() == 0:
                raise RuntimeError("Opportunity is no longer present.")

            record.scroll_into_view_if_needed(timeout=3000)
            if settle_ms > 0:
                page.wait_for_timeout(settle_ms)

            first_text = record.locator("tr").first.inner_text(timeout=3000)
            if get_record_id(record) != record_id:
                raise RuntimeError("Opportunity changed during capture.")

            if expected_profit is not None:
                current_profit = parse_percent(first_text)
                if (
                    current_profit is None
                    or abs(current_profit - expected_profit) > 0.001
                ):
                    raise RuntimeError("Profit changed during capture.")

            # Element screenshot uses the exact tbody boundary: no next header,
            # no manual padding and no mixed viewport/document coordinates.
            record.screenshot(
                path=out_path,
                animations="disabled",
                timeout=5000,
            )

            # Validate again after capture. If an AJAX refresh happened in the
            # tiny interval between validation and screenshot, discard it.
            final_first_text = record.locator("tr").first.inner_text(timeout=3000)
            final_profit = parse_percent(final_first_text)
            if (
                get_record_id(record) != record_id
                or final_profit is None
                or (
                    expected_profit is not None
                    and abs(final_profit - expected_profit) > 0.001
                )
            ):
                raise RuntimeError("Opportunity changed while screenshotting.")
            return True
        except Exception as exc:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except Exception:
                pass
            bot_log(f"[SS] Attempt {attempt}/{max_retry} failed: {exc}")
            if attempt < max_retry:
                page.wait_for_timeout(retry_wait_ms)

    return False

def main():
    if not os.path.exists(STATE_PATH):
        bot_log(f"❌ {STATE_PATH} bulunamadı. Önce python save_session.py ile login session kaydet.")
        return

    seen = set()  # her açılışta sıfırdan başla

    restart_requested = False
    threshold = load_runtime_threshold(VB_THRESHOLD)
    bot_log(f"[AYAR] Firsat limiti: %{format_threshold(threshold)}")

    with sync_playwright() as p:
        # Railway gibi sunucularda headless şart
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=STATE_PATH)
        page = context.new_page()

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        if not is_logged_in(page):
            try:
                send_page_screenshot(page, "Oturum gecersiz")
            except Exception as e:
                bot_log("[OTURUM] Ekran goruntusu gonderilemedi:", e)
            send_telegram("❌ Oturum geçersiz görünüyor. save_session.py ile yeniden giriş yapıp session kaydetmen lazım.")
            browser.close()
            return

        send_telegram("✅ Fırsat bot başlatıldı")

        # Sadece tek filtre kullanıyoruz
        set_site_filter(page, FILTER_NAME)
        ensure_scanning_on(page)

        command_offset = initialize_telegram_offset()
        paused = False
        bad_empty_scans = 0
        last_empty_recovery = 0.0
        last_empty_alert = 0.0

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
                                send_telegram("\u267b\ufe0f F\u0131rsat bot yeniden ba\u015flat\u0131l\u0131yor")
                            except Exception:
                                pass
                            break
                        if command == "logs":
                            send_telegram("\U0001f4cb Son 10 log:\n" + get_recent_logs(10))
                        elif command == "screenshot":
                            try:
                                send_page_screenshot(page, "Botun gordugu ekran")
                            except Exception as e:
                                bot_log("[EKRAN] Gonderilemedi:", e)
                                send_telegram(
                                    "\u274c Ekran goruntusu alinamadi: " + str(e)[:180]
                                )
                        elif command.startswith("threshold:"):
                            threshold = float(command.split(":", 1)[1])
                            saved = save_runtime_threshold(threshold)
                            bot_log(
                                f"[AYAR] Firsat limiti Telegram'dan "
                                f"%{format_threshold(threshold)} yapildi"
                            )
                            reply = (
                                f"\u2705 F\u0131rsat limiti "
                                f"%{format_threshold(threshold)} olarak ayarland\u0131"
                            )
                            if not saved:
                                reply += "\n\u26a0\ufe0f Restart sonras\u0131 korunamayabilir."
                            send_telegram(reply)
                        elif command == "dur" and not paused:
                            paused = True
                            send_telegram("\u23f8 Bot duraklat\u0131ld\u0131")
                        elif command == "devam" and paused:
                            paused = False
                            send_telegram("\u25b6\ufe0f Bot devam ediyor")

                if restart_requested:
                    break

                if paused:
                    time.sleep(COMMAND_POLL_SEC)
                    continue

                ensure_scanning_on(page)
                if not is_logged_in(page):
                    try:
                        send_page_screenshot(page, "Oturum kapandi")
                    except Exception as e:
                        bot_log("[OTURUM] Ekran goruntusu gonderilemedi:", e)
                    send_telegram("⚠️ Oturum düşmüş görünüyor. save_session.py ile session'ı yenile.")
                    break

                items = scrape_rows(page)
                reported_count = get_reported_result_count(page)
                bad_empty = not items and (
                    reported_count is None or reported_count > 0
                )

                if bad_empty:
                    bad_empty_scans += 1
                else:
                    bad_empty_scans = 0

                if bad_empty_scans >= 3:
                    now = time.monotonic()
                    details = get_page_diagnostics(page)
                    bot_log(f"[SAYFA] Firsatlar okunamiyor | {details}")

                    # Send at most one automatic diagnostic image per 10 min.
                    if now - last_empty_alert >= 600:
                        try:
                            send_page_screenshot(
                                page,
                                "Sayfa dolu gorunuyor fakat firsatlar okunamadi",
                            )
                        except Exception as e:
                            bot_log("[SAYFA] Teshis ekrani gonderilemedi:", e)
                        last_empty_alert = now

                    # Refresh at most once per minute, then restore the filter.
                    if now - last_empty_recovery >= 60:
                        bot_log("[SAYFA] Yenileniyor ve filtre tekrar seciliyor...")
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        set_site_filter(page, FILTER_NAME)
                        ensure_scanning_on(page)
                        last_empty_recovery = now
                    bad_empty_scans = 0
                    time.sleep(INTERVAL_SEC)
                    continue

                max_p = max([x["profit"] for x in items], default=0)
                bot_log(f"Gorulen satir: {len(items)} | Max kar: {max_p} | Filtre: {FILTER_NAME}")

                thr = threshold
                hits = [x for x in items if x["profit"] >= thr]
                bot_log(f"Eşik üstü: {len(hits)} (Filtre: {FILTER_NAME} | thr={thr})")

                hits.sort(key=lambda x: x["profit"], reverse=True)

                for h in hits:
                    record_id = h.get("record_id")
                    if not record_id:
                        bot_log("[SS] Firsat kimligi bulunamadi; sonraki turda tekrar denenecek.")
                        continue

                    key = make_key(
                        FILTER_NAME,
                        h["profit"],
                        h["text"],
                        record_id=record_id,
                    )
                    if key in seen:
                        continue

                    # İstenen değişiklik: metin yerine, o fırsatın bulunduğu satırların ekran görüntüsünü gönder
                    ts = int(time.time())
                    shot_path = os.path.join("/tmp", f"shot_{h['profit']:.2f}_{ts}.png")
                    ok = screenshot_surebet_record(
                        page,
                        record_id,
                        shot_path,
                        expected_profit=h["profit"],
                    )

                    # ✅ Burada artık her fırsat kendi calc_url'sini kullanıyor
                    calc = h.get("calc_url") or CALC_LINK
                    caption = f"🚨 Kar: %{h['profit']:.2f}"

                    if ok:
                        try:
                            send_telegram_photo(shot_path, caption=caption)
                            # Mark seen only after Telegram confirms the photo.
                            seen.add(key)
                        finally:
                            try:
                                os.remove(shot_path)
                            except Exception:
                                pass
                    else:
                        # Never send a caption-only alert. A live opportunity
                        # remains unseen and will be retried on the next scan.
                        bot_log(
                            f"[SS] %{h['profit']:.2f} ekran goruntusu alinamadi; "
                            "sonraki turda tekrar denenecek."
                        )

                save_seen(seen)

            except PWTimeout:
                bot_log("Timeout oldu, tekrar denenecek...")
            except Exception as e:
                bot_log("ERR:", e)

            time.sleep(INTERVAL_SEC)

        browser.close()

    if restart_requested:
        bot_log("[RESTART] Bot yeniden başlatılıyor...")
        return True

    return False


if __name__ == "__main__":
    while main():
        time.sleep(1)