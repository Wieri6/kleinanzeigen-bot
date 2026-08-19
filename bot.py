"""Checks a Kleinanzeigen search for new listings and notifies via Telegram.
Intended to be run every few minutes by Windows Task Scheduler (one run = one check)."""

import html
import json
import logging
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SECRETS_PATH = BASE_DIR / "secrets.json"
SEEN_PATH = BASE_DIR / "seen.json"
LOG_PATH = BASE_DIR / "bot.log"

GESUCH_TITLE_PATTERN = re.compile(r"^\s*(ich\s+)?suche\b", re.IGNORECASE)
ROOM_COUNT_PATTERN = re.compile(r"(\d+(?:,\d+)?)\s*Zi\.", re.IGNORECASE)
MAX_ROOMS = 2
MONTH_NAMES = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}
_MONTH_NAME_ALTERNATION = "|".join(MONTH_NAMES.keys())

AVAILABILITY_DATE_PATTERN = re.compile(
    r"ab\s*:?\s*(?:dem\s+)?(\d{1,2})\.\s*(\d{1,2})\.?\s*(\d{2,4})?"
    rf"|ab\s*:?\s*(?:dem\s+)?(?:\d{{1,2}}\.?\s*)?({_MONTH_NAME_ALTERNATION})\s*(\d{{4}})?",
    re.IGNORECASE,
)


def parse_availability_dates(text, today):
    """Best-effort extraction of every 'available from' date mentioned in text."""
    dates = []
    for m in AVAILABILITY_DATE_PATTERN.finditer(text):
        try:
            if m.group(1) and m.group(2):
                day, month = int(m.group(1)), int(m.group(2))
                year_str = m.group(3)
            else:
                day, month = 1, MONTH_NAMES[m.group(4).lower()]
                year_str = m.group(5)
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            if year_str:
                year = int(year_str)
                if year < 100:
                    year += 2000
            else:
                # No year given - assume this year, unless that month has
                # already passed, in which case it must mean next year.
                year = today.year if month >= today.month else today.year + 1
            day = min(day, 28) if month == 2 else day
            dates.append(date(year, month, day))
        except (ValueError, KeyError):
            continue
    return dates


def available_too_late(*texts, today=None, cutoff_month=11):
    """True if the listing's earliest stated 'available from' date is on/after
    November 1st (or later) - i.e. later than the desired move-in window."""
    today = today or date.today()
    cutoff = date(today.year if today.month < cutoff_month else today.year + 1, cutoff_month, 1)
    combined = " ".join(t for t in texts if t)
    found_dates = parse_availability_dates(combined, today)
    return any(d >= cutoff for d in found_dates)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)


def _clean_env(name):
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lstrip("﻿")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Local-only secrets file (never committed to git)
    if SECRETS_PATH.exists():
        with open(SECRETS_PATH, "r", encoding="utf-8-sig") as f:
            config.update(json.load(f))

    # Environment variables take precedence (used in GitHub Actions secrets).
    # Values are cleaned of stray BOM/whitespace characters that some shells
    # (e.g. PowerShell piping to a native process) can prepend.
    config["telegram_token"] = _clean_env("TELEGRAM_TOKEN") or config.get("telegram_token")
    config["telegram_chat_id"] = _clean_env("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id")
    config["anthropic_api_key"] = _clean_env("ANTHROPIC_API_KEY") or config.get("anthropic_api_key")
    profile_json = _clean_env("PROFILE_JSON")
    if profile_json:
        config["profile"] = json.loads(profile_json)
    return config


def load_seen():
    if SEEN_PATH.exists():
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_ids), f)


def search_url_sorted_by_date(url):
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}sortierung=datum"


