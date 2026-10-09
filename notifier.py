"""Collect upcoming CERN Indico events and post a digest."""

import html
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from fpdf import FPDF

INDICO_BASE = "https://indico.cern.ch"
INDICO_EXPORT = INDICO_BASE + "/export/categ/{cat_id}.json"
TELEGRAM_SEND = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_DOCUMENT = "https://api.telegram.org/bot{token}/sendDocument"
TELEGRAM_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
REQUEST_TIMEOUT = 20
TELEGRAM_MESSAGE_LIMIT = 4000
ZURICH = ZoneInfo("Europe/Zurich")
WINDOW_DAYS = 7

# Live public categories on indico.cern.ch for the named series.
# 2 is "TEST Category"; 3180, 3182 and 3183 currently return no events.
CATEGORIES = {
    72: "Academic Training",
    75: "CERN Colloquia",
    3247: "EP Seminars",
    84: "Detector Seminars",
}


def fetch_category(cat_id: int) -> list[dict]:
    """Return raw event dicts for one category. Raises on HTTP or JSON failure."""
    response = requests.get(
        INDICO_EXPORT.format(cat_id=cat_id),
        params={"from": "today", "to": f"+{WINDOW_DAYS}d"},
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "indico-notifier"},
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise ValueError("Indico response has no results list")
    return [item for item in results if isinstance(item, dict)]


def parse_end(raw: dict, key: str, start: datetime) -> datetime | None:
    end_raw = raw.get(key) or {}
    if not isinstance(end_raw, dict):
        return None
    end = parse_start({"startDate": end_raw})
    if end is None or end <= start:
        return None
    return end


def parse_start(raw: dict) -> datetime | None:
    start = raw.get("startDate") or {}
    if not isinstance(start, dict):
        return None
    date = str(start.get("date") or "").strip()
    time = str(start.get("time") or "00:00:00").strip()
    if not date:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{date} {time}", fmt)
        except ValueError:
            continue
    try:
        return datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None


def format_place(raw: dict) -> str:
    location = str(raw.get("location") or "").strip()
    room_full = str(raw.get("roomFullname") or "").strip()
    room = str(raw.get("room") or "").strip()
    room_label = room_full or room
    if location and room_label and room_label not in location:
        return f"{location}, {room_label}"
    return room_label or location or "Not announced"


def parse_event(raw: dict, category: str) -> dict | None:
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or "").strip()
    start = parse_start(raw)
    if not title or start is None:
        return None
    tz = ""
    start_raw = raw.get("startDate") or {}
    if isinstance(start_raw, dict):
        tz = str(start_raw.get("tz") or "").strip()
    return {
        "id": str(raw.get("id") or url or title),
        "title": title,
        "start": start,
        "end": parse_end(raw, "endDate", start),
        "tz": tz,
        "place": format_place(raw),
        "url": url or "https://indico.cern.ch/",
        "category": category,
    }


def collect_events() -> tuple[list[dict], list[str]]:
    events: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    for cat_id, name in CATEGORIES.items():
        try:
            raw_events = fetch_category(cat_id)
        except requests.Timeout:
            errors.append(f"{name} ({cat_id}): request timed out")
            continue
        except requests.RequestException as exc:
            errors.append(f"{name} ({cat_id}): request failed ({exc})")
            continue
        except ValueError as exc:
            errors.append(f"{name} ({cat_id}): {exc}")
            continue

        for raw in raw_events:
            event = parse_event(raw, name)
            if event is None or event["id"] in seen:
                continue
            seen.add(event["id"])
            events.append(event)

    events.sort(key=lambda event: (event["start"], event["title"].lower()))
    return events, errors


def window_dates() -> tuple[datetime.date, datetime.date]:
    today = datetime.now(ZURICH).date()
    return today, today + timedelta(days=WINDOW_DAYS)


def in_window(start: datetime) -> bool:
    first, last = window_dates()
    return first <= start.date() <= last


