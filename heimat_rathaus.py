import os
import json
import html
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE = "https://www.heimat-info.de"
LIST_URL = (
    "https://www.heimat-info.de/gemeinden/witzenhausen"
    "?tab=City_Hall&categoryid=761f0ac3-3372-479d-8f4b-f3b076f0851a&page={page}"
)

STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()

MAX_PAGES = int(os.getenv("MAX_PAGES", "2"))
MAX_SEEN = int(os.getenv("MAX_SEEN", "500"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "50"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
RATE_LIMIT_SLEEP = float(os.getenv("RATE_LIMIT_SLEEP", "0.8"))

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

EXISTING_POST = os.getenv("EXISTING_POST", "0").strip() == "1"
DEBUG = os.getenv("DEBUG", "0").strip() == "1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_ws(s: str) -> str:
    return " ".join((s or "").split()).strip()


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"seen": [], "created_at": now_iso()}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "seen" not in data or not isinstance(data["seen"], list):
        data["seen"] = []
    return data


def save_state(state: dict) -> None:
    state["updated_at"] = now_iso()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; github-actions-bot/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
        }
    )
    return s


def fetch_list_page(session: requests.Session, page: int) -> list[dict]:
    url = LIST_URL.format(page=page)
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items: list[dict] = []
    used: set[str] = set()

    for a in soup.select('a[href^="/beitraege/"]'):
        href = normalize_ws(a.get("href") or "")
        if not href:
            continue

        title = normalize_ws(a.get_text(" ", strip=True))
        if not title:
            continue

        low = title.lower()
        if low == "mehr anzeigen":
            continue

        full_url = urljoin(BASE, href)

        if full_url in used:
            continue

        used.add(full_url)
        items.append({"title": title, "url": full_url})

    return items


def format_block(title: str, url: str) -> str:
    safe_title = html.escape(title, quote=False)
    safe_url = html.escape(url, quote=True)
    return f"<b>{safe_title}</b>\n<a href=\"{safe_url}\">Infos</a>"


def tg_send(session: requests.Session, text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("TG_TOKEN oder TG_CHAT_ID fehlen als Env Vars.")

    api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    resp = session.post(api, json=payload, timeout=REQUEST_TIMEOUT)

    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Telegram Antwort ist kein JSON: {resp.text}") from e

    if resp.status_code >= 400:
        raise RuntimeError(f"Telegram HTTP Fehler {resp.status_code}: {data}")

    if not data.get("ok", False):
        raise RuntimeError(f"Telegram ok=false: {data}")

    if DEBUG:
        msg_id = (data.get("result") or {}).get("message_id")
        print(f"Telegram gesendet, message_id={msg_id}")


def main() -> None:
    state = load_state()
    seen_list: list[str] = state.get("seen", [])
    seen_set = set(seen_list)

    s = make_session()

    all_items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        items = fetch_list_page(s, page)
        if DEBUG:
            print(f"Page {page} gefunden: {len(items)}")
        if not items:
            break
        all_items.extend(items)

    print(f"Gefunden insgesamt: {len(all_items)} Eintraege")
    if DEBUG and all_items:
        print("Erster Eintrag:", all_items[0]["url"])

    if not all_items:
        state["seen"] = seen_list[-MAX_SEEN:]
        save_state(state)
        print("Keine Eintraege gefunden, State gespeichert.")
        return

        # Testmodus: poste alle Eintraege, auch wenn schon gesehen
    if EXISTING_POST:
        print("EXISTING_POST aktiv, sende alle aktuell gefundenen Eintraege")

        posted = 0
        for it in reversed(all_items):
            tg_send(s, format_block(it["title"], it["url"]))
            posted += 1
            time.sleep(RATE_LIMIT_SLEEP)

        print(f"Gepostet im EXISTING_POST Modus: {posted}")

        for it2 in all_items:
            if it2["url"] not in seen_set:
                seen_set.add(it2["url"])
                seen_list.append(it2["url"])

        state["seen"] = seen_list[-MAX_SEEN:]
        save_state(state)
        print("State gespeichert trotz EXISTING_POST:", STATE_FILE)
        return

    if not seen_list:
        for it in all_items:
            if it["url"] not in seen_set:
                seen_set.add(it["url"])
                seen_list.append(it["url"])
        state["seen"] = seen_list[-MAX_SEEN:]
        save_state(state)
        print("Bootstrap run, keine Nachrichten gesendet, State gespeichert.")
        return

    new_items = [it for it in all_items if it["url"] not in seen_set]
    print(f"Neue Eintraege: {len(new_items)}")

    to_post = list(reversed(new_items))[:MAX_POSTS_PER_RUN]

    posted = 0
    for it in to_post:
        tg_send(s, format_block(it["title"], it["url"]))
        posted += 1

        seen_set.add(it["url"])
        seen_list.append(it["url"])

        time.sleep(RATE_LIMIT_SLEEP)

    print(f"Gepostet: {posted}")

    for it in all_items:
        if it["url"] not in seen_set:
            seen_set.add(it["url"])
            seen_list.append(it["url"])

    state["seen"] = seen_list[-MAX_SEEN:]
    save_state(state)
    print("State gespeichert:", STATE_FILE)


if __name__ == "__main__":
    main()
