import json
import logging
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from InquirerPy import inquirer
from InquirerPy.utils import get_style
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()

APP_VERSION = "1.2"
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
AUTO_MODES = ("auto", "auto-as-is")
CONFIG: dict[str, Any] = {}
MENU_STYLE = get_style(
    {
        "question": "fg:#1f2937 bg:#ffd54a bold",
        "highlighted": "fg:#ffffff bg:#005fb8",
        "selected": "fg:#ffffff bg:#005fb8",
        "selected-option": "fg:#ffffff bg:#005fb8",
        "pointer": "fg:#ffffff bg:#005fb8",
    }
)


def setup_logging() -> None:
    """
    Starts a new log file next to the app for this run, named
    sender_yyyy-mm-dd_hh-MM-ss.log.
    If the log cannot be opened, the sender keeps working without it.
    """

    log.setLevel(logging.INFO)

    log_file = APP_DIR / (
        "sender_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
    )

    try:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as e:
        console.print(
            f"[yellow]Could not open {log_file.name}: {e}. "
            "Continuing without a log file.[/yellow]"
        )
        return

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )
    log.addHandler(handler)


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
        title="Send Preview",
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

    for plan in plans:
        if plan.error:
            table.add_row(
                plan.file.name,
                "-",
                "-",
                "-",
                "-",
                f"[red]{plan.error}[/red]",
            )

            continue

        version_to_send = (
            str(plan.version_to_send)
            if plan.version_to_send is not None
            else "unchanged"
        )

        table.add_row(
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


def show_send_order(
    plans: list[FilePlan],
    read_only: bool = True,
) -> None:
    """
    Lists the files in the exact order they will be sent, with the
    version each one will go out with.

    read_only=False is used right before sending, so the closing
    "nothing was sent" note is left out.
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

    if read_only:
        console.print(
            "[dim]Nothing was sent or modified.[/dim]"
        )
        console.print()


# ============================================================
# Sending
# ============================================================

def send_files(plans: list[FilePlan]) -> None:
    valid_plans = [
        plan
        for plan in plans
        if plan.error is None
    ]

    if not valid_plans:
        console.print(
            "[yellow]There are no valid files to send.[/yellow]"
        )
        return

    show_send_order(plans, read_only=False)

    confirmed = inquirer.confirm(
        message=(
            f"Send {len(valid_plans)} files "
            f"to {len(CONFIG['URLS'])} endpoint(s)?"
        ),
        default=False,
    ).execute()

    if not confirmed:
        log.info("Send cancelled by user (%d file(s) ready)", len(valid_plans))
        return

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
                    with Progress(
                        SpinnerColumn(),
                        TextColumn(
                            f"POST {url}"
                        ),
                        transient=True,
                    ) as progress:

                        progress.add_task(
                            "",
                            total=None,
                        )

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


# ============================================================
# Write remembered versions into XML files
# ============================================================

def write_versions_to_files() -> None:
    try:
        versions = load_versions()
    except Exception as e:
        console.print(
            f"[red]Could not load versions.json: {e}[/red]"
        )
        return

    if not versions:
        console.print(
            "[yellow]No remembered versions found.[/yellow]"
        )
        return

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
        return

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

    confirmed = inquirer.confirm(
        message=(
            f"Write versions into "
            f"{len(candidates)} files?"
        ),
        default=False,
    ).execute()

    if not confirmed:
        return

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

        except Exception as e:
            console.print(
                f"[red]✗ {file.name}: {e}[/red]"
            )


# ============================================================
# Main menu
# ============================================================

def main() -> None:
    if not load_config():
        pause_before_exit()
        return

    setup_logging()

    log.info(
        "ODF Sender v%s started (VERSION=%r)",
        APP_VERSION,
        CONFIG["VERSION"],
    )

    while True:
        #
        # Analyze before displaying the menu.
        #
        # This means "Send 56 files" reflects what's actually
        # present and valid right now.
        #
        plans = analyze_files()

        sendable_count = sum(
            1
            for plan in plans
            if plan.error is None
        )

        error_count = len(plans) - sendable_count

        send_label = f"Send {sendable_count} files"

        if CONFIG["VERSION"] is None:
            send_label += " without changing versions"
        elif is_auto():
            send_label += " and increment the version"
        else:
            send_label += f" with Version {CONFIG['VERSION']}"

        preview_label = (
            f"Preview {len(plans)} files"
        )

        if error_count:
            preview_label += (
                f" ({error_count} errors)"
            )

        action = inquirer.select(
            message=f"ODF Sender v{APP_VERSION}",
            choices=[
                preview_label,
                "Show send order",
                send_label,
                "Write versions to files",
                "View config",
                "Help",
                "Exit",
            ],
            default=preview_label,
            style=MENU_STYLE,
        ).execute()

        if action == preview_label:
            preview(plans)

        elif action == "Show send order":
            show_send_order(plans)

        elif action == send_label:
            #
            # Analyze again immediately before sending.
            # The directory may have changed while the
            # menu was open.
            #
            plans = analyze_files()

            send_files(plans)

        elif action == "Write versions to files":
            write_versions_to_files()

        elif action == "View config":
            view_config()

        elif action == "Help":
            show_help()

        elif action == "Exit":
            break


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        pause_before_exit()
        raise SystemExit(1)