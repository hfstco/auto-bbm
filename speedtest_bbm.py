#!/usr/bin/env python3
"""Run a Breitbandmessung browser speed test with Selenium and save CSV results."""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import tempfile
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


TEST_URL = "https://www.breitbandmessung.de/test"
MEASUREMENT_TIMEOUT_SECONDS = 600
CSV_DOWNLOAD_TIMEOUT_SECONDS = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run https://www.breitbandmessung.de/test and save the finished measurement to CSV."
    )
    parser.add_argument(
        "-o",
        "--output",
        default="results.csv",
        help="CSV file to create or append to. Default: %(default)s",
    )
    parser.add_argument(
        "--browser",
        choices=("chrome", "firefox"),
        default="chrome",
        help="Browser to automate. Selenium Manager will locate or fetch the driver. Default: %(default)s",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser without opening a visible window.",
    )
    return parser.parse_args()


def build_driver(browser: str, download_dir: Path, headless: bool) -> webdriver.Remote:
    if browser == "chrome":
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1440,1000")
        options.add_argument("--disable-notifications")
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(download_dir),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "profile.default_content_setting_values.geolocation": 2,
                "profile.default_content_setting_values.notifications": 2,
            },
        )
        return webdriver.Chrome(options=options)

    options = webdriver.FirefoxOptions()
    if headless:
        options.add_argument("-headless")
    options.set_preference("browser.download.folderList", 2)
    options.set_preference("browser.download.dir", str(download_dir))
    options.set_preference(
        "browser.helperApps.neverAsk.saveToDisk",
        "text/csv,application/csv,application/force-download",
    )
    options.set_preference("pdfjs.disabled", True)
    options.set_preference("geo.enabled", False)
    return webdriver.Firefox(options=options)


def xpath_for_button_text(*texts: str) -> str:
    parts = [
        "contains(normalize-space(.), %s)" % xpath_literal(text)
        for text in texts
    ]
    return (
        "//*[self::button or self::a or @role='button']"
        "[not(@disabled) and (%s)]" % " or ".join(parts)
    )


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    return "concat(%s)" % ', "\'", '.join(f"'{piece}'" for piece in pieces)


def click_if_present(driver: webdriver.Remote, wait: WebDriverWait, xpath: str) -> bool:
    try:
        element = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
    except TimeoutException:
        return False
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    driver.execute_script("arguments[0].click();", element)
    return True


def run_measurement(driver: webdriver.Remote) -> None:
    short_wait = WebDriverWait(driver, 8)
    wait = WebDriverWait(driver, MEASUREMENT_TIMEOUT_SECONDS)

    driver.get(TEST_URL)

    click_if_present(
        driver,
        short_wait,
        xpath_for_button_text("Nur notwendige Cookies verwenden", "Alle Cookies zulassen"),
    )

    click_if_present(driver, short_wait, xpath_for_button_text("Browsermessung starten"))

    click_if_present(driver, short_wait, xpath_for_button_text("Akzeptieren"))

    try:
        wait.until(
            lambda d: "Die Messung ist abgeschlossen." in d.page_source
            or "Es ist ein Fehler aufgetreten" in d.page_source
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"Measurement did not finish within {MEASUREMENT_TIMEOUT_SECONDS} seconds."
        ) from exc

    if "Es ist ein Fehler aufgetreten" in driver.page_source:
        message = read_error_message(driver)
        raise RuntimeError(f"Breitbandmessung reported an error: {message}")


def read_error_message(driver: webdriver.Remote) -> str:
    try:
        modal = driver.find_element(By.XPATH, "//*[contains(., 'Es ist ein Fehler aufgetreten')]")
    except Exception:
        return "unknown error"
    text = " ".join(modal.text.split())
    return text or "unknown error"


def export_csv(driver: webdriver.Remote, download_dir: Path) -> Path:
    before = {path.resolve() for path in download_dir.glob("*")}
    wait = WebDriverWait(driver, 30)
    clicked = click_if_present(driver, wait, xpath_for_button_text("Ergebnis exportieren (csv)"))
    if not clicked:
        raise RuntimeError("The export CSV button was not found after the measurement completed.")

    deadline = time.time() + CSV_DOWNLOAD_TIMEOUT_SECONDS
    while time.time() < deadline:
        candidates = [
            path
            for path in download_dir.glob("*")
            if path.resolve() not in before
            and path.is_file()
            and path.suffix.lower() == ".csv"
            and not path.name.endswith((".crdownload", ".part", ".tmp"))
        ]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime)
        time.sleep(0.5)

    raise RuntimeError("CSV export did not appear in the download directory.")


def append_or_create_csv(downloaded_csv: Path, output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    if not output_csv.exists() or output_csv.stat().st_size == 0:
        shutil.copyfile(downloaded_csv, output_csv)
        return

    with downloaded_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        rows = list(csv.reader(handle, dialect))

    if len(rows) < 2:
        raise RuntimeError(f"Downloaded CSV does not contain a data row: {downloaded_csv}")

    with output_csv.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, dialect)
        writer.writerows(rows[1:])


def main() -> int:
    args = parse_args()
    output_csv = Path(args.output).expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="bbm-download-") as temp_dir:
        download_dir = Path(temp_dir)
        driver = build_driver(args.browser, download_dir, args.headless)
        try:
            run_measurement(driver)
            downloaded_csv = export_csv(driver, download_dir)
            append_or_create_csv(downloaded_csv, output_csv)
        finally:
            driver.quit()

    print(f"Results saved to: {output_csv}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
