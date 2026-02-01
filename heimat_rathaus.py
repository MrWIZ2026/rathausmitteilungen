import os
import json
import html
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE = "https://www.heimat-info.de"
LIST_URL = "https://www.heimat-info.de/gemeinden/witzenhausen?tab=City_Hall&categoryid=761f0ac3-3372-479d-8f4b-f3b076f0851a&page={page}"

STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()
MAX_PAGES = int(os.getenv("MAX_PAGES", "2"))
MAX_SEEN = int(os.getenv("MAX_SEEN", "500"))

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

EXISTING_POST = os.getenv("EXISTING_POST", "0").strip() == "1"


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"seen": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_list_page(session: requests.Session, page: int) -> list[dict]:
    url = LIST_URL.format(page=page)
    r = session.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; github-actions-bot/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
        },
        timeout=30,
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    items: list[dict] = []
    used: set[str] = set()

    # Beiträge sind Links wie /beitraege/<uuid>
    for a in soup.select('a[href^="/beitraege/"]'):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        title = a.get_text(" ", strip=True)
        if not title:
            continue

        # zweiten Link "mehr anzeigen" ignorieren
        if title.lower() == "mehr anzeigen":
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

    resp = session.post(api, json=payload, timeout=30)
    resp.raise_for_status()

    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Telegram Antwort ist kein JSON: {resp.text}") from e

    if not data.get("ok", False):
        raise RuntimeError(f"Telegram ok=false: {data}")


def chunk_blocks(blocks: list[str], max_len: int = 3500) -> list[str]:
    chunks: list[str] = []
    buf = ""
    for b in blocks:
        candidate = (buf + "\n\n" + b).strip() if buf else b
        if len(candidate) > max_len and buf:
            chunks.append(buf)
            buf = b
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


def main() -> None:
    state = load_state()
    seen_list: list[str] = state.get("seen", [])
    seen_set = set(seen_list)

    s = requests.Session()

    all_items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        items = fetch_list_page(s, page)
        if not items:
            break
        all_items.extend(items)

    print(f"Gefunden insgesamt: {len(all_items)} Eintraege")

    # Testmodus: poste den neuesten Eintrag, auch wenn schon gesehen
    if EXISTING_POST:
        if all_items:
            it = all_items[0]
            print("EXISTING_POST aktiv, sende Testbeitrag:", it["url"])
            tg_send(s, format_block(it["title"], it["url"]))
        else:
            print("Keine Eintraege gefunden, Testpost nicht moeglich.")
        return

    # Bootstrap: beim ersten Run nur merken, nicht posten
    if not seen_list:
        for it in all_items:
            if it["url"] not in seen_set:
                seen_set.add(it["url"])
                seen_list.append(it["url"])
        state["seen"] = seen_list[-MAX_SEEN:]
        save_state(state)
        print("Bootstrap run, keine Nachrichten gesendet.")
        return

    new_items = [it for it in all_items if it["url"] not in seen_set]
    print(f"Neue Eintraege: {len(new_items)}")

    # alte zuerst posten
    new_blocks = [format_block(it["title"], it["url"]) for it in reversed(new_items)]

    if new_blocks:
        for msg in chunk_blocks(new_blocks):
            tg_send(s, msg)
            time.sleep(0.8)

    # State updaten
    for it in all_items:
        if it["url"] not in seen_set:
            seen_set.add(it["url"])
            seen_list.append(it["url"])

    state["seen"] = seen_list[-MAX_SEEN:]
    save_state(state)
    print("State gespeichert:", STATE_FILE)


if __name__ == "__main__":
    main()
