"""Fetch the Chinese workday calendars (holiday-cn) into data/ for offline use.

The service also refreshes these automatically once a day; this CLI exists for
first-time setup, air-gapped machines and post-incident verification.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mumu_common import TZ, refresh_workdays, workday_summary  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=None,
                        help="fetch a specific year (default: this year and next)")
    args = parser.parse_args()
    years = (args.year,) if args.year else (datetime.now(TZ).year, datetime.now(TZ).year + 1)
    fetched = refresh_workdays(years)
    for year in years:
        summary = workday_summary(year)
        if not summary["days"]:
            print(f"{year}: no calendar cached (not published yet or download failed); "
                  "weekday-only fallback applies")
            continue
        note = "fetched now" if year in fetched else "already cached"
        print(f"{year}: {summary['days']} special days ({note})")
        for date in summary["off"]:
            print(f"  off  {date}")
        for date in summary["extra_work"]:
            print(f"  WORK {date}  <- weekend that is a working day")
    return 0


if __name__ == "__main__":
    sys.exit(main())
