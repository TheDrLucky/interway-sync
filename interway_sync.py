#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


LOGIN_URL = "https://planningtechweb.interway.fr/login"
PLANNING_URL = "https://planningtechweb.interway.fr/"
STATUSES = {"PM", "PA", "NO", "NW", "NT", "NR", "-"}
WORK_STATUSES = {"PM", "PA", "NO", "NW"}
MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}
TOOLTIP_SELECTOR = ",".join(
    (
        '[role="tooltip"]',
        ".mud-tooltip",
        ".mud-popover",
        ".mud-popover-open",
        '[class*="tooltip" i]',
        '[class*="popover" i]',
    )
)
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
PARIS = ZoneInfo("Europe/Paris")
INTERWAY_EVENT_SOURCE = "Source : Planning Tech Web Interway"


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ")).strip()


def fold(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value).lower()
        if not unicodedata.combining(character)
    )


def parse_epacks(text: str) -> list[dict[str, object]]:
    text = (text or "").replace("\u00a0", " ")
    starts = list(re.finditer(r"(?m)^\s*\d{1,2}:\d{2}\s*:", text))
    results: list[dict[str, object]] = []
    seen: set[str] = set()

    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        record = text[start.start() : end].strip()
        heading = re.match(
            r"(?P<heure>(?:[01]?\d|2[0-3]):[0-5]\d)\s*:\s*"
            r"(?P<type>[^/\n]+?)\s+-\s+(?P<ville>[^/\n]+?)\s*/",
            record,
            re.IGNORECASE,
        )
        if not heading or clean(heading.group("type")).upper() != "EPACK":
            continue

        reference_match = re.search(r"\b(SV\d+)\b", record, re.IGNORECASE)
        duration_match = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*h\b", record, re.IGNORECASE)
        parts = record.split("/")
        activity = re.sub(r"\s*-\s*$", "", clean(parts[1])) if len(parts) > 1 else ""
        duration = float(duration_match.group(1).replace(",", ".")) if duration_match else None
        reference = reference_match.group(1).upper() if reference_match else None
        dedupe_key = reference or f'{heading.group("heure")}|{clean(heading.group("ville"))}'
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        results.append(
            {
                "type": "EPACK",
                "heure": heading.group("heure").zfill(5),
                "ville": clean(heading.group("ville")),
                "activite": activity or None,
                "duree_heures": duration,
                "duree_minutes": round(duration * 60) if duration is not None else None,
                "reference": reference,
            }
        )

    return results


def infer_dates(days: list[int], start_month: int, start_year: int) -> list[date]:
    month, year, previous = start_month, start_year, None
    dates: list[date] = []
    for day in days:
        if previous is not None and day < previous:
            month += 1
            if month == 13:
                month, year = 1, year + 1
        try:
            dates.append(date(year, month, day))
        except ValueError as error:
            raise RuntimeError(
                "La période affichée ne correspond pas aux jours du calendrier. "
                "Affiche une période complète puis relance l'extraction."
            ) from error
        previous = day
    return dates


def find_period(page_text: str, year_override: int | None) -> tuple[str, int, int]:
    match = re.search(r"BUSINESS\s+PARTNER\s*\(([^)]+)\)", page_text, re.IGNORECASE)
    if not match:
        raise RuntimeError("Période du calendrier Interway introuvable.")

    for month_name, year in re.findall(r"([A-Za-zÀ-ÿ]+)\s+(\d{4})", match.group(1)):
        month = MONTHS.get(fold(month_name))
        if month:
            return clean(match.group(0)), month, year_override or int(year)
    raise RuntimeError("Mois et année de la période Interway introuvables.")


def read_credentials(path: Path) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError(f"Identifiants Interway illisibles : {path}") from error
    if len(lines) != 2 or not lines[0] or not lines[1]:
        raise RuntimeError("Le fichier d'identifiants Interway est invalide.")
    return lines[0], lines[1]


