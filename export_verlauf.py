#!/usr/bin/env python3
"""
AniWorld Verlaufsexport
=======================
Loggt sich auf aniworld.to ein und exportiert den Verlauf von
https://aniworld.to/account/watched/1 .. n nach CSV + JSON.

Aufruf:
    python export_verlauf.py                # voller Export (CSV + JSON)
    python export_verlauf.py --inspect      # nur Seite 1 als HTML speichern (Struktur-Analyse)
    python export_verlauf.py --max-pages 3  # nur die ersten 3 Seiten crawlen
    python export_verlauf.py --delay 1.5    # Pause zwischen den Anfragen (Sekunden)
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import dotenv_values, set_key

BASE = "https://aniworld.to"
WATCHED_URL = f"{BASE}/account/watched/{{page}}"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DEBUG_DIR = OUTPUT_DIR / "debug"
ENV_PFAD = Path(__file__).resolve().parent / ".env"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


@dataclass
class Eintrag:
    serie: str              # z.B. "Witch Hat Atelier"
    staffel: int | None     # z.B. 1
    episode_nr: int | None  # z.B. 6
    titel: str              # Episoden-Titel (DE, leer falls nicht vorhanden)
    titel_original: str     # Originaltitel (meist Englisch)
    datum: str              # "29.09.2026 00:24:57" (wie auf der Seite)
    datum_iso: str          # "2026-09-29T00:24:57" (aus data-livestamp)
    url: str                # Vollständige URL der Episode
    seite: int              # Watched-Seite, auf der der Eintrag stand


# --------------------------------------------------------------------------
# HTTP-Grundgerüst
# --------------------------------------------------------------------------

def make_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=20.0)


def get(client: httpx.Client, url: str, tries: int = 3) -> httpx.Response:
    """GET mit sichtbaren Wiederholungen (DDoS-Guard blockt ab und zu)."""
    last_exc: Exception | None = None
    for attempt in range(tries):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            # Leerer Body kommt bei AniWorld vor (z.B. ohne Login) -> einmal retry
            if resp.text.strip() == "" and attempt < tries - 1:
                print(f"  Leere Antwort bei {url} – Wiederholung …")
                time.sleep(1.5 * (attempt + 1))
                continue
            return resp
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < tries - 1:
                print(f"  Fehler bei {url} ({type(exc).__name__}) – Wiederholung …")
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} fehlgeschlagen: {last_exc}")


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

class CaptchaFehler(RuntimeError):
    """Der Login erfordert einen Mensch-Nachweis (Captcha)."""


def login_fehler(html: str) -> str | None:
    """Extrahiert die Fehlermeldung aus einer Login-Antwort; None wenn kein Fehler."""
    if 'name="password"' not in html and "messageAlert" not in html:
        return None
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(".messageAlert strong") or soup.select_one(".messageAlert")
    return node.get_text(" ", strip=True) if node else ""


def login(client: httpx.Client, email: str, password: str) -> None:
    # 1. Login-Seite holen -> Session-Cookies setzen
    get(client, f"{BASE}/login")

    # 2. Formular absenden (entspricht exakt den Feldern der HTML-Seite)
    print("Melde an …")
    resp = client.post(
        f"{BASE}/login",
        data={"email": email, "password": password, "autoLogin": "on"},
    )
    resp.raise_for_status()

    fehler = login_fehler(resp.text)
    if fehler is not None:
        if "captcha" in resp.text.lower() or "captcha" in fehler.lower():
            raise CaptchaFehler(fehler or "Captcha erforderlich")
        raise RuntimeError(fehler or "keine Fehlermeldung vom Server gefunden")

    # 3. Erfolg verifizieren: Verlaufsseite muss eingeloggt ausliefern
    probe = get(client, WATCHED_URL.format(page=1))
    if "userSessionStatus = true" not in probe.text:
        raise RuntimeError(
            "Login nicht bestätigt (userSessionStatus != true). "
            "Wahrscheinlich falsche Zugangsdaten oder die Seite hat "
            "ungewöhnlich reagiert."
        )
    if probe.text.strip() == "":
        raise RuntimeError("Verlaufsseite ist leer - trotz vermutetem Login.")
    print("Login erfolgreich.")


# --------------------------------------------------------------------------
# Paginierung
# --------------------------------------------------------------------------

def max_page_from_html(html: str) -> int | None:
    """Höchste Seitenzahl aus Pagination-Links / Texten ermitteln."""
    kandidaten = [int(m) for m in re.findall(r"/account/watched/(\d+)", html)]
    kandidaten += [int(m) for m in re.findall(r"[?&]page=(\d+)", html)]
    kandidaten += [int(m) for m in re.findall(r"von\s+(\d+)", html, re.I)]
    return max(kandidaten) if kandidaten else None


# --------------------------------------------------------------------------
# Parsing (Heurrik - wird nach --inspect an die echte Struktur angepasst)
# --------------------------------------------------------------------------

def parse_page(html: str, seite: int) -> list[Eintrag]:
    """Parst die Tabelle <table class="table"> der Verlaufsseite.

    Zeilenstruktur:
        <tr>
          <td><a href="/anime/stream/<slug>/staffel-1/episode-6">Witch Hat Atelier - S1 E6</a></td>
          <td><a href="..."><strong>Deutscher Titel</strong><br><small>Originaltitel</small></a></td>
          <td>27.09.2026 10:40:27 Uhr <br><span data-livestamp="...">...</span></td>
        </tr>
    """
    soup = BeautifulSoup(html, "lxml")
    eintraege: list[Eintrag] = []

    for row in soup.select("table.table tbody tr"):
        cells = row.select("td")
        if len(cells) < 3:
            continue
        link = cells[0].select_one("a[href]")
        if not link:
            continue

        url = urljoin(BASE, link.get("href", ""))
        label = link.get_text(" ", strip=True)

        # "Witch Hat Atelier - S1 E6" -> Serie, Staffel, Episodennummer
        m = re.match(r"^(.*?)\s*-\s*S(\d+)\s*E(\d+)$", label)
        if m:
            serie, staffel, episode_nr = m.group(1).strip(), int(m.group(2)), int(m.group(3))
        else:
            # Fallback: Nummern aus der URL ziehen
            mu = re.search(r"staffel-(\d+)/episode-(\d+)", url)
            serie = label
            staffel = int(mu.group(1)) if mu else None
            episode_nr = int(mu.group(2)) if mu else None

        # Episodentitel
        strong = cells[1].select_one("strong")
        small = cells[1].select_one("small")
        titel = strong.get_text(" ", strip=True) if strong else ""
        titel_original = small.get_text(" ", strip=True) if small else ""

        # Datum: Text + Unix-Timestamp
        zeile_datum = cells[2].get_text(" ", strip=True)
        md = re.search(r"\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}", zeile_datum)
        datum = md.group(0) if md else ""
        span = cells[2].select_one("[data-livestamp]")
        datum_iso = ""
        if span and span.get("data-livestamp", "").isdigit():
            datum_iso = datetime.fromtimestamp(int(span["data-livestamp"])).isoformat(timespec="seconds")

        eintraege.append(
            Eintrag(
                serie=serie, staffel=staffel, episode_nr=episode_nr,
                titel=titel, titel_original=titel_original,
                datum=datum, datum_iso=datum_iso,
                url=url, seite=seite,
            )
        )

    return eintraege


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------

def crawl(client: httpx.Client, delay: float, max_pages: int | None) -> list[Eintrag]:
    alle: list[Eintrag] = []
    gesehen: set[tuple[str, str]] = set()  # (URL, Datum) - Wiederholungen bleiben sichtbar

    erste = get(client, WATCHED_URL.format(page=1))
    seiten_gesamt = max_page_from_html(erste.text) or 1
    print(f"Pagination: Seite 1 von {seiten_gesamt}")
    if max_pages:
        seiten_gesamt = min(seiten_gesamt, max_pages)
        print(f"max-pages gesetzt -> crawle {seiten_gesamt} Seiten")

    for seite in range(1, seiten_gesamt + 1):
        html = erste.text if seite == 1 else get(client, WATCHED_URL.format(page=seite)).text
        neu = 0
        for e in parse_page(html, seite):
            schluessel = (e.url, e.datum)
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            alle.append(e)
            neu += 1
        print(f"  Seite {seite}/{seiten_gesamt}: {neu} Einträge (gesamt {len(alle)})")

        if neu == 0 and seite < seiten_gesamt:
            print("  Seite ohne neue Einträge -> abbrechen")
            break
        if seite < seiten_gesamt:
            time.sleep(delay)

    return alle


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def zusammenfassen(eintraege: list[Eintrag]) -> list[dict]:
    """Gruppiert Einträge je Serie -> eine Zusammenfassungszeile je Serie."""
    nach_serie: dict[str, list[Eintrag]] = {}
    for e in eintraege:
        nach_serie.setdefault(e.serie, []).append(e)

    zeilen = []
    for serie, es in nach_serie.items():
        staffeln = sorted(s for s in {e.staffel for e in es} if s is not None)
        pro_staffel: dict[int, int] = {}
        for e in es:
            if e.staffel is not None:
                pro_staffel[e.staffel] = pro_staffel.get(e.staffel, 0) + 1
        daten = [e.datum_iso for e in es if e.datum_iso]
        zeilen.append({
            "serie": serie,
            "episoden": len(es),
            "staffeln": len(staffeln),
            "staffeln_details": ", ".join(f"S{s}: {pro_staffel[s]}" for s in staffeln),
            "von": min(daten) if daten else "",
            "bis": max(daten) if daten else "",
        })
    zeilen.sort(key=lambda z: z["episoden"], reverse=True)
    return zeilen


def export(eintraege: list[Eintrag]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    csv_pfad = OUTPUT_DIR / "verlauf.csv"
    felder = ["serie", "staffel", "episode_nr", "titel", "titel_original",
              "datum", "datum_iso", "url", "seite"]
    with csv_pfad.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=felder)
        writer.writeheader()
        for e in eintraege:
            writer.writerow(asdict(e))

    json_pfad = OUTPUT_DIR / "verlauf.json"
    json_pfad.write_text(
        json.dumps([asdict(e) for e in eintraege], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Zusammenfassung je Serie
    zusammenfassung = zusammenfassen(eintraege)
    sum_pfad = OUTPUT_DIR / "zusammenfassung.csv"
    sum_felder = ["serie", "episoden", "staffeln", "staffeln_details", "von", "bis"]
    with sum_pfad.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=sum_felder)
        writer.writeheader()
        writer.writerows(zusammenfassung)

    serien = {e.serie for e in eintraege}
    print(f"\nExport fertig:")
    print(f"  {len(eintraege)} Episoden-Einträge, {len(serien)} verschiedene Serien")
    print(f"  {csv_pfad}")
    print(f"  {json_pfad}")
    print(f"  {sum_pfad}")

    print(f"\nZusammenfassung ({len(zusammenfassung)} Serien):")
    for z in zusammenfassung:
        print(
            f"  {z['episoden']:>4}x  {z['serie']:<55} "
            f"[{z['staffeln']} Staffel(n): {z['staffeln_details'] or '-'}]"
        )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Zugangsdaten (.env)
# --------------------------------------------------------------------------

def env_laden() -> tuple[str, str]:
    """Liest E-Mail/Passwort aus der .env (leer, wenn nicht vorhanden)."""
    if not ENV_PFAD.exists():
        return "", ""
    werte = dotenv_values(ENV_PFAD)
    email = (werte.get("ANIWORLD_EMAIL") or "").strip()
    password = (werte.get("ANIWORLD_PASSWORD") or "").strip()
    return email, password


def env_schreiben(email: str, password: str) -> None:
    """Legt/aktualisiert die .env mit den Zugangsdaten."""
    if not ENV_PFAD.exists():
        ENV_PFAD.write_text("ANIWORLD_EMAIL=\nANIWORLD_PASSWORD=\n", encoding="utf-8")
    set_key(str(ENV_PFAD), "ANIWORLD_EMAIL", email)
    set_key(str(ENV_PFAD), "ANIWORLD_PASSWORD", password)


def zugangsdaten() -> tuple[str, str]:
    """Liest .env; fehlen Daten, werden sie abgefragt und gespeichert."""
    email, password = env_laden()
    if email and password:
        print(f"Zugangsdaten aus .env geladen ({email}).")
        return email, password

    print("Keine .env gefunden - Zugangsdaten werden abgefragt und gespeichert.")
    try:
        email = input(f"E-Mail{' [' + email + ']' if email else ''}: ").strip() or email
        password = getpass.getpass("Passwort: ")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAbbruch.")
    if not email or not password:
        sys.exit("\nAbbruch: E-Mail und Passwort werden benötigt.")
    env_schreiben(email, password)
    print(f"Gespeichert in {ENV_PFAD} (Klartext! .env ist in .gitignore).")
    return email, password


def login_abfragen(email: str) -> tuple[str, str]:
    """Fragt nach neuen Zugangsdaten nach einem fehlgeschlagenen Login."""
    print("\nNeue Zugangsdaten eingeben - Enter bei der E-Mail behält den Wert.")
    print("(Leeres Passwort oder Strg+C bricht ab.)")
    try:
        email_neu = input(f"E-Mail [{email}]: ").strip() or email
        password_neu = getpass.getpass("Passwort: ")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAbbruch.")
    if not password_neu:
        sys.exit("\nAbbruch: kein Passwort eingegeben.")
    env_schreiben(email_neu, password_neu)
    print(f"Aktualisiert in {ENV_PFAD}.")
    return email_neu, password_neu


# --------------------------------------------------------------------------
# Session (Browser-Login) & Browser-Fallback
# --------------------------------------------------------------------------

SESSION_PFAD = OUTPUT_DIR / "session.json"


def session_einlesen(client: httpx.Client) -> None:
    """Lädt Cookies aus einer Playwright-Storage-State-Datei in den httpx-Client."""
    state = json.loads(SESSION_PFAD.read_text(encoding="utf-8"))
    for cookie in state.get("cookies", []):
        client.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain", ""),
            path=cookie.get("path", "/"),
        )


def session_gueltig(client: httpx.Client) -> bool:
    """Testet, ob der Client eine eingeloggte Verlaufsseite abrufen kann."""
    try:
        probe = get(client, WATCHED_URL.format(page=1))
    except RuntimeError:
        return False
    return "userSessionStatus = true" in probe.text and probe.text.strip() != ""


def _browser_oeffnen(p):
    """Läuft mit Chrome bzw. Edge des Systems (soll es erst gar kein Chromium downloaden)."""
    versuche = [{"channel": "chrome"}, {"channel": "msedge"}, {}]
    letzter = None
    for kw in versuche:
        try:
            return p.chromium.launch(headless=False, **kw)
        except Exception as exc:  # noqa: BLE001
            letzter = exc
    raise SystemExit(
        "Kein Browser startbar. Bitte `python -m playwright install chromium` "
        f"ausführen. ({letzter})"
    )


def _login_formular_ausfuellen(page, email: str, password: str) -> bool:
    """Füllt E-Mail/Passwort in das Login-Formular ein (nur leere Felder)."""
    gefunden = False
    for selector, wert in [
        ('input[name="email"]', email),
        ('input[name="password"]', password),
    ]:
        inp = page.query_selector(selector)
        if not inp:
            continue
        gefunden = True
        try:
            if not inp.input_value():  # Werte des Nutzers nicht überschreiben
                inp.fill(wert)
        except Exception:  # noqa: BLE001
            pass
    return gefunden


def _captcha_fokussieren(page) -> bool:
    """Fokussiert ein Captcha-Eingabefeld bzw. klickt eine reCAPTCHA-Checkbox an."""
    kandidaten = [
        'input[name="captcha"]',
        'input#captcha',
        'input[class*="captcha"]',
        'div[class*="captcha"] input',
        'input[placeholder*="aptcha" i]',
    ]
    for sel in kandidaten:
        inp = page.query_selector(sel)
        if inp:
            try:
                inp.focus()
                print("  Captcha-Feld fokussiert – Code eintippen und absenden.")
                return True
            except Exception:  # noqa: BLE001
                pass
    # Google reCAPTCHA-Checkbox (falls eingebettet)
    box = page.query_selector(
        '.g-recaptcha iframe, iframe[src*="recaptcha"], '
        'iframe[src*="hcaptcha"]'
    )
    if box:
        try:
            box.click()
            print("  Captcha-Checkbox angeklickt – ggf. Bildaufgabe im Fenster lösen.")
            return True
        except Exception:  # noqa: BLE001
            pass
    return False


def browser_login() -> None:
    """Öffnet einen echten Browser; der Nutzer loggt sich einmalig ein (ggf. Captcha)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright ist nicht installiert.\n"
            "Installation:  pip install -r requirements-browser.txt\n"
            "dann:          python -m playwright install chromium"
        )

    with sync_playwright() as p:
        browser = _browser_oeffnen(p)
        context = browser.new_context(locale="de-DE")
        page = context.new_page()
        page.goto(f"{BASE}/login", wait_until="domcontentloaded")

        email, password = env_laden()
        if email and password:
            if _login_formular_ausfuellen(page, email, password):
                print(f"\nBrowser geöffnet – Zugangsdaten aus .env vorausgefüllt ({email}).")
                print("Falls ein Captcha erscheint, bekommt das Feld automatisch den Fokus.")
            else:
                print("\nBrowser geöffnet – kein Login-Formular gefunden. Bitte manuell einloggen.")
        else:
            print("\nBrowser geöffnet – keine Zugangsdaten in .env gefunden.")
            print("Bitte manuell einloggen.")

        print("Das Skript wartet, bis du eingeloggt bist …")
        ende = time.monotonic() + 300
        eingeloggt = False
        while time.monotonic() < ende:
            try:
                html = page.content()
            except Exception:  # noqa: BLE001
                html = ""
            if "userSessionStatus = true" in html and 'name="password"' not in html:
                eingeloggt = True
                break
            # Formular erscheint neu (z.B. mit Captcha nach dem Absenden): erneut füllen/fokussieren
            if 'name="password"' in html:
                _login_formular_ausfuellen(page, email, password)
                _captcha_fokussieren(page)
            time.sleep(1)
        if eingeloggt:
            context.storage_state(path=str(SESSION_PFAD))
        browser.close()

    if not eingeloggt:
        sys.exit("Login im Browser nicht abgeschlossen (nach 5 Minuten).")
    print(f"Session gespeichert: {SESSION_PFAD}")