def fetch_listings(search_url):
    url = search_url_sorted_by_date(search_url)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    listings = []
    for article in soup.select("article.aditem"):
        ad_id = article.get("data-adid")
        href = article.get("data-href")
        if not ad_id or not href:
            continue

        title_tag = article.select_one("h2 a")
        title = title_tag.get_text(strip=True) if title_tag else "(ohne Titel)"

        price_tag = article.select_one(".aditem-main--middle--price-shipping--price")
        price = price_tag.get_text(strip=True) if price_tag else "?"

        location_tag = article.select_one(".aditem-main--top--left")
        location = location_tag.get_text(strip=True) if location_tag else "?"

        if GESUCH_TITLE_PATTERN.match(title):
            continue

        tags_tag = article.select_one(".aditem-main--middle--tags")
        if tags_tag:
            room_match = ROOM_COUNT_PATTERN.search(tags_tag.get_text())
            if room_match and float(room_match.group(1).replace(",", ".")) > MAX_ROOMS:
                continue

        listings.append(
            {
                "id": ad_id,
                "title": title,
                "price": price,
                "location": location,
                "url": "https://www.kleinanzeigen.de" + href,
            }
        )
    return listings


def fetch_ad_details(detail_url):
    resp = requests.get(detail_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    desc_tag = soup.select_one("#viewad-description-text")
    description = desc_tag.get_text("\n", strip=True) if desc_tag else ""

    name_tag = soup.select_one(".userprofile-vip")
    seller_name = name_tag.get_text(strip=True) if name_tag else ""
    is_commercial = "Gewerblicher Nutzer" in soup.select_one("#viewad-contact").get_text() if soup.select_one("#viewad-contact") else False

    return {
        "description": description,
        "seller_name": seller_name,
        "is_commercial": is_commercial,
    }


# Generic fallback only (no personal data) — the real example lives in the
# private profile secret ("example_message"), never committed to this public repo.
DEFAULT_EXAMPLE_MESSAGE = """Hallo,
ich interessiere mich sehr für Ihre Wohnung und würde diese gerne besichtigen. Kurz zu mir: [kurze Selbstvorstellung].
Ich kann bei Bedarf gerne Bewerbungsunterlagen vorab zusenden.
Über einen Besichtigungstermin würde ich mich sehr freuen. Wann würde es Ihnen passen?
Viele Grüße
[Name]"""


def generate_message_draft(listing, description, seller_name, is_commercial, profile, api_key):
    example_message = profile.get("example_message", DEFAULT_EXAMPLE_MESSAGE)
    profile_lines = [
        f"Name (Absender): {profile.get('name', '')}",
        f"Status: {profile.get('status', 'keine Angabe')}",
        f"Haustiere: {'ja' if profile.get('has_pets') else 'nein'}",
        f"Zieht allein ein: {'ja' if profile.get('moving_alone') else 'nein (mit Partner/WG)'}",
        f"Einzugstermin: {profile.get('move_in', 'keine Angabe')}",
    ]
    if profile.get("notes"):
        profile_lines.append(f"Sonstiges: {profile['notes']}")

    user_prompt = (
        "Formuliere eine Kontaktanfrage auf Deutsch fuer eine Wohnungsanzeige auf "
        "Kleinanzeigen, im Stil des folgenden Beispiels (Ton, Laenge, Inhalte wie "
        "Selbstvorstellung, Angebot der Unterlagen, Buergschaft der Eltern):\n\n"
        f"--- BEISPIEL ---\n{example_message}\n--- ENDE BEISPIEL ---\n\n"
        f"Anzeigentitel: {listing['title']}\n"
        f"Anzeigenbeschreibung:\n{description[:3000] or '(keine Beschreibung vorhanden)'}\n\n"
        f"Name/Account des Inserenten auf Kleinanzeigen: "
        f"{seller_name or '(kein Name angegeben)'}"
        f"{' (als Gewerblicher Nutzer/Firma markiert)' if is_commercial else ''}\n\n"
        f"Fakten zur anfragenden Person (nur diese verwenden, nichts erfinden):\n"
        + "\n".join(profile_lines)
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 500,
            "system": (
                "Du hilfst bei Wohnungssuche in Deutschland und schreibst Kontaktanfragen "
                "im Stil des vom Nutzer vorgegebenen Beispiels: locker-natuerlich und "
                "persoenlich, wie ein echter Mensch schreiben wuerde, nicht wie eine "
                "ausgefuellte Vorlage. Variiere Satzbau und Formulierungen, vermeide "
                "steife Behoerdensprache. Persoenlich, direkt, mit "
                "kurzer Selbstvorstellung (Alter, Studium/Beruf, Einzugstermin), Angebot "
                "der Bewerbungsunterlagen und ggf. Elternbuergschaft, Abschluss mit Bitte "
                "um Besichtigungstermin und Unterschrift mit dem angegebenen Namen.\n\n"
                "Anrede je nach Name/Account des Inserenten waehlen:\n"
                "- Wirkt der Name wie ein normaler VOLLER Vor- und Nachname einer "
                "Privatperson (z.B. 'Gerd Franz' oder 'Anna Vasarhelyi') -> foermliche "
                "Anrede 'Sehr geehrte Frau [Nachname],' bzw. 'Sehr geehrter Herr "
                "[Nachname],'. Bestimme das Geschlecht anhand des Vornamens (bei "
                "eindeutig weiblichen Vornamen 'Frau', bei eindeutig maennlichen "
                "'Herr'). Ist bei einem ungewoehnlichen/nicht eindeutigen Vornamen das "
                "Geschlecht nicht sicher bestimmbar, nutze stattdessen 'Hallo [Vorname "
                "Nachname],' als neutrale Alternative.\n"
                "- Ist es klar eine Firma/Immobilienfirma (Firmenname oder als "
                "Gewerblicher Nutzer markiert) -> z.B. 'Hallo liebes Team von [Firma],' "
                "oder 'Sehr geehrtes Team von [Firma],'. Ist der volle Firmenname lang "
                "oder foermlich (enthaelt z.B. GmbH, Verwaltungs, u. Co, mbH & Co KG, "
                "AG), nutze in der Anrede NUR den kurzen, bekannten Markennamen/Kernnamen "
                "(z.B. 'ACTIVA' statt 'ACTIVA Wohnanlagen- u. Grundstuecksverwaltungs "
                "GmbH') - das wirkt natuerlicher und weniger steif.\n"
                "- Ist der Name/Account kein normaler Personenname (Fantasiename, "
                "Nutzername, anonym, kein Name angegeben) -> neutrale Anrede wie "
                "'Hallo,' oder 'Sehr geehrte Damen und Herren,' ohne den Namen zu nennen\n\n"
                "WICHTIG: Die Person studiert an der UNIVERSITAET Erfurt, NICHT an der "
                "Fachhochschule (FH) Erfurt. Erwaehne niemals 'Fachhochschule' oder 'FH' "
                "in Bezug auf die Person, auch wenn die Anzeige FH/Uni-Naehe bewirbt - "
                "dann nur 'Uni-Naehe' bzw. 'Naehe zur Universitaet' aufgreifen, falls "
                "ueberhaupt.\n\n"
                "Baue zusaetzlich 1 konkretes Detail aus der Anzeigenbeschreibung ein "
                "(z.B. Lage, Ausstattung, Zustand), sofern vorhanden. Nutze "
                "ausschliesslich die angegebenen Fakten zur Person, erfinde nichts hinzu. "
                "Wenn ein Wunsch aus 'Sonstiges' (z.B. Balkon) in der Anzeige NICHT "
                "vorkommt, erwaehne ihn gar nicht - weder positiv noch als "
                "Fehlen/Vermissen. Verwende KEINE Gedankenstriche im Text (weder – "
                "noch —); nutze stattdessen Kommas, Punkte oder 'bzw.'. Gib nur den "
                "Nachrichtentext aus, ohne Erklaerung oder Meta-Kommentare."
            ),
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"].strip()


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    resp.raise_for_status()


def format_message(listing):
    return (
        f"🏠 <b>{listing['title']}</b>\n"
        f"💶 {listing['price']} · 📍 {listing['location']}\n"
        f"{listing['url']}"
    )


def running_in_github_actions():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def git_pull_latest_seen():
    try:
        subprocess.run(
            ["git", "pull", "--quiet"],
            cwd=BASE_DIR, check=True, capture_output=True, timeout=30,
        )
    except Exception:
        logging.exception("Konnte seen.json nicht von GitHub abgleichen (lokaler Stand wird verwendet)")


def git_push_seen(had_new_listings):
    try:
        subprocess.run(["git", "add", "seen.json"], cwd=BASE_DIR, check=True, capture_output=True, timeout=15)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR, capture_output=True, timeout=15,
        )
        if diff.returncode == 0:
            return  # nothing changed, nothing to push
        message = "Update seen listings (local)" if had_new_listings else "Sync seen listings (local)"
        subprocess.run(["git", "commit", "-m", message, "--quiet"], cwd=BASE_DIR, check=True, capture_output=True, timeout=15)
        try:
            subprocess.run(["git", "push", "--quiet"], cwd=BASE_DIR, check=True, capture_output=True, timeout=30)
        except subprocess.CalledProcessError:
            # Remote moved on (e.g. GitHub Actions pushed in the meantime) - rebase and retry once.
            subprocess.run(["git", "pull", "--rebase", "--quiet"], cwd=BASE_DIR, check=True, capture_output=True, timeout=30)
            subprocess.run(["git", "push", "--quiet"], cwd=BASE_DIR, check=True, capture_output=True, timeout=30)
    except Exception:
        logging.exception("Konnte seen.json nicht zu GitHub pushen (Cloud-Task uebernimmt notfalls)")


def main():
    config = load_config()
    if not running_in_github_actions():
        git_pull_latest_seen()
    seen = load_seen()
    first_run = len(seen) == 0

    try:
        listings = fetch_listings(config["search_url"])
    except Exception:
        logging.exception("Fehler beim Abrufen der Suchergebnisse")
        return

    new_listings = [l for l in listings if l["id"] not in seen]
    notified_ids = set()

    if first_run:
        logging.info(
            "Erster Lauf: %d bestehende Anzeigen als bekannt markiert, keine Benachrichtigung.",
            len(listings),
        )
        notified_ids.update(l["id"] for l in listings)
    else:
        for listing in new_listings:
            ad_details = None
            try:
                ad_details = fetch_ad_details(listing["url"])
                if available_too_late(listing["title"], ad_details["description"]):
                    logging.info(
                        "Uebersprungen (erst ab November oder spaeter verfuegbar): %s (%s)",
                        listing["title"], listing["id"],
                    )
                    notified_ids.add(listing["id"])
                    continue
            except Exception:
                logging.exception(
                    "Konnte Verfuegbarkeit nicht pruefen fuer %s - benachrichtige trotzdem",
                    listing["id"],
                )

            try:
                send_telegram_message(
                    config["telegram_token"],
                    config["telegram_chat_id"],
                    format_message(listing),
                )
                logging.info("Benachrichtigt: %s (%s)", listing["title"], listing["id"])
                notified_ids.add(listing["id"])
            except Exception:
                logging.exception(
                    "Fehler beim Senden der Telegram-Nachricht fuer %s - wird beim naechsten Lauf erneut versucht",
                    listing["id"],
                )
                continue

            api_key = config.get("anthropic_api_key")
            if not api_key:
                continue
            try:
                if ad_details is None:
                    ad_details = fetch_ad_details(listing["url"])
                draft = generate_message_draft(
                    listing,
                    ad_details["description"],
                    ad_details["seller_name"],
                    ad_details["is_commercial"],
                    config.get("profile", {}),
                    api_key,
                )
                send_telegram_message(
                    config["telegram_token"],
                    config["telegram_chat_id"],
                    html.escape(draft),
                )
                logging.info("Entwurf gesendet fuer %s", listing["id"])
            except Exception:
                logging.exception(
                    "Fehler beim Erstellen/Senden des Nachrichtenentwurfs fuer %s",
                    listing["id"],
                )

    seen.update(notified_ids)
    save_seen(seen)
    if not running_in_github_actions():
        git_push_seen(had_new_listings=bool(notified_ids) and not first_run)


if __name__ == "__main__":
    main()
