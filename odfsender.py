import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from InquirerPy import inquirer
from InquirerPy.utils import get_style
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()

TOKEN_HEADER = "x-ApiKey"
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_FILE = APP_DIR / "config.json"
VERSIONS_FILE = APP_DIR / "versions.json"
HTTP_TIMEOUT_SECONDS = 30
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


def pause_before_exit() -> None:
    try:
        input("Press Enter to close...")
    except EOFError:
        pass

ODF_BODY_RE = re.compile(rb"<OdfBody\b[^>]*>")
VERSION_RE = re.compile(rb'(\bVersion\s*=\s*)(["\'])(.*?)\2')


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
            '[green]Note: You can provide a specific version number such as 5, use "auto" to automatically increment the version before sending each XML, or null to send XML exactly as-is.[/green]'
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
            or version == "auto"
            or (isinstance(version, int) and not isinstance(version, bool))
        ):
            raise ValueError('VERSION must be null, an integer, or "auto".')

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

    return sorted(
        file
        for file in directory.iterdir()
        if file.is_file()
        and file.suffix.lower() == ".xml"
    )


def analyze_files() -> list[FilePlan]:
    """
    Reads and validates all XML files beforehand.

    This is used both for:
      - dynamic menu counts
      - preview
      - send planning
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

            elif CONFIG["VERSION"] == "auto":
                if remembered_version is not None:
                    version_to_send = remembered_version + 1
                else:
                    version_to_send = current_version + 1

            else:
                version_to_send = int(CONFIG["VERSION"])

            plans.append(
                FilePlan(
                    file=file,
                    current_version=current_version,
                    remembered_version=remembered_version,
                    version_to_send=version_to_send,
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

    return plans


# ============================================================
# HTTP
# ============================================================

def send_xml(url: str, xml: bytes) -> None:
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
        "Uses its remembered version from versions.json, or the current "
        "XML version if none is remembered. Successful sends are remembered."
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

    confirmed = inquirer.confirm(
        message=(
            f"Send {len(valid_plans)} files "
            f"to {len(CONFIG['URLS'])} endpoint(s)?"
        ),
        default=False,
    ).execute()

    if not confirmed:
        return

    versions = (
        load_versions()
        if CONFIG["VERSION"] is not None
        else {}
    )

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

            else:
                xml_to_send = set_xml_version(
                    original_xml,
                    plan.version_to_send,
                )

                console.print(
                    f"  Version: {plan.version_to_send}"
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

                        send_xml(
                            url,
                            xml_to_send,
                        )

                    console.print(
                        f"  [green]✓[/green] {url}"
                    )

                except Exception as e:
                    all_succeeded = False

                    console.print(
                        f"  [red]✗[/red] {url}"
                    )

                    console.print(
                        f"    [red]{e}[/red]"
                    )

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

        except Exception as e:
            console.print(
                f"  [red]ERROR: {e}[/red]"
            )

        console.print()


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
        elif CONFIG["VERSION"] == "auto":
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
            message="ODF Sender",
            choices=[
                preview_label,
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