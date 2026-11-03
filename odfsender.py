import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.table import Table

console = Console()

APP_VERSION = "1.3"
TOKEN_HEADER = "x-ApiKey"
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_FILE = APP_DIR / "config.json"
VERSIONS_FILE = APP_DIR / "versions.json"
HTTP_TIMEOUT_SECONDS = 30
log = logging.getLogger("odfsender")
# Without a handler, Python's fallback prints warnings to stderr, which
# would draw over the full-screen UI whenever the log file can't be opened.
log.addHandler(logging.NullHandler())
AUTO_MODES = ("auto", "auto-as-is")
CONFIG: dict[str, Any] = {}


class QuietFileHandler(logging.FileHandler):
    """
    File handler that opens its file on the first line and never lets a
    logging problem (unwritable folder, full disk) reach the UI.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:
            pass

    def handleError(self, record: logging.LogRecord) -> None:
        pass


session_logged = False


def setup_logging() -> None:
    """
    Prepares a log file next to the app for this run, named
    sender_yyyy-mm-dd_hh-MM-ss.log.

    Nothing is written until log_session_start() is called, which
    happens when something is actually sent or written. Opening the
    app, looking around and closing it leaves no log file behind.
    If the log cannot be written, the sender keeps working without it.
    """

    log.setLevel(logging.INFO)

    log_file = APP_DIR / (
        "sender_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
    )

    handler = QuietFileHandler(log_file, encoding="utf-8", delay=True)

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )
    log.addHandler(handler)


def log_session_start() -> None:
    """Writes the "started" line once, before the first logged action."""

    global session_logged

    if session_logged:
        return

    session_logged = True

    log.info(
        "ODF Sender v%s started (VERSION=%r)",
        APP_VERSION,
        CONFIG["VERSION"],
    )


def pause_before_exit() -> None:
    try:
        input("Press Enter to close...")
    except EOFError:
        pass

ODF_BODY_RE = re.compile(rb"<OdfBody\b[^>]*>")
VERSION_RE = re.compile(rb'(\bVersion\s*=\s*)(["\'])(.*?)\2')
DATE_RE = re.compile(
    rb'\bDate\s*=\s*(["\'])\s*(\d{4})-?(\d{2})-?(\d{2})\s*\1'
)
# HHMMSS followed by any number of fractional-second digits (HHMMSSm,
# HHMMSSmm, HHMMSSmmm, ...). Colons and a "." or "," before the
# fraction are tolerated too.
TIME_RE = re.compile(
    rb'\bTime\s*=\s*(["\'])\s*(\d{2}):?(\d{2}):?(\d{2})(?:[.,]?(\d*))?\s*\1'
)


# ============================================================
# Models
# ============================================================

@dataclass
class FilePlan:
    file: Path
    current_version: int | None
    remembered_version: int | None
    version_to_send: int | None
    error: str | None = None
    document_time: str | None = None


# ============================================================
# XML
# ============================================================

def get_xml_version(xml: bytes) -> int:
    body_match = ODF_BODY_RE.search(xml)

    if not body_match:
        raise ValueError("Could not find <OdfBody ...> element")

    version_match = VERSION_RE.search(body_match.group(0))

    if not version_match:
        raise ValueError("Could not find Version attribute on <OdfBody>")

    try:
        return int(version_match.group(3))
    except ValueError:
        raise ValueError(
            f"Invalid Version value: {version_match.group(3)!r}"
        )


def get_xml_timestamp(xml: bytes) -> str | None:
    """
    Returns "YYYY-MM-DD HH:MM:SS.ffffff" from the Date and Time attributes
    of <OdfBody>, or None if either is missing or malformed.

    Time is HHMMSS plus any number of fractional-second digits, so
    "062046", "06204639", "062046394" are 06:20:46.000, .390 and .394.
    The fraction is padded/truncated to 6 digits, so the result always
    sorts chronologically as a plain string.
    """

    body_match = ODF_BODY_RE.search(xml)

    if not body_match:
        return None

    tag = body_match.group(0)
    date_match = DATE_RE.search(tag)
    time_match = TIME_RE.search(tag)

    if not date_match or not time_match:
        return None

    year, month, day = (g.decode("ascii") for g in date_match.groups()[1:])
    hour, minute, second, fraction = (
        g.decode("ascii") for g in time_match.groups()[1:]
    )

    if not (
        1 <= int(month) <= 12
        and 1 <= int(day) <= 31
        and int(hour) <= 24
        and int(minute) <= 59
        and int(second) <= 60
    ):
        return None

    fraction = fraction.ljust(6, "0")[:6]

    return f"{year}-{month}-{day} {hour}:{minute}:{second}.{fraction}"


def set_xml_version(xml: bytes, version: int) -> bytes:
    """
    Changes only Version="..." inside the opening <OdfBody> tag.
    Everything else remains byte-for-byte unchanged.
    """

    body_match = ODF_BODY_RE.search(xml)

    if not body_match:
        raise ValueError("Could not find <OdfBody ...> element")

    original_tag = body_match.group(0)

    def replace_version(match: re.Match[bytes]) -> bytes:
        prefix = match.group(1)
        quote = match.group(2)

        return (
            prefix
            + quote
            + str(version).encode("ascii")
            + quote
        )

    new_tag, count = VERSION_RE.subn(
        replace_version,
        original_tag,
        count=1,
    )

    if count != 1:
        raise ValueError(
            "Could not find Version attribute on <OdfBody>"
        )

    return (
        xml[:body_match.start()]
        + new_tag
        + xml[body_match.end():]
    )


# ============================================================
# Versions
# ============================================================

def load_versions() -> dict[str, int]:
    if not VERSIONS_FILE.exists():
        return {}

    with VERSIONS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    return {
        str(filename): int(version)
        for filename, version in data.items()
    }


def save_versions(versions: dict[str, int]) -> None:
    temp_file = VERSIONS_FILE.with_suffix(".json.tmp")

    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(
            versions,
            f,
            indent=2,
            sort_keys=True,
        )
        f.write("\n")

    temp_file.replace(VERSIONS_FILE)


def load_config() -> bool:
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text("""{
  "URLS": [
    "https://xxxxxx/api/v1/ingest"
  ],
  "TOKEN": "",
  "DIRECTORY": "./",
  "VERSION": "auto"
}
""", encoding="utf-8")
                                   
        except OSError as e:
            console.print(
                f"[red]Could not create {CONFIG_FILE.name}: {e}[/red]"
            )
            return False

        try:
            os.startfile(str(CONFIG_FILE))
        except OSError as e:
            console.print(
                f"[yellow]Could not open {CONFIG_FILE.name}: {e}[/yellow]"
            )

        console.print(f"[red]Configuration file {CONFIG_FILE.name} did not exist and was created.[/red]")
        console.print(
            f"[yellow]Created {CONFIG_FILE.name}. Fill it with URLS, TOKEN, "
            "DIRECTORY, and VERSION settings, then run the sender again.[/yellow]"
        )
        console.print()
        console.print()
        console.print(
            '[green]Note: You can provide a specific version number such as 5, use "auto" to automatically increment the version before sending each XML (first send starts at 1), "auto-as-is" to do the same but start with the version inside the file, or null to send XML exactly as-is.[/green]'
        )
        console.print()
        console.print()
        return False

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("The top-level JSON value must be an object.")

        required = ("URLS", "TOKEN", "DIRECTORY", "VERSION")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(
                "Missing settings: " + ", ".join(missing)
            )

        if (
            not isinstance(data["URLS"], list)
            or not data["URLS"]
            or any(
                not isinstance(url, str) or not url.strip()
                for url in data["URLS"]
            )
        ):
            raise ValueError("URLS must be a non-empty list of strings.")

        if not isinstance(data["TOKEN"], str) or not data["TOKEN"].strip():
            raise ValueError("TOKEN must be a non-empty string.")

        if not isinstance(data["DIRECTORY"], str) or not data["DIRECTORY"].strip():
            raise ValueError("DIRECTORY must be a non-empty string.")

        version = data["VERSION"]
        if not (
            version is None
            or version in AUTO_MODES
            or (isinstance(version, int) and not isinstance(version, bool))
        ):
            raise ValueError(
                'VERSION must be null, an integer, "auto", or "auto-as-is".'
            )

    except (OSError, json.JSONDecodeError, ValueError) as e:
        console.print(
            f"[red]Invalid {CONFIG_FILE.name}: {e}[/red]"
        )
        return False

    CONFIG.update(data)
    return True


# ============================================================
# Analysis
# ============================================================

def get_xml_files() -> list[Path]:
    directory = Path(CONFIG["DIRECTORY"])
    if not directory.is_absolute():
        directory = APP_DIR / directory

    if not directory.is_dir():
        return []

    # Name order here; analyze_files() re-sorts oldest first by the
    # Date/Time inside each XML.
    return sorted(
        file
        for file in directory.iterdir()
        if file.is_file()
        and file.suffix.lower() == ".xml"
    )


def is_auto() -> bool:
    return CONFIG["VERSION"] in AUTO_MODES


def analyze_files() -> list[FilePlan]:
    """
    Reads and validates all XML files beforehand.

    This is used both for:
      - dynamic menu counts
      - preview
      - send planning

    For files that have no remembered version yet:
      - "auto":       start with 1
      - "auto-as-is": start with the version inside the file
    """

    files = get_xml_files()

    try:
        versions = (
            load_versions()
            if CONFIG["VERSION"] is not None
            else {}
        )
    except Exception as e:
        return [
            FilePlan(
                file=file,
                current_version=None,
                remembered_version=None,
                version_to_send=None,
                error=f"Could not load versions.json: {e}",
            )
            for file in files
        ]

    plans: list[FilePlan] = []

    for file in files:
        try:
            xml = file.read_bytes()
            current_version = get_xml_version(xml)

            remembered_version = versions.get(file.name)

            if CONFIG["VERSION"] is None:
                version_to_send = None

            elif is_auto():
                if remembered_version is not None:
                    version_to_send = remembered_version + 1
                elif CONFIG["VERSION"] == "auto-as-is":
                    version_to_send = current_version
                else:
                    version_to_send = 1

            else:
                version_to_send = int(CONFIG["VERSION"])

            plans.append(
                FilePlan(
                    file=file,
                    current_version=current_version,
                    remembered_version=remembered_version,
                    version_to_send=version_to_send,
                    document_time=get_xml_timestamp(xml),
                )
            )

        except Exception as e:
            plans.append(
                FilePlan(
                    file=file,
                    current_version=None,
                    remembered_version=None,
                    version_to_send=None,
                    error=str(e),
                )
            )

    # Oldest document first, using Date/Time from inside the XML.
    # Files without a usable timestamp go last, ordered by name.
    plans.sort(
        key=lambda plan: (
            plan.document_time is None,
            plan.document_time or "",
            plan.file.name,
        )
    )

    return plans


# ============================================================
# HTTP
# ============================================================

def send_xml(url: str, xml: bytes) -> int:
    request = urllib.request.Request(
        url=url,
        data=xml,
        method="POST",
        headers={
            TOKEN_HEADER: CONFIG["TOKEN"],
            "Content-Type": "application/xml",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:

            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"HTTP {response.status}"
                )

            return response.status

    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"HTTP {e.code}: {e.reason}"
        ) from e

    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Connection failed: {e.reason}"
        ) from e


# ============================================================
# UI
# ============================================================

@contextmanager
def use_console(target: Console) -> Iterator[Console]:
    """
    Points the module-level console at another Console for the duration
    of the block, so the view functions can print into the full-screen
    UI's content pane instead of straight to the terminal.
    """

    global console

    previous = console
    console = target

    try:
        yield target
    finally:
        console = previous


def view_config() -> None:
    table = Table(
        title="Configuration",
        show_header=False,
    )

    table.add_column("Setting", style="bold")
    table.add_column("Value")

    table.add_row(
        "Directory",
        str(CONFIG["DIRECTORY"]),
    )

    table.add_row(
        "Version",
        repr(CONFIG["VERSION"]),
    )

    table.add_row(
        "Token header",
        TOKEN_HEADER,
    )

    table.add_row(
        "Token",
        mask_token(CONFIG["TOKEN"]),
    )

    table.add_row(
        "URLs",
        "\n".join(CONFIG["URLS"]),
    )

    console.print()
    console.print(table)
    console.print()


def show_help() -> None:
    console.print()
    console.print("[bold]Keys[/bold]")
    console.print(
        "  [bold]Left/Right + Enter[/bold]  Pick a menu item (or click it), "
        "or press its underlined letter."
    )
    console.print(
        "  [bold]Up/Down, PgUp/PgDn, Home/End[/bold]  Scroll the view "
        "(the mouse wheel works too)."
    )
    console.print(
        "  [bold]Enter or y / n or Esc[/bold]  Confirm or cancel a question "
        "in the bottom line."
    )
    console.print(
        "  [bold]Ctrl+C[/bold]  While sending: stop after the current file. "
        "Otherwise: quit."
    )
    console.print(
        "  [dim]Hold Shift while dragging to select text with the mouse.[/dim]"
    )
    console.print()
    console.print("[bold]Version configuration[/bold]")
    console.print(
        'Set "VERSION" in config.json to one of the following:'
    )
    console.print(
        '  [bold]"auto"[/bold]  Increment each file version before sending. '
        "Uses its remembered version from versions.json. Successful sends "
        "are remembered."
    )
    console.print(
        "         Files with no remembered version start with 1 "
        "(then 2, 3, ... on the next sends)."
    )
    console.print(
        '  [bold]"auto-as-is"[/bold]  Same as "auto", but files with no '
        "remembered version start with the version inside the file."
    )
    console.print(
        '  [bold]An integer[/bold]  Send every file with exactly that version '
        "number. Successful sends are remembered."
    )
    console.print(
        '  [bold]null[/bold]  Send each XML unchanged and do not update '
        "remembered versions."
    )
    console.print()


def mask_token(token: str) -> str:
    if not token:
        return "<empty>"

    if len(token) <= 4:
        return "*" * len(token)

    return (
        token[:2]
        + "*" * (len(token) - 4)
        + token[-2:]
    )


def preview(plans: list[FilePlan]) -> None:
    table = Table(
        title="Send Preview (oldest first)",
    )

    table.add_column(
        "#",
        justify="right",
    )
    table.add_column("File")
    table.add_column("Document time")
    table.add_column(
        "Current",
        justify="right",
    )
    table.add_column(
        "Remembered",
        justify="right",
    )
    table.add_column(
        "Will send",
        justify="right",
    )
    table.add_column("Status")

    # Numbered in send order. Files with errors are not sent, so they
    # get no number.
    position = 0

    for plan in plans:
        if plan.error:
            table.add_row(
                "-",
                plan.file.name,
                "-",
                "-",
                "-",
                "-",
                f"[red]{plan.error}[/red]",
            )

            continue

        position += 1

        version_to_send = (
            str(plan.version_to_send)
            if plan.version_to_send is not None
            else "unchanged"
        )

        table.add_row(
            str(position),
            plan.file.name,
            plan.document_time or "-",
            str(plan.current_version),
            (
                str(plan.remembered_version)
                if plan.remembered_version is not None
                else "-"
            ),
            version_to_send,
            "[green]Ready[/green]",
        )

    console.print()
    console.print(table)

    ready = sum(
        1 for plan in plans
        if plan.error is None
    )

    errors = len(plans) - ready

    console.print()
    console.print(
        f"[bold]{ready}[/bold] file(s) ready, "
        f"[bold red]{errors}[/bold red] error(s)."
    )

    console.print(
        "[dim]Nothing was sent or modified.[/dim]"
    )
    console.print()


def show_send_order(plans: list[FilePlan]) -> None:
    """
    Lists the files in the exact order they will be sent, with the
    version each one will go out with. Shown right before sending.
    """

    valid_plans = [
        plan
        for plan in plans
        if plan.error is None
    ]

    skipped = [
        plan
        for plan in plans
        if plan.error is not None
    ]

    if not valid_plans:
        console.print(
            "[yellow]There are no valid files to send.[/yellow]"
        )
        return

    table = Table(
        title="Send Order (oldest first)",
    )

    table.add_column(
        "#",
        justify="right",
    )
    table.add_column("File")
    table.add_column("Document time")
    table.add_column(
        "Version",
        justify="right",
    )

    for index, plan in enumerate(valid_plans, start=1):
        if plan.version_to_send is None:
            version = f"{plan.current_version} (unchanged)"
        elif plan.version_to_send == plan.current_version:
            version = f"{plan.version_to_send} (same)"
        else:
            version = (
                f"{plan.current_version} -> {plan.version_to_send}"
            )

        if is_auto() and plan.remembered_version is None:
            version += " (first send)"

        if (
            plan.version_to_send is not None
            and plan.version_to_send < plan.current_version
        ):
            version = f"[yellow]{version} (lower!)[/yellow]"

        table.add_row(
            str(index),
            plan.file.name,
            plan.document_time or "[yellow]- (no Date/Time)[/yellow]",
            version,
        )

    console.print()
    console.print(table)
    console.print()

    lower = sum(
        1
        for plan in valid_plans
        if plan.version_to_send is not None
        and plan.version_to_send < plan.current_version
    )

    if lower:
        console.print(
            f"[yellow]{lower} file(s) will be sent with a version lower "
            "than the one inside the file. The receiver may reject "
            "them if it already has a higher version.[/yellow]"
        )
        console.print()

    console.print(
        f"[bold]{len(valid_plans)}[/bold] file(s) to "
        f"[bold]{len(CONFIG['URLS'])}[/bold] endpoint(s):"
    )

    for url in CONFIG["URLS"]:
        console.print(f"  {url}")

    if skipped:
        console.print()
        console.print(
            f"[red]{len(skipped)} file(s) will be skipped:[/red]"
        )

        for plan in skipped:
            console.print(
                f"  [red]{plan.file.name}: {plan.error}[/red]"
            )

    console.print()


# ============================================================
# Sending
# ============================================================

def send_description(count: int) -> str:
    description = f"Send {count} files"

    if CONFIG["VERSION"] is None:
        description += " without changing versions"
    elif is_auto():
        description += " and increment the version"
    else:
        description += f" with Version {CONFIG['VERSION']}"

    return description


def send_files(
    plans: list[FilePlan],
    should_stop: Callable[[], bool] = lambda: False,
    on_status: Callable[[str], None] = lambda text: None,
) -> tuple[int, int]:
    """
    Sends every valid plan, oldest first, and returns
    (fully_sent, failed).

    should_stop is checked between files, never in the middle of one, so
    a file always goes to all endpoints or is not started at all.
    on_status receives a short "what is happening now" text.
    """

    valid_plans = [
        plan
        for plan in plans
        if plan.error is None
    ]

    log_session_start()

    versions = (
        load_versions()
        if CONFIG["VERSION"] is not None
        else {}
    )

    log.info(
        "Send started: %d file(s) to %d endpoint(s), VERSION=%r",
        len(valid_plans),
        len(CONFIG["URLS"]),
        CONFIG["VERSION"],
    )

    for url in CONFIG["URLS"]:
        log.info("Endpoint: %s", url)

    for plan in plans:
        if plan.error is not None:
            log.warning("Skipped %s: %s", plan.file.name, plan.error)

    fully_sent = 0
    failed = 0

    console.print()

    for plan in valid_plans:
        if should_stop():
            log.warning(
                "Send stopped by user after %d of %d file(s)",
                fully_sent + failed,
                len(valid_plans),
            )

            console.print(
                "[yellow]Stopped by user. The remaining files "
                "were not sent.[/yellow]"
            )

            console.print()

            break

        console.print(
            f"[bold]{plan.file.name}[/bold]"
        )

        try:
            original_xml = plan.file.read_bytes()

            if plan.version_to_send is None:
                xml_to_send = original_xml

                console.print(
                    "  Version: unchanged"
                )

                log.info(
                    "%s: sending unchanged (version %s)",
                    plan.file.name,
                    plan.current_version,
                )

            else:
                xml_to_send = set_xml_version(
                    original_xml,
                    plan.version_to_send,
                )

                console.print(
                    f"  Version: {plan.version_to_send}"
                )

                log.info(
                    "%s: sending version %s (file has %s, remembered %s)",
                    plan.file.name,
                    plan.version_to_send,
                    plan.current_version,
                    plan.remembered_version,
                )

            all_succeeded = True

            for url in CONFIG["URLS"]:
                try:
                    on_status(f"Sending {plan.file.name} to {url}")

                    status = send_xml(
                        url,
                        xml_to_send,
                    )

                    console.print(
                        f"  [green]✓[/green] {url}"
                    )

                    log.info(
                        "%s: OK %s -> HTTP %s",
                        plan.file.name,
                        url,
                        status,
                    )

                except Exception as e:
                    all_succeeded = False

                    console.print(
                        f"  [red]✗[/red] {url}"
                    )

                    console.print(
                        f"    [red]{e}[/red]"
                    )

                    log.error(
                        "%s: FAILED %s -> %s",
                        plan.file.name,
                        url,
                        e,
                    )

            if all_succeeded:
                fully_sent += 1
            else:
                failed += 1

            if (
                all_succeeded
                and plan.version_to_send is not None
            ):
                versions[plan.file.name] = (
                    plan.version_to_send
                )

                save_versions(versions)

                console.print(
                    f"  [dim]Remembered version "
                    f"{plan.version_to_send}[/dim]"
                )

                log.info(
                    "%s: remembered version %s",
                    plan.file.name,
                    plan.version_to_send,
                )

            elif not all_succeeded:
                log.warning(
                    "%s: not remembered (not every endpoint accepted it)",
                    plan.file.name,
                )

        except Exception as e:
            failed += 1

            console.print(
                f"  [red]ERROR: {e}[/red]"
            )

            log.exception("%s: ERROR %s", plan.file.name, e)

        console.print()

    log.info(
        "Send finished: %d of %d file(s) fully sent, %d failed",
        fully_sent,
        len(valid_plans),
        failed,
    )

    console.print(
        f"[bold]Done:[/bold] {fully_sent} of {len(valid_plans)} file(s) "
        f"fully sent, {failed} failed."
    )

    console.print()

    return fully_sent, failed


# ============================================================
# Write remembered versions into XML files
# ============================================================

def show_pending_version_writes() -> list[tuple[Path, int, int]]:
    """
    Prints which files would get their remembered version written into
    them and returns them as (file, current, remembered). Returns an
    empty list when there is nothing to write.
    """

    try:
        versions = load_versions()
    except Exception as e:
        console.print(
            f"[red]Could not load versions.json: {e}[/red]"
        )
        return []

    if not versions:
        console.print(
            "[yellow]No remembered versions found.[/yellow]"
        )
        return []

    files = get_xml_files()

    candidates: list[
        tuple[Path, int, int]
    ] = []

    errors: list[
        tuple[Path, str]
    ] = []

    for file in files:
        remembered = versions.get(file.name)

        if remembered is None:
            continue

        try:
            xml = file.read_bytes()
            current = get_xml_version(xml)

            if current != remembered:
                candidates.append(
                    (
                        file,
                        current,
                        remembered,
                    )
                )

        except Exception as e:
            errors.append(
                (
                    file,
                    str(e),
                )
            )

    if errors:
        console.print()

        for file, error in errors:
            console.print(
                f"[red]{file.name}: {error}[/red]"
            )

    if not candidates:
        console.print(
            "[green]All XML files already have "
            "their remembered versions.[/green]"
        )
        return []

    table = Table(
        title="Versions to write",
    )

    table.add_column("File")
    table.add_column(
        "Current",
        justify="right",
    )
    table.add_column(
        "Write",
        justify="right",
    )

    for file, current, remembered in candidates:
        table.add_row(
            file.name,
            str(current),
            str(remembered),
        )

    console.print()
    console.print(table)
    console.print()

    return candidates


def write_versions_to_files(
    candidates: list[tuple[Path, int, int]],
) -> None:
    log_session_start()

    log.info(
        "Writing remembered versions into %d file(s)",
        len(candidates),
    )

    console.print()

    for file, current, remembered in candidates:
        try:
            xml = file.read_bytes()

            updated_xml = set_xml_version(
                xml,
                remembered,
            )

            file.write_bytes(updated_xml)

            console.print(
                f"[green]✓[/green] "
                f"{file.name}: "
                f"{current} → {remembered}"
            )

            log.info(
                "%s: wrote version %s (was %s)",
                file.name,
                remembered,
                current,
            )

        except Exception as e:
            console.print(
                f"[red]✗ {file.name}: {e}[/red]"
            )

            log.error("%s: write FAILED -> %s", file.name, e)


# ============================================================
# Full-screen UI
# ============================================================

UI_STYLE = Style.from_dict(
    {
        "title": "bg:#ffd54a fg:#1f2937 bold",
        "menu": "",
        "menu.selected": "bg:#005fb8 fg:#ffffff",
        "menu.disabled": "fg:#6b7280",
        "menu.key": "underline",
        "info": "fg:#9ca3af",
        "info.error": "fg:#f87171",
        "rule": "fg:#4b5563",
        "status": "fg:#9ca3af",
        "status.busy": "fg:#ffd54a",
        "status.confirm": "bg:#ffd54a fg:#1f2937 bold",
    }
)

# Rows the layout uses besides the content pane:
# title, menu, info, rule, rule, status.
FIXED_ROWS = 6

# Menu order. The hotkey is the first letter of the item's label.
MENU_ACTIONS = ("preview", "send", "write", "config", "help", "quit")


@dataclass
class Confirm:
    question: str
    on_yes: Callable[[], None]
    on_no: Callable[[], None]


class LiveWriter:
    """File-like target that streams Rich output into the content pane."""

    encoding = "utf-8"

    def __init__(self, ui: "SenderUI") -> None:
        self.ui = ui

    def write(self, text: str) -> int:
        self.ui.append_text(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


class ContentControl(FormattedTextControl):
    """The scrollable content pane; the mouse wheel scrolls it."""

    def __init__(
        self,
        text: Callable[[], Any],
        on_scroll: Callable[[int], None],
    ) -> None:
        super().__init__(text, show_cursor=False)
        self.on_scroll = on_scroll

    def mouse_handler(self, mouse_event: MouseEvent) -> Any:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.on_scroll(-3)
            return None

        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.on_scroll(3)
            return None

        return NotImplemented


def terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((100, 30))
    return size.columns, size.lines


def render_text(draw: Callable[[], None], width: int) -> str:
    """Runs a view function and returns what it printed, as ANSI text."""

    buffer = io.StringIO()

    target = Console(
        file=buffer,
        force_terminal=True,
        color_system="256",
        width=width,
        height=50,
        legacy_windows=False,
    )

    with use_console(target):
        draw()

    return buffer.getvalue()


class SenderUI:
    """
    Full-screen layout:

        title bar with the current section
        menu bar
        settings / counts line
        ----
        content pane (scrolls)
        ----
        status line (hints, questions, progress)

    Views are the same functions as before; their output is rendered to
    ANSI and shown in the content pane. Sending runs in a worker thread
    that streams into the pane.
    """

    def __init__(self) -> None:
        self.plans: list[FilePlan] = analyze_files()
        self.section: str | None = None
        self.selected = 0

        self.text = ""
        self.version = 0
        self.parsed_version = -1
        self.lines: list[list[tuple[str, str]]] = []
        self.scroll = 0
        self.follow = False
        self.draw: Callable[[], None] | None = None
        self.render_width = 0

        self.busy = False
        self.status = ""
        self.notice = ""
        self.confirm: Confirm | None = None
        self.stop_requested = threading.Event()

        self.content_window = Window(
            ContentControl(self.content_fragments, self.scroll_by),
            wrap_lines=False,
        )

        self.app: Application[None] = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(
                            FormattedTextControl(self.title_fragments),
                            height=1,
                            style="class:title",
                        ),
                        Window(
                            FormattedTextControl(self.menu_fragments),
                            height=1,
                        ),
                        Window(
                            FormattedTextControl(self.info_fragments),
                            height=1,
                            style="class:info",
                        ),
                        Window(height=1, char="─", style="class:rule"),
                        self.content_window,
                        Window(height=1, char="─", style="class:rule"),
                        Window(
                            FormattedTextControl(self.status_fragments),
                            height=1,
                            style=self.status_style,
                        ),
                    ]
                )
            ),
            key_bindings=self.build_key_bindings(),
            style=UI_STYLE,
            full_screen=True,
            mouse_support=True,
        )

        self.run_action("preview")

    def run(self) -> None:
        self.app.run()

    # --------------------------------------------------------
    # Content
    # --------------------------------------------------------

    def width(self) -> int:
        # One column short of the terminal so a full-width table row
        # never touches the last column.
        return max(40, terminal_size()[0] - 1)

    def view_height(self) -> int:
        info = self.content_window.render_info

        if info is not None:
            return max(1, info.window_height)

        return max(1, terminal_size()[1] - FIXED_ROWS)

    def max_scroll(self) -> int:
        return max(0, len(self.lines) - self.view_height())

    def show(self, section: str, draw: Callable[[], None]) -> None:
        """Replaces the content pane with a freshly rendered view."""

        self.section = section
        self.draw = draw
        self.scroll = 0
        self.follow = False
        self.redraw()

    def redraw(self) -> None:
        if self.draw is None:
            return

        self.render_width = self.width()
        self.text = render_text(self.draw, self.render_width)
        self.version += 1

    def append(self, draw: Callable[[], None]) -> None:
        """Adds output below the current view and scrolls to it."""

        # Output that did something (wrote files) must not be replayed on
        # resize, so the view is frozen from here on.
        self.draw = None
        self.text += render_text(draw, self.render_width or self.width())
        self.version += 1
        self.follow = True

    def append_text(self, text: str) -> None:
        """Called from the worker thread with raw Rich output."""

        self.text += text
        self.version += 1
        self.app.invalidate()

    def parse(self) -> None:
        lines = [
            [(style, part) for style, part, *_ in line]
            for line in split_lines(to_formatted_text(ANSI(self.text)))
        ]

        if self.text.endswith("\n") and lines and not any(
            part for _, part in lines[-1]
        ):
            lines.pop()

        self.lines = lines
        self.parsed_version = self.version

    def scroll_by(self, amount: int) -> None:
        self.follow = False
        self.scroll = max(0, min(self.scroll + amount, self.max_scroll()))

    def content_fragments(self) -> list[tuple[str, str]]:
        if (
            self.draw is not None
            and not self.busy
            and self.width() != self.render_width
        ):
            self.redraw()

        if self.parsed_version != self.version:
            self.parse()

        height = self.view_height()
        max_scroll = self.max_scroll()

        if self.follow:
            self.scroll = max_scroll

        self.scroll = max(0, min(self.scroll, max_scroll))

        fragments: list[tuple[str, str]] = []

        for index, line in enumerate(
            self.lines[self.scroll:self.scroll + height]
        ):
            if index:
                fragments.append(("", "\n"))

            fragments.extend(line)

        return fragments

    # --------------------------------------------------------
    # Bars
    # --------------------------------------------------------

    def menu_labels(self) -> dict[str, str]:
        ready = sum(1 for plan in self.plans if plan.error is None)

        return {
            "preview": "Preview",
            "send": f"Send {ready} files",
            "write": "Write versions",
            "config": "Config",
            "help": "Help",
            "quit": "Quit",
        }

    def title_fragments(self) -> list[tuple[str, str]]:
        title = f" ODF Sender v{APP_VERSION}"

        if self.section:
            title += f"  ›  {self.section}"

        return [("", title)]

    def menu_fragments(self) -> list[Any]:
        locked = self.busy or self.confirm is not None
        labels = self.menu_labels()
        fragments: list[Any] = []

        for index, action in enumerate(MENU_ACTIONS):
            label = labels[action]

            if locked:
                style = "class:menu.disabled"
            elif index == self.selected:
                style = "class:menu.selected"
            else:
                style = "class:menu"

            def click(
                mouse_event: MouseEvent,
                action: str = action,
                index: int = index,
            ) -> None:
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.selected = index
                    self.run_action(action)

            fragments.append((style, " ", click))
            fragments.append((f"{style} class:menu.key", label[0], click))
            fragments.append((style, label[1:] + " ", click))
            fragments.append(("", " "))

        return fragments

    def info_fragments(self) -> list[tuple[str, str]]:
        ready = sum(1 for plan in self.plans if plan.error is None)
        errors = len(self.plans) - ready

        fragments = [
            (
                "",
                f" Directory: {CONFIG['DIRECTORY']}  ·  "
                f"Version: {CONFIG['VERSION']!r}  ·  "
                f"{len(CONFIG['URLS'])} endpoint(s)  ·  "
                f"{ready} ready, ",
            ),
        ]

        fragments.append(
            ("class:info.error" if errors else "", f"{errors} error(s)")
        )

        return fragments

    def status_style(self) -> str:
        if self.confirm is not None:
            return "class:status.confirm"

        if self.busy:
            return "class:status.busy"

        return "class:status"

    def status_fragments(self) -> list[tuple[str, str]]:
        if self.confirm is not None:
            return [
                (
                    "",
                    f" {self.confirm.question}  Enter/y: yes  ·  n/Esc: no  "
                    "·  PgUp/PgDn: scroll the list first",
                )
            ]

        if self.busy:
            return [
                (
                    "",
                    f" {self.status or 'Working...'}  ·  "
                    "Ctrl+C: stop after the current file",
                )
            ]

        left = (
            f" {self.notice}"
            if self.notice
            else " Left/Right + Enter or underlined letter  ·  "
            "Up/Down/PgUp/PgDn scroll  ·  Ctrl+C quit"
        )

        total = len(self.lines)
        height = self.view_height()
        right = ""

        if total > height:
            right = (
                f"lines {self.scroll + 1}-"
                f"{min(total, self.scroll + height)} of {total} "
            )

        padding = max(1, terminal_size()[0] - len(left) - len(right))

        return [("", left + " " * padding + right)]

    # --------------------------------------------------------
    # Keys
    # --------------------------------------------------------

    def build_key_bindings(self) -> KeyBindings:
        keys = KeyBindings()

        idle = Condition(lambda: not self.busy and self.confirm is None)
        asking = Condition(lambda: self.confirm is not None)

        def move(step: int) -> None:
            self.selected = (self.selected + step) % len(MENU_ACTIONS)

        def page() -> int:
            return max(1, self.view_height() - 1)

        def to_end() -> None:
            self.scroll = self.max_scroll()
            self.follow = self.busy

        def hotkey(action: str) -> Callable[[Any], None]:
            def handler(event: Any) -> None:
                self.selected = MENU_ACTIONS.index(action)
                self.run_action(action)

            return handler

        def interrupt(event: Any) -> None:
            if self.busy:
                self.stop_requested.set()
                self.status = "Stopping after the current file..."
            elif self.confirm is not None:
                self.answer(False)
            else:
                event.app.exit()

        keys.add("left", filter=idle)(lambda event: move(-1))
        keys.add("right", filter=idle)(lambda event: move(1))
        keys.add("enter", filter=idle)(
            lambda event: self.run_action(MENU_ACTIONS[self.selected])
        )

        for action, label in self.menu_labels().items():
            letter = label[0].lower()
            keys.add(letter, filter=idle)(hotkey(action))
            keys.add(letter.upper(), filter=idle)(hotkey(action))

        for key in ("y", "Y", "enter"):
            keys.add(key, filter=asking)(lambda event: self.answer(True))

        for key in ("n", "N"):
            keys.add(key, filter=asking)(lambda event: self.answer(False))

        keys.add("escape", filter=asking, eager=True)(
            lambda event: self.answer(False)
        )

        keys.add("up")(lambda event: self.scroll_by(-1))
        keys.add("down")(lambda event: self.scroll_by(1))
        keys.add("pageup")(lambda event: self.scroll_by(-page()))
        keys.add("pagedown")(lambda event: self.scroll_by(page()))
        keys.add("home")(lambda event: self.scroll_by(-len(self.lines)))
        keys.add("end")(lambda event: to_end())

        keys.add("c-c", eager=True)(interrupt)

        return keys

    # --------------------------------------------------------
    # Actions
    # --------------------------------------------------------

    def run_action(self, action: str) -> None:
        if self.busy or self.confirm is not None:
            return

        self.notice = ""
        self.plans = analyze_files()
        plans = self.plans

        if action == "preview":
            self.show("Preview", lambda: preview(plans))

        elif action == "send":
            self.begin_send(plans)

        elif action == "write":
            self.begin_write()

        elif action == "config":
            self.show("Config", view_config)

        elif action == "help":
            self.show("Help", show_help)

        elif action == "quit":
            self.app.exit()

    def ask(
        self,
        question: str,
        on_yes: Callable[[], None],
        on_no: Callable[[], None],
    ) -> None:
        self.confirm = Confirm(question, on_yes, on_no)

    def answer(self, yes: bool) -> None:
        confirm = self.confirm
        self.confirm = None

        if confirm is None:
            return

        if yes:
            confirm.on_yes()
        else:
            confirm.on_no()

    def begin_send(self, plans: list[FilePlan]) -> None:
        valid_plans = [plan for plan in plans if plan.error is None]

        self.show("Send", lambda: show_send_order(plans))

        if not valid_plans:
            return

        def cancelled() -> None:
            self.notice = "Send cancelled. Nothing was sent."

        self.ask(
            f"{send_description(len(valid_plans))} "
            f"to {len(CONFIG['URLS'])} endpoint(s)?",
            lambda: self.start_send(plans),
            cancelled,
        )

    def start_send(self, plans: list[FilePlan]) -> None:
        self.draw = None
        self.text = ""
        self.version += 1
        self.scroll = 0
        self.follow = True
        self.busy = True
        self.status = "Starting..."
        self.stop_requested.clear()

        width = self.width()

        def set_status(text: str) -> None:
            self.status = text
            self.app.invalidate()

        def worker() -> None:
            live = Console(
                file=LiveWriter(self),
                force_terminal=True,
                color_system="256",
                width=width,
                height=50,
                legacy_windows=False,
            )

            sent = failed = 0

            try:
                with use_console(live):
                    sent, failed = send_files(
                        plans,
                        should_stop=self.stop_requested.is_set,
                        on_status=set_status,
                    )

                self.notice = (
                    f"Send finished: {sent} sent, {failed} failed."
                )

            except Exception as e:
                log.exception("Send crashed: %s", e)

                with use_console(live):
                    console.print(f"[red]ERROR: {e}[/red]")

                self.notice = "Send failed. See the list above."

            finally:
                self.busy = False
                self.plans = analyze_files()
                self.app.invalidate()

        threading.Thread(target=worker, daemon=True).start()

    def begin_write(self) -> None:
        pending: list[list[tuple[Path, int, int]]] = [[]]

        def draw() -> None:
            pending[0] = show_pending_version_writes()

        self.show("Write versions", draw)

        candidates = pending[0]

        if not candidates:
            return

        def apply() -> None:
            self.append(lambda: write_versions_to_files(candidates))
            self.plans = analyze_files()
            self.notice = f"Wrote versions into {len(candidates)} file(s)."

        def cancelled() -> None:
            self.notice = "Cancelled. No files were changed."

        self.ask(
            f"Write versions into {len(candidates)} files?",
            apply,
            cancelled,
        )


# ============================================================
# Entry point
# ============================================================

def main() -> None:
    if not load_config():
        pause_before_exit()
        return

    setup_logging()

    SenderUI().run()

    if session_logged:
        log.info("ODF Sender closed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        pause_before_exit()
        raise SystemExit(1)