def crawl_browser(delay: float, max_pages: int | None) -> list[Eintrag]:
    """Crawlt die Verlaufsseiten komplett im Browser (Fallback bei SSL-Blockaden)."""
    from playwright.sync_api import sync_playwright

    alle: list[Eintrag] = []
    gesehen: set[tuple[str, str]] = set()

    with sync_playwright() as p:
        browser = _browser_oeffnen(p)
        context = browser.new_context(locale="de-DE", storage_state=str(SESSION_PFAD))
        page = context.new_page()

        page.goto(WATCHED_URL.format(page=1), wait_until="domcontentloaded")
        seiten_gesamt = max_page_from_html(page.content()) or 1
        if max_pages:
            seiten_gesamt = min(seiten_gesamt, max_pages)
        print(f"Pagination: Seite 1 von {seiten_gesamt} (im Browser)")

        for seite in range(1, seiten_gesamt + 1):
            if seite == 1:
                page.goto(WATCHED_URL.format(page=1), wait_until="domcontentloaded")
            else:
                page.goto(WATCHED_URL.format(page=seite), wait_until="domcontentloaded")
            neu = 0
            for e in parse_page(page.content(), seite):
                schluessel = (e.url, e.datum)
                if schluessel in gesehen:
                    continue
                gesehen.add(schluessel)
                alle.append(e)
                neu += 1
            print(f"  Seite {seite}/{seiten_gesamt}: {neu} Einträge (gesamt {len(alle)})")
            if neu == 0 and seite < seiten_gesamt:
                print("  Seite ohne neue Einträge -> abbrechen")
                break
            if seite < seiten_gesamt:
                time.sleep(delay)

        browser.close()
    return alle