def indico_get(path: str, token: str, params: list[tuple[str, str]] | None = None) -> requests.Response:
    try:
        return requests.get(
            INDICO_BASE + path,
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": "indico-notifier",
                "Authorization": f"Bearer {token}",
            },
        )
    except requests.Timeout as exc:
        raise RuntimeError(f"{path}: request timed out") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"{path}: request failed ({exc.__class__.__name__})") from exc


def auth_failure(response: requests.Response, path: str) -> str | None:
    if response.status_code == 401:
        return f"{path}: Indico rejected the API token"
    if response.status_code == 403:
        return f"{path}: the API token is missing permission for this request"
    if response.status_code >= 400:
        return f"{path}: Indico returned HTTP {response.status_code}"
    return None


def unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def parse_ics_datetime(value: str) -> tuple[datetime, str] | None:
    value = value.strip()
    if value.endswith("Z"):
        try:
            parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        local = parsed.astimezone(ZURICH).replace(tzinfo=None)
        return local, "Europe/Zurich"
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt), ""
        except ValueError:
            continue
    return None


def parse_ics_events(text: str) -> list[dict]:
    events: list[dict] = []
    current: dict[str, str] | None = None
    for line in unfold_ics(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                event = ics_event(current)
                if event is not None and in_window(event["start"]):
                    events.append(event)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, value = line.split(":", 1)
        name, _, params = left.partition(";")
        name = name.upper()
        current[name] = unescape_ics(value)
        if name == "DTSTART" and "TZID=" in params:
            current["_TZID"] = params.split("TZID=", 1)[1].split(";")[0]
    return events


def ics_event(fields: dict[str, str]) -> dict | None:
    parsed = parse_ics_datetime(fields.get("DTSTART", ""))
    title = fields.get("SUMMARY", "").split("\n", 1)[0].strip()
    if parsed is None or not title:
        return None
    start, tz = parsed
    tz = tz or fields.get("_TZID") or "Europe/Zurich"
    end_parsed = parse_ics_datetime(fields.get("DTEND", ""))
    end = end_parsed[0] if end_parsed and end_parsed[0] > start else None
    url = fields.get("URL") or INDICO_BASE + "/"
    return {
        "id": fields.get("UID") or url or title,
        "title": title,
        "start": start,
        "end": end,
        "tz": tz or "Europe/Zurich",
        "place": fields.get("LOCATION") or "Not announced",
        "url": url,
        "category": "Your agenda",
    }


def slot_end(raw: dict, start: datetime) -> datetime | None:
    return parse_end(raw, "endDT", start) or parse_end(raw, "endDate", start)


def slot_start(raw: dict) -> tuple[datetime, str] | None:
    start_raw = raw.get("startDT") or raw.get("startDate") or {}
    if not isinstance(start_raw, dict):
        return None
    start = parse_start({"startDate": start_raw})
    if start is None:
        return None
    tz = str(start_raw.get("tz") or "Europe/Zurich").strip()
    return start, tz


def reservation_place(item: dict) -> str:
    room = item.get("room")
    if isinstance(room, dict):
        room_label = str(
            room.get("fullName") or room.get("fullname") or room.get("verbose_name") or room.get("name") or ""
        ).strip()
    else:
        room_label = str(room or item.get("roomFullname") or "").strip()
    location = str(item.get("location") or "CERN").strip()
    if location and room_label and room_label not in location:
        return f"{location}, {room_label}"
    return room_label or location or "CERN"


def parse_reservations(payload: dict) -> list[dict]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("Indico reservation response has no results list")
    events: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("isCancelled") or item.get("is_cancelled") or item.get("isRejected") or item.get("is_rejected"):
            continue
        occurrences = item.get("occurrences")
        slots = occurrences if isinstance(occurrences, list) and occurrences else [item]
        for index, slot in enumerate(slots):
            if not isinstance(slot, dict):
                continue
            parsed = slot_start(slot) or slot_start(item)
            if parsed is None or not in_window(parsed[0]):
                continue
            start, tz = parsed
            end = None
            if isinstance(slot, dict):
                end = slot_end(slot, start)
            if end is None:
                end = slot_end(item, start)
            title = str(item.get("reason") or item.get("bookingReason") or "Room booking").strip()
            url = str(item.get("bookingUrl") or item.get("url") or f"{INDICO_BASE}/rooms/").strip()
            events.append(
                {
                    "id": f"booking-{item.get('id', title)}-{index}",
                    "title": title or "Room booking",
                    "start": start,
                    "end": end,
                    "tz": tz,
                    "place": reservation_place(item),
                    "url": url,
                    "category": "Room booking",
                }
            )
    return events


def fetch_agenda(token: str) -> tuple[list[dict], str | None]:
    # Indico rejects the relative value "today" on this endpoint (HTTP 422).
    first, _last = window_dates()
    response = indico_get(
        "/user/dashboard.ics",
        token,
        [
            ("from", first.isoformat()),
            ("include", "linked"),
            ("include", "categories"),
            ("limit", "100"),
        ],
    )
    failure = auth_failure(response, "Your agenda")
    if failure:
        if response.status_code == 403:
            failure += ". Enable Everything (only GET) on the token"
        return [], failure
    if "BEGIN:VCALENDAR" not in response.text:
        return [], "Your agenda: Indico did not return a calendar"
    return parse_ics_events(response.text), None


def fetch_bookings(token: str) -> tuple[list[dict], str | None]:
    user_response = indico_get("/api/user/", token)
    failure = auth_failure(user_response, "Indico profile")
    if failure:
        if user_response.status_code == 403:
            failure += ". Enable User information (read only) on the token"
        return [], failure
    try:
        user = user_response.json()
    except ValueError:
        return [], "Indico profile: response was not valid JSON"
    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    if not first_name or not last_name:
        return [], "Indico profile did not include a name, so room bookings were skipped"

    patterns = (f"*{last_name}, {first_name}*", f"*{first_name} {last_name}*")
    bookings: list[dict] = []
    seen: set[str] = set()
    for pattern in patterns:
        response = indico_get(
            "/export/reservation/CERN.json",
            token,
            [
                ("from", "today"),
                ("to", f"+{WINDOW_DAYS}d"),
                ("detail", "reservations"),
                ("occurrences", "yes"),
                ("cancelled", "no"),
                ("rejected", "no"),
                ("bookedfor", pattern),
            ],
        )
        failure = auth_failure(response, "Room bookings")
        if failure:
            if response.status_code == 403:
                failure += ". Enable Classic API (read only) on the token"
            return [], failure
        try:
            found = parse_reservations(response.json())
        except ValueError as exc:
            return [], f"Room bookings: {exc}"
        for booking in found:
            if booking["id"] in seen:
                continue
            seen.add(booking["id"])
            bookings.append(booking)
        if bookings:
            break
    return bookings, None


def fetch_account_items(token: str) -> tuple[list[dict], list[dict], list[str]]:
    errors: list[str] = []
    try:
        agenda, agenda_error = fetch_agenda(token)
    except RuntimeError as exc:
        agenda, agenda_error = [], str(exc)
    if agenda_error:
        errors.append(agenda_error)
    try:
        bookings, booking_error = fetch_bookings(token)
    except RuntimeError as exc:
        bookings, booking_error = [], str(exc)
    if booking_error:
        errors.append(booking_error)
    return agenda, bookings, errors


def merge_items(personal: list[dict], bookings: list[dict], seminars: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen_urls: dict[str, dict] = {}
    for event in personal + bookings:
        merged.append(event)
        if event["url"]:
            seen_urls[event["url"]] = event
    for seminar in seminars:
        existing = seen_urls.get(seminar["url"])
        if existing is not None:
            if seminar["category"] not in existing["category"]:
                existing["category"] = f"{existing['category']} · {seminar['category']}"
            continue
        merged.append(seminar)
    merged.sort(key=lambda event: (event["start"], event["title"].lower()))
    return merged


def assemble_digest() -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    personal: list[dict] = []
    bookings: list[dict] = []
    token = os.environ.get("INDICO_API_TOKEN", "").strip()
    if token:
        personal, bookings, account_errors = fetch_account_items(token)
        errors.extend(account_errors)
    seminars, seminar_errors = collect_events()
    errors.extend(seminar_errors)
    return merge_items(personal, bookings, seminars), errors


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def is_personal(event: dict) -> bool:
    category = event["category"]
    return category.startswith("Your agenda") or category.startswith("Room booking")


def display_label(event: dict) -> str:
    category = event["category"]
    if category == "Your agenda":
        return "Your agenda"
    if category.startswith("Your agenda · "):
        return category.removeprefix("Your agenda · ")
    if category == "Room booking":
        return "Room booking"
    return category


def friendly_place(place: str) -> str:
    if not isinstance(place, str) or not place.strip() or place == "Not announced":
        return "Place not announced"
    return place


def format_when(event: dict) -> str:
    start = event["start"]
    when = f"{WEEKDAYS[start.weekday()]} {start.day} {MONTHS[start.month - 1]} · {start.strftime('%H:%M')}"
    tz = event.get("tz") or ""
    if tz and tz not in {"Europe/Zurich", "UTC"}:
        when = f"{when} ({tz})"
    return when


def format_span() -> str:
    first, last = window_dates()
    if first.year == last.year and first.month == last.month:
        return f"{first.day}–{last.day} {MONTHS[first.month - 1]} {first.year}"
    if first.year == last.year:
        return (
            f"{first.day} {MONTHS[first.month - 1]} – "
            f"{last.day} {MONTHS[last.month - 1]} {first.year}"
        )
    return (
        f"{first.day} {MONTHS[first.month - 1]} {first.year} – "
        f"{last.day} {MONTHS[last.month - 1]} {last.year}"
    )


def split_for_reader(events: list[dict]) -> tuple[list[dict], list[dict]]:
    personal = [event for event in events if is_personal(event)]
    public = [event for event in events if not is_personal(event)]
    return personal, public


def count_label(events: list[dict]) -> str:
    count = len(events)
    if count == 1:
        return "1 event"
    return f"{count} events"


def markdown_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def clock_label(event: dict) -> str:
    _start, _end, label = schedule_span(event)
    return label


def markdown_meta(event: dict) -> str:
    bits: list[str] = []
    label = display_label(event)
    if label != "Your agenda":
        bits.append(markdown_text(label))
    place = friendly_place(event["place"])
    if place != "Place not announced":
        bits.append(markdown_text(place))
    bits.append(f"[Indico]({event['url']})")
    return " · ".join(bits)


def markdown_section(title: str, events: list[dict], empty_text: str) -> list[str]:
    if not events:
        return [f"**{title}** · {empty_text}", ""]
    lines = [f"**{title}** · {count_label(events)}", ""]
    current_day = None
    for event in events:
        day = event["start"].date()
        if day != current_day:
            if current_day is not None:
                lines.append("")
            current_day = day
            lines.append(f"**{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}**")
        lines.append(f"- **{clock_label(event)}** {markdown_text(event['title'])} · {markdown_meta(event)}")
    lines.append("")
    return lines


def build_markdown(events: list[dict], errors: list[str]) -> str:
    personal, public = split_for_reader(events)
    lines = [f"**Next 7 days** · {format_span()}", ""]
    lines.extend(markdown_section("For you", personal, "Nothing on your agenda in the next 7 days."))
    lines.extend(["---", ""])
    lines.extend(markdown_section("General seminars", public, "No public seminars in the next 7 days."))
    if errors:
        lines.extend(["**Could not read**", ""])
        lines.extend(f"- {markdown_text(error)}" for error in errors)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def event_html(event: dict) -> str:
    url = html.escape(event["url"], quote=True)
    return "\n".join(
        [
            f"🕐 <b>{html.escape(format_when(event))}</b>",
            f"<b>{html.escape(event['title'])}</b>",
            f"🏷 {html.escape(display_label(event))}",
            f"📍 {html.escape(friendly_place(event['place']))}",
            f'🔗 <a href="{url}">Open in Indico</a>',
        ]
    )


def telegram_section(emoji: str, title: str, events: list[dict], empty_text: str) -> list[str]:
    heading = f"{emoji} <b>{title}</b>"
    if not events:
        return [f"{heading}\n{html.escape(empty_text)}"]
    blocks = [f"{heading}\n<i>{html.escape(count_label(events))}</i>"]
    blocks.extend(event_html(event) for event in events)
    return blocks


# Omitted from the Telegram text only. The PDF and the Mattermost digest still include them.
TELEGRAM_SKIP_TITLES = (
    "CERN CAF - plan of the day",
    "Cassette Integration - remaining open issues",
    "TPG Stage 1",
    "HGCAL DQM working meeting",
    "Electronics simulation meeting",
    "Online Software Coordination",
    "MAC module testing QC Meeting",
    "Tileboard QC Technical Meeting",
    "Production Coordination Meeting - CM, Engines, Wagons",
)


def hidden_on_telegram(event: dict) -> bool:
    title = event["title"].casefold().strip()
    return any(title.startswith(skip.casefold()) for skip in TELEGRAM_SKIP_TITLES)


def build_telegram_messages(events: list[dict], errors: list[str]) -> list[str]:
    visible = [event for event in events if not hidden_on_telegram(event)]
    personal, public = split_for_reader(visible)
    header = f"📅 <b>Next 7 days</b>\n<i>{html.escape(format_span())}</i>"
    blocks = telegram_section(
        "⭐",
        "For you",
        personal,
        "Nothing on your agenda in the next 7 days.",
    )
    blocks.append("---")
    blocks.extend(
        telegram_section(
            "🎓",
            "General seminars",
            public,
            "No public seminars in the next 7 days.",
        )
    )
    if errors:
        notes = "\n".join(f"• {html.escape(error)}" for error in errors)
        blocks.append(f"⚠️ <b>Sources that could not be read</b>\n{notes}")

    messages: list[str] = []
    current = header
    for block in blocks:
        piece = f"\n\n{block}"
        if len(current) + len(piece) <= TELEGRAM_MESSAGE_LIMIT:
            current += piece
            continue
        messages.append(current)
        current = block
    messages.append(current)
    return messages


WEEKDAYS_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS_FULL = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
PAGE_W = 210
PAGE_H = 297
PAGE_MARGIN = 14


def font_files() -> tuple[str | None, str | None]:
    pairs = (
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        (
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ),
    )
    for regular, bold in pairs:
        if Path(regular).is_file():
            return regular, bold if Path(bold).is_file() else regular
    return None, None


def truncate(pdf: FPDF, text: str, width: float) -> str:
    if pdf.get_string_width(text) <= width:
        return text
    trimmed = text
    while trimmed and pdf.get_string_width(trimmed + "...") > width:
        trimmed = trimmed[:-1]
    return trimmed.rstrip() + "..."


def schedule_span(event: dict) -> tuple[datetime, datetime | None, str]:
    """Return the drawn start, drawn end, and the clock label.

    A missing end stays unlabeled. A span longer than eight hours, or one that
    crosses midnight, is shown as an all-day chip instead of a tall bar.
    """
    start = event["start"]
    end = event.get("end")
    if not isinstance(end, datetime) or end <= start:
        return start, start + timedelta(hours=1), start.strftime("%H:%M")
    if end.date() != start.date() or end - start > timedelta(hours=8):
        return start, None, "All day"
    return start, end, f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def assign_lanes(items: list[dict]) -> tuple[list[dict], int]:
    lane_ends: list[datetime] = []
    placed: list[dict] = []
    for item in sorted(items, key=lambda item: (item["start"], item["end"], item["title"])):
        lane = next((index for index, lane_end in enumerate(lane_ends) if item["start"] >= lane_end), None)
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(item["end"])
        else:
            lane_ends[lane] = item["end"]
        placed.append({**item, "lane": lane})
    return placed, max(len(lane_ends), 1)


def paint_schedule_header(pdf: FPDF, font: str) -> float:
    pdf.set_fill_color(18, 42, 74)
    pdf.rect(0, 0, PAGE_W, 24, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font(font, "B", 16)
    pdf.set_xy(PAGE_MARGIN, 6)
    pdf.cell(110, 8, "CERN schedule")
    pdf.set_font(font, "", 11)
    pdf.set_xy(120, 7)
    pdf.cell(PAGE_W - 120 - PAGE_MARGIN, 7, format_span(), align="R")

    pdf.set_fill_color(255, 236, 204)
    pdf.set_draw_color(176, 112, 32)
    pdf.rect(PAGE_MARGIN, 28, 4, 4, style="DF")
    pdf.set_text_color(28, 36, 48)
    pdf.set_font(font, "", 9)
    pdf.set_xy(PAGE_MARGIN + 6, 27)
    pdf.cell(28, 6, "For you")

    pdf.set_fill_color(222, 234, 246)
    pdf.set_draw_color(0, 83, 150)
    pdf.rect(PAGE_MARGIN + 36, 28, 4, 4, style="DF")
    pdf.set_xy(PAGE_MARGIN + 42, 27)
    pdf.cell(50, 6, "General seminars")
    return 38


def paint_event_bar(pdf: FPDF, font: str, item: dict, x: float, y: float, width: float, height: float) -> None:
    personal = is_personal(item["event"])
    if personal:
        pdf.set_fill_color(255, 236, 204)
        pdf.set_draw_color(176, 112, 32)
    else:
        pdf.set_fill_color(222, 234, 246)
        pdf.set_draw_color(0, 83, 150)
    pdf.set_line_width(0.3)
    pdf.rect(x, y, width, height, style="DF")

    pad = 1.4
    inner = width - pad * 2
    cursor = y + 1.2
    pdf.set_text_color(90, 98, 110)
    pdf.set_font(font, "", 7.5)
    pdf.set_xy(x + pad, cursor)
    pdf.cell(inner, 3.2, truncate(pdf, item["label"], inner))
    cursor += 3.4

    if height >= 7:
        pdf.set_text_color(28, 36, 48)
        pdf.set_font(font, "B", 8)
        pdf.set_xy(x + pad, cursor)
        pdf.cell(inner, 3.6, truncate(pdf, item["event"]["title"], inner))
        cursor += 3.6
    if height >= 14:
        pdf.set_text_color(90, 98, 110)
        pdf.set_font(font, "", 7.5)
        pdf.set_xy(x + pad, cursor)
        pdf.cell(inner, 3.2, truncate(pdf, friendly_place(item["event"]["place"]), inner))


def paint_day(pdf: FPDF, font: str, day: datetime.date, events: list[dict], top: float) -> float:
    timed: list[dict] = []
    allday: list[dict] = []
    for event in events:
        start, end, label = schedule_span(event)
        record = {"event": event, "start": start, "end": end, "label": label, "title": event["title"]}
        if end is None:
            allday.append(record)
        else:
            timed.append(record)

    block_height = 8 + len(allday) * 7
    hour_h = 18
    start_hour = end_hour = 0
    if timed:
        start_hour = min(item["start"].hour for item in timed)
        end_hour = start_hour + 1
        for item in timed:
            end = item["end"]
            mark = end.hour if end.minute == 0 and end.second == 0 else end.hour + 1
            end_hour = max(end_hour, mark)
        hours = max(end_hour - start_hour, 1)
        available = PAGE_H - PAGE_MARGIN - 16
        hour_h = min(18, max(12, (available - 20) / hours))
        block_height += hours * hour_h + 4

    if top + block_height > PAGE_H - 12:
        pdf.add_page()
        top = paint_schedule_header(pdf, font)

    pdf.set_fill_color(18, 42, 74)
    pdf.rect(PAGE_MARGIN, top, PAGE_W - 2 * PAGE_MARGIN, 7, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font(font, "B", 11)
    pdf.set_xy(PAGE_MARGIN + 2, top + 1.2)
    day_title = f"{WEEKDAYS_FULL[day.weekday()]} {day.day} {MONTHS_FULL[day.month - 1]}"
    pdf.cell(PAGE_W - 2 * PAGE_MARGIN - 4, 5, day_title)
    cursor = top + 9

    for item in allday:
        if is_personal(item["event"]):
            pdf.set_fill_color(255, 236, 204)
            pdf.set_draw_color(176, 112, 32)
        else:
            pdf.set_fill_color(222, 234, 246)
            pdf.set_draw_color(0, 83, 150)
        pdf.rect(PAGE_MARGIN, cursor, PAGE_W - 2 * PAGE_MARGIN, 6, style="DF")
        pdf.set_text_color(28, 36, 48)
        pdf.set_font(font, "", 8)
        pdf.set_xy(PAGE_MARGIN + 2, cursor + 1.2)
        chip = f"All day  {item['event']['title']}  ·  {friendly_place(item['event']['place'])}"
        pdf.cell(PAGE_W - 2 * PAGE_MARGIN - 4, 4, truncate(pdf, chip, PAGE_W - 2 * PAGE_MARGIN - 6))
        cursor += 7

    if not timed:
        return cursor + 4

    placed, lane_count = assign_lanes(timed)
    axis_x = PAGE_MARGIN
    axis_w = 14
    grid_x = axis_x + axis_w
    grid_w = PAGE_W - PAGE_MARGIN - grid_x
    origin = datetime.combine(day, datetime.min.time()) + timedelta(hours=start_hour)
    grid_top = cursor + 1
    grid_h = (end_hour - start_hour) * hour_h

    pdf.set_draw_color(220, 224, 230)
    pdf.set_line_width(0.2)
    pdf.set_text_color(90, 98, 110)
    pdf.set_font(font, "", 8)
    for hour in range(start_hour, end_hour + 1):
        y = grid_top + (hour - start_hour) * hour_h
        pdf.line(grid_x, y, grid_x + grid_w, y)
        pdf.set_xy(axis_x, y - 2)
        pdf.cell(axis_w - 1, 4, f"{hour:02d}:00", align="R")
    pdf.set_draw_color(180, 186, 196)
    pdf.line(grid_x, grid_top, grid_x, grid_top + grid_h)

    lane_gap = 1.2
    lane_w = (grid_w - 2 - lane_gap * (lane_count - 1)) / lane_count
    for item in placed:
        y0 = grid_top + (item["start"] - origin).total_seconds() / 3600 * hour_h
        y1 = grid_top + (item["end"] - origin).total_seconds() / 3600 * hour_h
        y1 = max(y1, y0 + 8)
        y1 = min(y1, grid_top + grid_h - 0.4)
        height = min(max(y1 - y0 - 0.8, 7), grid_top + grid_h - y0 - 0.6)
        x = grid_x + 1 + item["lane"] * (lane_w + lane_gap)
        paint_event_bar(pdf, font, item, x, y0 + 0.4, lane_w, height)

    return grid_top + grid_h + 6


def build_schedule_pdf(events: list[dict]) -> bytes:
    regular, bold = font_files()
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=False)
    font = "Helvetica"
    if regular:
        pdf.add_font("Schedule", "", regular)
        pdf.add_font("Schedule", "B", bold or regular)
        font = "Schedule"

    pdf.add_page()
    cursor = paint_schedule_header(pdf, font)
    if not events:
        pdf.set_text_color(28, 36, 48)
        pdf.set_font(font, "", 12)
        pdf.set_xy(PAGE_MARGIN, cursor + 4)
        pdf.cell(0, 8, "No events in the next 7 days.")
        return bytes(pdf.output())

    by_day: dict[datetime.date, list[dict]] = {}
    for event in events:
        by_day.setdefault(event["start"].date(), []).append(event)
    for day in sorted(by_day):
        cursor = paint_day(pdf, font, day, by_day[day], cursor)
    return bytes(pdf.output())


def send_telegram(messages: list[str], token: str, chat_id: str) -> None:
    endpoint = TELEGRAM_SEND.format(token=token)
    for message in messages:
        try:
            response = requests.post(
                endpoint,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            raise RuntimeError("could not reach api.telegram.org") from None

        if response.ok:
            continue

        description = "Telegram rejected the message"
        try:
            description = response.json().get("description") or description
        except ValueError:
            pass
        raise RuntimeError(description)


def send_schedule_pdf(events: list[dict], token: str, chat_id: str) -> None:
    try:
        response = requests.post(
            TELEGRAM_DOCUMENT.format(token=token),
            data={"chat_id": chat_id, "caption": "Hour-by-hour schedule"},
            files={"document": ("cern-schedule.pdf", build_schedule_pdf(events), "application/pdf")},
            timeout=40,
        )
    except requests.RequestException:
        raise RuntimeError("could not reach api.telegram.org") from None

    if response.ok:
        return

    description = "Telegram rejected the schedule"
    try:
        description = response.json().get("description") or description
    except ValueError:
        pass
    raise RuntimeError(description)


def send_webhook(text: str, webhook: str) -> None:
    try:
        response = requests.post(
            webhook,
            json={"text": text},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "indico-notifier"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"webhook delivery failed ({exc})") from None


def print_chat_ids() -> None:
    """List chats that have recently messaged the bot."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env first.", file=sys.stderr)
        sys.exit(1)

    try:
        response = requests.get(
            TELEGRAM_UPDATES.format(token=token),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        print("Could not reach api.telegram.org.", file=sys.stderr)
        sys.exit(1)

    try:
        payload = response.json()
    except ValueError:
        print("Telegram returned a response that was not JSON.", file=sys.stderr)
        sys.exit(1)

    if not payload.get("ok"):
        description = payload.get("description") or "Telegram rejected the request"
        print(description, file=sys.stderr)
        sys.exit(1)

    seen: set[str] = set()
    for update in payload.get("result") or []:
        message = update.get("message") or update.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or str(chat_id) in seen:
            continue
        seen.add(str(chat_id))
        label = chat.get("title") or chat.get("username") or chat.get("first_name") or "chat"
        print(f"{chat_id}\t{chat.get('type', 'unknown')}\t{label}")

    if not seen:
        print(
            "No chats yet. Open the bot in Telegram, send /start, then run this again.",
            file=sys.stderr,
        )
        sys.exit(1)


def deliver(events: list[dict], errors: list[str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    webhook = os.environ.get("WEBHOOK_URL", "").strip()

    if bool(token) != bool(chat_id):
        print("Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", file=sys.stderr)
        sys.exit(1)

    if not token and not webhook:
        print(build_markdown(events, errors), end="")
        return

    if token:
        try:
            send_telegram(build_telegram_messages(events, errors), token, chat_id)
            send_schedule_pdf(events, token, chat_id)
        except RuntimeError as exc:
            print(f"Telegram delivery failed: {exc}", file=sys.stderr)
            sys.exit(1)
        print("Telegram notification sent.", file=sys.stderr)

    if webhook:
        try:
            send_webhook(build_markdown(events, errors), webhook)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        print("Webhook notification sent.", file=sys.stderr)


def main() -> None:
    events, errors = assemble_digest()
    deliver(events, errors)


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    if "--chat-id" in sys.argv:
        print_chat_ids()
    else:
        main()
