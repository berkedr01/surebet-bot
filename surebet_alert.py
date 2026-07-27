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


def screenshot_rows_group(
    page,
    start_i: int,
    end_i: int,
    out_path: str,
    expected_profit: float | None = None,
) -> bool:
    """Capture one row group without scrolling again while measuring it."""
    rows = page.locator("table tbody tr")

    pad_x = int(os.getenv("SS_PAD_X", "6"))
    pad_y = int(os.getenv("SS_PAD_Y", "6"))
    max_retry = int(os.getenv("SS_RETRY", "2"))
    settle_ms = int(os.getenv("SS_SETTLE_MS", "60"))
    retry_wait_ms = int(os.getenv("SS_RETRY_WAIT_MS", "100"))

    for attempt in range(1, max_retry + 1):
        try:
            row_count = rows.count()
            if start_i < 0 or start_i >= row_count or end_i <= start_i:
                return False

            # Scroll only once. Scrolling for every row mixed bounding boxes
            # measured at different viewport positions.
            rows.nth(start_i).evaluate(
                "el => el.scrollIntoView({block: 'start', inline: 'nearest'})",
                timeout=3000,
            )
            if settle_ms > 0:
                page.wait_for_timeout(settle_ms)

            # Measure every target row atomically at the same scroll position,
            # then convert viewport coordinates to document coordinates.
            measured = page.evaluate(
                """
                ({ start, end, padX, padY }) => {
                    const allRows = Array.from(
                        document.querySelectorAll("table tbody tr")
                    );
                    if (start < 0 || start >= allRows.length || end <= start) {
                        return null;
                    }

                    const targets = allRows.slice(start, Math.min(end, allRows.length));
                    const rects = [];

                    for (const row of targets) {
                        const style = window.getComputedStyle(row);
                        const rect = row.getBoundingClientRect();
                        if (
                            style.display === "none" ||
                            style.visibility === "hidden" ||
                            rect.width <= 0 ||
                            rect.height <= 0
                        ) {
                            continue;
                        }
                        rects.push({
                            left: rect.left,
                            top: rect.top,
                            right: rect.right,
                            bottom: rect.bottom,
                        });
                    }

                    if (!rects.length) {
                        return null;
                    }

                    const minLeft = Math.min(...rects.map(r => r.left));
                    const minTop = Math.min(...rects.map(r => r.top));
                    const maxRight = Math.max(...rects.map(r => r.right));
                    const maxBottom = Math.max(...rects.map(r => r.bottom));

                    const x = Math.max(0, minLeft - padX);
                    const y = Math.max(0, minTop - padY);
                    const right = Math.min(window.innerWidth, maxRight + padX);
                    const bottom = Math.min(window.innerHeight, maxBottom + padY);
                    const fullyVisible =
                        minLeft >= 0 &&
                        minTop >= 0 &&
                        maxRight <= window.innerWidth &&
                        maxBottom <= window.innerHeight;

                    return {
                        clip: {
                            x,
                            y,
                            width: Math.max(2, right - x),
                            height: Math.max(2, bottom - y),
                        },
                        fullyVisible,
                        firstText: targets[0] ? targets[0].innerText : "",
                    };
                }
                """,
                {
                    "start": start_i,
                    "end": end_i,
                    "padX": pad_x,
                    "padY": pad_y,
                },
            )

            if not measured:
                raise RuntimeError("Screenshot rows could not be measured.")
            if not measured.get("fullyVisible", False):
                raise RuntimeError("Screenshot row group does not fit in the viewport.")

            # If the live table changed just before capture, do not send a
            # screenshot belonging to a different opportunity.
            if expected_profit is not None:
                current_profit = parse_percent(measured.get("firstText", ""))
                if current_profit is None or abs(current_profit - expected_profit) > 0.001:
                    return False

            page.screenshot(path=out_path, clip=measured["clip"])
            return True
        except Exception as e:
            bot_log(f"[SS] Attempt {attempt}/{max_retry} failed: {e}")
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
                                send_telegram("\u267b\ufe0f F\u0131rsat bot yeniden ba\u015flat\u0131l\u0131yor")
                            except Exception:
                                pass
                            break
                        if command == "logs":
                            send_telegram("\U0001f4cb Son 10 log:\n" + get_recent_logs(10))
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
                    send_telegram("⚠️ Oturum düşmüş görünüyor. save_session.py ile session'ı yenile.")
                    break

                items = scrape_rows(page)

                max_p = max([x["profit"] for x in items], default=0)
                bot_log(f"Gorulen satir: {len(items)} | Max kar: {max_p} | Filtre: {FILTER_NAME}")

                thr = threshold
                hits = [x for x in items if x["profit"] >= thr]
                bot_log(f"Eşik üstü: {len(hits)} (Filtre: {FILTER_NAME} | thr={thr})")

                hits.sort(key=lambda x: x["profit"], reverse=True)

                for h in hits:
                    key = make_key(FILTER_NAME, h["profit"], h["text"])
                    if key in seen:
                        continue
                    seen.add(key)

                    # İstenen değişiklik: metin yerine, o fırsatın bulunduğu satırların ekran görüntüsünü gönder
                    ts = int(time.time())
                    shot_path = os.path.join("/tmp", f"shot_{h['profit']:.2f}_{ts}.png")
                    ok = screenshot_rows_group(
                        page,
                        h["start_i"],
                        h["end_i"],
                        shot_path,
                        expected_profit=h["profit"],
                    )

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