def form_login(client: httpx.Client) -> None:
    """Login über das HTML-Formular (mit Wiederholung bei falschen Daten)."""
    email, password = zugangsdaten()
    while True:
        try:
            login(client, email, password)
            return
        except CaptchaFehler as exc:
            print(f"\nCaptcha erforderlich: {exc}")
            print("\n")
            print("Versuche: `python export_verlauf.py --browser-login`")
            print("\n")
            sys.exit(1)
        except RuntimeError as exc:
            print(f"\nLogin fehlgeschlagen: {exc}")
            email, password = login_abfragen(email)


def main() -> None:
    parser = argparse.ArgumentParser(description="AniWorld-Verlauf exportieren")
    parser.add_argument("--inspect", action="store_true",
                        help="Nur Seite 1 als HTML speichern (Struktur-Analyse)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Maximale Anzahl Seiten (Default: alle)")
    parser.add_argument("--delay", type=float, default=0.7,
                        help="Pause zwischen Seiten in Sekunden (Default: 0.7)")
    parser.add_argument("--browser-login", action="store_true",
                        help="Im echten Browser einloggen (Captcha-fähig) und Session speichern")
    args = parser.parse_args()

    browser_modus = False

    with make_client() as client:
        if args.browser_login:
            # 1) Browser-Login: Session wird in output/session.json gelegt
            browser_login()
            session_einlesen(client)
            if session_gueltig(client):
                print("Browser-Session übernommen – Crawl läuft über httpx.")
            else:
                print("httpx kann die Browser-Session nicht nutzen – Crawl läuft im Browser.")
                browser_modus = True
        elif SESSION_PFAD.exists():
            # 2) Gespeicherte Session wiederverwenden
            session_einlesen(client)
            if session_gueltig(client):
                print(f"Gültige Session aus {SESSION_PFAD} übernommen.")
            else:
                print("Gespeicherte Session ungültig – versuche Browser-Crawl.")
                browser_modus = True
        else:
            # 3) Formular-Login (Primärweg)
            form_login(client)

        if args.inspect:
            if browser_modus:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = _browser_oeffnen(p)
                    context = browser.new_context(
                        locale="de-DE", storage_state=str(SESSION_PFAD))
                    page = context.new_page()
                    page.goto(WATCHED_URL.format(page=1), wait_until="domcontentloaded")
                    html = page.content()
                    browser.close()
            else:
                html = get(client, WATCHED_URL.format(page=1)).text
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            pfad = DEBUG_DIR / "watched_seite1.html"
            pfad.write_text(html, encoding="utf-8")
            print(f"Seite 1 gespeichert: {pfad} ({len(html)} Zeichen)")
            print(f"Pagination deutet auf max. Seite: {max_page_from_html(html)}")
            return

        try:
            if browser_modus:
                eintraege = crawl_browser(delay=args.delay, max_pages=args.max_pages)
            else:
                eintraege = crawl(client, delay=args.delay, max_pages=args.max_pages)
        except RuntimeError as exc:
            sys.exit(f"Fehler beim Crawlen: {exc}")

    if not eintraege:
        sys.exit("Keine Einträge gefunden - Struktur prüfen: "
                 "python export_verlauf.py --inspect")
    export(eintraege)


if __name__ == "__main__":
    main()