def login_with_credentials(page: Page, credentials: Path) -> None:
    username, password = read_credentials(credentials)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    username_field = page.locator('input[type="text"]')
    password_field = page.locator('input[type="password"]')
    submit = page.get_by_role("button", name=re.compile("se connecter", re.IGNORECASE))
    username_field.wait_for(state="visible", timeout=15_000)
    if username_field.count() != 1 or password_field.count() != 1 or submit.count() != 1:
        raise RuntimeError("Formulaire de connexion Interway introuvable.")
    username_field.fill(username)
    password_field.fill(password)
    submit.click()
    page.wait_for_timeout(2_500)
    page.goto(PLANNING_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1_500)


def wait_for_technician(page: Page, technician_id: str, timeout_ms: int = 15_000) -> None:
    error = None
    for _ in range(max(1, timeout_ms // 500)):
        try:
            require_technician(page, technician_id)
            return
        except RuntimeError as current_error:
            error = current_error
            page.wait_for_timeout(500)
    raise error or RuntimeError("Planning Interway introuvable.")


def find_calendar(page: Page) -> Locator:
    tables = page.locator("table")
    for index in range(tables.count()):
        table = tables.nth(index)
        text = clean(table.inner_text())
        if re.search(r"\bLundi\b", text, re.IGNORECASE) and re.search(
            r"\bDimanche\b", text, re.IGNORECASE
        ):
            return table
    raise RuntimeError("Calendrier personnel Interway introuvable.")


def require_technician(page: Page, technician_id: str) -> None:
    rows = page.locator("tr")
    for index in range(rows.count()):
        values = [clean(value) for value in rows.nth(index).locator("th, td").all_inner_texts()]
        if technician_id in values or (
            technician_id.isdigit()
            and any(value.isdigit() and int(value) == int(technician_id) for value in values)
        ):
            return
    raise RuntimeError(
        f"Ligne du technicien {technician_id} introuvable. "
        "Vérifie la connexion et affiche la page du planning."
    )


def calendar_cells(table: Locator) -> list[tuple[int, str, Locator]]:
    result: list[tuple[int, str, Locator]] = []
    cells = table.locator("td")
    for index in range(cells.count()):
        cell = cells.nth(index)
        tokens = clean(cell.inner_text()).split()
        day = next((int(token) for token in tokens if re.fullmatch(r"0?[1-9]|[12]\d|3[01]", token)), None)
        status = next((token.upper() for token in tokens if token.upper() in STATUSES), None)
        if day is not None and status:
            result.append((day, status, cell))
    if not result:
        raise RuntimeError("Aucune journée lisible dans le calendrier Interway.")
    return result


def attribute_texts(cell: Locator) -> list[str]:
    return cell.evaluate(
        """cell => [cell, ...cell.querySelectorAll('*')].flatMap(node =>
          [...node.attributes]
            .filter(attribute => /^(title|aria-label|data-.*(?:title|tooltip|content).*)$/i.test(attribute.name))
            .map(attribute => attribute.value)
        )"""
    )


def visible_tooltip_texts(page: Page) -> list[str]:
    return page.locator(TOOLTIP_SELECTOR).evaluate_all(
        r"""elements => elements.flatMap(element => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          const text = (element.innerText || element.textContent || '').trim();
          const visible = style.display !== 'none' && style.visibility !== 'hidden' &&
            Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
          return visible && text.length < 2500 && /\b\d{1,2}:\d{2}\b/.test(text) &&
            /\b(?:SV\d+|EPACK|LOTO|GIFI|MCO)\b/i.test(text) ? [text] : [];
        })"""
    )


def extract(
    page: Page,
    technician_id: str,
    hover_ms: int,
    year: int | None,
    seen_dates: set[str] | None = None,
) -> dict[str, object]:
    require_technician(page, technician_id)
    period, start_month, start_year = find_period(page.locator("body").inner_text(), year)
    cells = calendar_cells(find_calendar(page))
    dates = infer_dates([day for day, _, _ in cells], start_month, start_year)
    days = []

    for current_date, (_, status, cell) in zip(dates, cells):
        if seen_dates and current_date.isoformat() in seen_dates:
            continue
        texts = attribute_texts(cell)
        if status in WORK_STATUSES:
            page.mouse.move(1, 1)
            page.wait_for_timeout(100)
            cell.scroll_into_view_if_needed()
            cell.hover(timeout=5_000)
            page.wait_for_timeout(hover_ms)
            texts.extend(visible_tooltip_texts(page))

        interventions = []
        seen = set()
        for text in texts:
            for intervention in parse_epacks(text):
                key = intervention["reference"] or (
                    intervention["heure"],
                    intervention["ville"],
                )
                if key not in seen:
                    seen.add(key)
                    interventions.append(intervention)
        days.append(
            {
                "date": current_date.isoformat(),
                "statut": status,
                "interventions": interventions,
            }
        )

    return {
        "source": "Planning Tech Web Interway",
        "technicien": technician_id,
        "periode": period,
        "capture_utc": datetime.now(timezone.utc).isoformat(),
        "jours": days,
    }


def shift_week(page: Page, direction: str, wait_ms: int) -> bool:
    before = clean(find_calendar(page).inner_text())
    button = page.get_by_role("button", name=direction, exact=True)
    if button.count() != 1:
        raise RuntimeError(f"Flèche « {direction} » introuvable.")
    button.click()
    for _ in range(max(wait_ms, 15_000) // 500):
        page.wait_for_timeout(500)
        if clean(find_calendar(page).inner_text()) != before:
            return True
    return False


def google_event(day: dict[str, object], intervention: dict[str, object]) -> dict[str, object]:
    start = datetime.fromisoformat(f'{day["date"]}T{intervention["heure"]}').replace(tzinfo=PARIS)
    end = start + timedelta(minutes=int(intervention.get("duree_minutes") or 15))
    reference = intervention.get("reference")
    identity = reference or f'{day["date"]}|{intervention["heure"]}|{intervention["ville"]}'
    description = [INTERWAY_EVENT_SOURCE]
    if intervention.get("activite"):
        description.append(f'Activité : {intervention["activite"]}')
    if reference:
        description.append(f'Référence : {reference}')
    return {
        "id": hashlib.sha256(f"interway:{identity}".encode()).hexdigest(),
        "summary": f'EPACK - {intervention["ville"]}',
        "location": intervention["ville"],
        "description": "\n".join(description),
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Paris"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Paris"},
        "extendedProperties": {"private": {"interwaySync": "1"}},
    }


def sync_google_calendar(
    planning: dict[str, object], credentials_path: Path, calendar_id: str, session=None
) -> dict[str, int]:
    if session is None:
        try:
            credentials = service_account.Credentials.from_service_account_file(
                credentials_path, scopes=[CALENDAR_SCOPE]
            )
            session = AuthorizedSession(credentials)
        except (OSError, ValueError) as error:
            raise RuntimeError("La clé Google Agenda est invalide ou illisible.") from error

    base_url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"
    try:
        dates = [date.fromisoformat(str(day["date"])) for day in planning["jours"]]
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("La période Interway est invalide ; aucune suppression effectuée.") from error
    if not dates:
        raise RuntimeError("La période Interway est vide ; aucune suppression effectuée.")

    stats = {"created": 0, "updated": 0, "unchanged": 0, "pending_deletion": 0, "deleted": 0}
    compared_fields = (
        "summary",
        "location",
        "description",
        "start",
        "end",
        "extendedProperties",
    )
    current_events = {
        event["id"]: event
        for day in planning["jours"]
        for intervention in day["interventions"]
        for event in (google_event(day, intervention),)
    }

    existing_events = {}
    page_token = None
    try:
        while True:
            params = {
                "timeMin": datetime.combine(min(dates), datetime.min.time(), PARIS).isoformat(),
                "timeMax": datetime.combine(
                    max(dates) + timedelta(days=1), datetime.min.time(), PARIS
                ).isoformat(),
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": 2500,
            }
            if page_token:
                params["pageToken"] = page_token
            response = session.get(base_url, params=params)
            if not response.ok:
                raise RuntimeError(f"Google Agenda a répondu avec l'erreur {response.status_code}.")
            data = response.json()
            for event in data.get("items", []):
                private = event.get("extendedProperties", {}).get("private", {})
                if private.get("interwaySync") == "1" or event.get(
                    "description", ""
                ).splitlines()[:1] == [INTERWAY_EVENT_SOURCE]:
                    existing_events[event["id"]] = event
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError("Connexion à Google Agenda impossible.") from error

    for event in current_events.values():
        event_url = f'{base_url}/{event["id"]}'
        try:
            existing = existing_events.get(event["id"])
            if existing is None:
                response = session.post(base_url, params={"sendUpdates": "none"}, json=event)
                if response.status_code == 409:
                    response = session.put(
                        event_url, params={"sendUpdates": "none"}, json=event
                    )
                    action = "updated"
                else:
                    action = "created"
            elif all(existing.get(key) == event[key] for key in compared_fields):
                stats["unchanged"] += 1
                continue
            else:
                response = session.put(event_url, params={"sendUpdates": "none"}, json=event)
                action = "updated"
        except Exception as error:
            raise RuntimeError("Connexion à Google Agenda impossible.") from error
        if not response.ok:
            raise RuntimeError(f"Google Agenda a répondu avec l'erreur {response.status_code}.")
        stats[action] += 1

    for event_id, event in existing_events.items():
        if event_id in current_events:
            continue
        event_url = f"{base_url}/{quote(event_id, safe='')}"
        private = dict(event.get("extendedProperties", {}).get("private", {}))
        try:
            if private.get("interwayMissing") == "1":
                response = session.delete(event_url, params={"sendUpdates": "none"})
                action = "deleted"
            else:
                private.update({"interwaySync": "1", "interwayMissing": "1"})
                response = session.patch(
                    event_url,
                    params={"sendUpdates": "none"},
                    json={"extendedProperties": {"private": private}},
                )
                action = "pending_deletion"
        except Exception as error:
            raise RuntimeError("Connexion à Google Agenda impossible.") from error
        if not response.ok:
            raise RuntimeError(f"Google Agenda a répondu avec l'erreur {response.status_code}.")
        stats[action] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrait le planning Interway visible en JSON.")
    parser.add_argument("--login", action="store_true", help="ouvre Chromium pour une connexion manuelle")
    parser.add_argument("--headed", action="store_true", help="affiche Chromium pendant l'extraction")
    parser.add_argument("--url", default=os.getenv("INTERWAY_URL"))
    parser.add_argument("--technicien", default=os.getenv("INTERWAY_TECHNICIAN"))
    parser.add_argument("--credentials", type=Path, help="fichier contenant identifiant puis mot de passe")
    parser.add_argument("--profile", type=Path, default=Path("browser/profile"))
    parser.add_argument("--state", type=Path, default=Path("browser/state.json"))
    parser.add_argument("--output", type=Path, default=Path("planning.json"))
    parser.add_argument("--hover-ms", type=int, default=1800)
    parser.add_argument("--previous-weeks", type=int, default=0)
    parser.add_argument("--next-weeks", type=int, default=0)
    parser.add_argument("--navigation-ms", type=int, default=1800)
    parser.add_argument("--year", type=int, help="année de départ si l'intitulé Interway est ambigu")
    parser.add_argument(
        "--google-credentials",
        type=Path,
        default=os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        help="clé JSON du compte de service Google",
    )
    parser.add_argument(
        "--google-calendar",
        default=os.getenv("GOOGLE_CALENDAR_ID"),
        help="identifiant de l'agenda Google cible",
    )
    args = parser.parse_args()

    if args.hover_ms < 200:
        parser.error("--hover-ms doit être supérieur ou égal à 200")
    if args.previous_weeks < 0 or args.next_weeks < 0:
        parser.error("Le nombre de semaines ne peut pas être négatif")
    if args.navigation_ms < 200:
        parser.error("--navigation-ms doit être supérieur ou égal à 200")
    if not args.technicien:
        parser.error("--technicien est obligatoire")
    if bool(args.google_credentials) != bool(args.google_calendar):
        parser.error("--google-credentials et --google-calendar doivent être fournis ensemble")
    args.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.profile.chmod(0o700)

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(args.profile),
                headless=not (args.login or args.headed),
                viewport={"width": 1440, "height": 1000},
            )
            if args.state.exists():
                state = json.loads(args.state.read_text(encoding="utf-8"))
                if state.get("cookies"):
                    context.add_cookies(state["cookies"])
                if state.get("sessionStorage"):
                    saved_session = json.dumps(state["sessionStorage"])
                    context.add_init_script(
                        f"""(() => {{
                          const saved = {saved_session};
                          for (const [key, value] of Object.entries(saved[location.origin] || {{}})) {{
                            sessionStorage.setItem(key, value);
                          }}
                        }})()"""
                    )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                args.url or (LOGIN_URL if args.login else PLANNING_URL),
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            if args.login:
                input(
                    f"Connecte-toi, affiche le planning contenant la ligne {args.technicien}, "
                    "puis appuie sur Entrée ici… "
                )
            else:
                page.wait_for_timeout(1_500)

            if not args.login:
                try:
                    wait_for_technician(page, args.technicien)
                except RuntimeError:
                    if not args.credentials:
                        raise
                    login_with_credentials(page, args.credentials)
                    wait_for_technician(page, args.technicien)

            for _ in range(args.previous_weeks):
                if not shift_week(page, "Précédent", args.navigation_ms):
                    break

            merged_days = {}
            periods = []
            window_count = args.previous_weeks + args.next_weeks + 1
            for window in range(window_count):
                current = extract(
                    page,
                    args.technicien,
                    args.hover_ms,
                    args.year,
                    set(merged_days),
                )
                merged_days.update((day["date"], day) for day in current["jours"])
                if not periods or periods[-1] != current["periode"]:
                    periods.append(current["periode"])
                if window + 1 < window_count:
                    if not shift_week(page, "Suivant", args.navigation_ms):
                        break

            result = {
                "source": "Planning Tech Web Interway",
                "technicien": args.technicien,
                "periode": periods[0] if len(periods) == 1 else f"{periods[0]} → {periods[-1]}",
                "capture_utc": datetime.now(timezone.utc).isoformat(),
                "jours": [merged_days[key] for key in sorted(merged_days)],
            }
            args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            args.state.parent.chmod(0o700)
            state = context.storage_state()
            state["sessionStorage"] = {
                page.evaluate("location.origin"): page.evaluate(
                    "Object.fromEntries(Object.entries(sessionStorage))"
                )
            }
            args.state.write_text(json.dumps(state), encoding="utf-8")
            args.state.chmod(0o600)
            context.close()
    except (PlaywrightTimeoutError, RuntimeError) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        print("Relance avec --login si la session Interway n'est pas active.", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    epack_count = sum(len(day["interventions"]) for day in result["jours"])
    print(f'{len(result["jours"])} jours et {epack_count} EPACK écrits dans {args.output}')
    if args.google_credentials:
        try:
            stats = sync_google_calendar(result, args.google_credentials, args.google_calendar)
        except RuntimeError as error:
            print(f"Erreur : {error}", file=sys.stderr)
            return 1
        print(
            "Google Agenda : "
            f'{stats["created"]} créé(s), {stats["updated"]} modifié(s), '
            f'{stats["unchanged"]} inchangé(s), {stats["pending_deletion"]} en attente, '
            f'{stats["deleted"]} supprimé(s).'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
