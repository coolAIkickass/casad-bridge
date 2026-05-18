#!/usr/bin/env python3
"""
run_report.py — Generate a report directly from a fixture JSON file.
Bypasses WhatsApp and AI parsing entirely. Instant iteration.

Usage:
  python3 run_report.py                                   # uses test_fixture_khokhara.json, Excel R&B
  python3 run_report.py --fmt word                        # Word format
  python3 run_report.py --fmt excel_rb                    # Excel R&B (default)
  python3 run_report.py --fmt excel_amc                   # Excel AMC
  python3 run_report.py --fixture my_fixture.json         # different fixture
  python3 run_report.py --no-photos                       # skip photo processing (fastest)
"""

import os, sys, json, argparse, subprocess
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault('EXCEL_TEMPLATE_PATH', 'casad_excel_template.xlsx')
os.environ.setdefault('AMC_TEMPLATE_PATH',   'casad_amc_template.xlsx')
os.environ.setdefault('OUTPUT_DIR',          'media')

RESET  = '\033[0m'; BOLD = '\033[1m'; GREEN = '\033[32m'
CYAN   = '\033[36m'; YELLOW = '\033[33m'; RED = '\033[31m'
def info(m): print(f'\033[33mℹ   {m}\033[0m')
def ok(m):   print(f'\033[32m✅  {m}\033[0m')
def err(m):  print(f'\033[31m❌  {m}\033[0m')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture',   default='test_fixture_khokhara.json')
    parser.add_argument('--fmt',       default='excel_rb',
                        choices=['word', 'excel_rb', 'excel_amc'])
    parser.add_argument('--no-photos', action='store_true',
                        help='Strip photos from fixture (much faster, no API calls)')
    args = parser.parse_args()

    # Load fixture
    fixture_path = args.fixture
    if not os.path.exists(fixture_path):
        err(f'Fixture not found: {fixture_path}')
        sys.exit(1)

    with open(fixture_path) as f:
        report_json = json.load(f)

    if args.no_photos:
        report_json['photos']           = []
        report_json['photo_titles']     = []
        report_json['photo_categories'] = []
        info('Photos stripped — fast mode')
    else:
        # Verify photos exist
        photos = report_json.get('photos', [])
        missing = [p for p in photos if p and not os.path.exists(p)]
        if missing:
            for p in missing:
                err(f'Photo not found: {p}')
            sys.exit(1)
        info(f'{len(photos)} photo(s) loaded')

    info(f'Format: {args.fmt}  |  Fixture: {fixture_path}')
    info(f'Bridge: {report_json.get("bridge_title", "?")}')

    # Generate report
    if args.fmt == 'word':
        from report_gen import build_docx
        out = build_docx(report_json)
    elif args.fmt == 'excel_rb':
        from report_gen_excel import build_excel
        out = build_excel(report_json)
    elif args.fmt == 'excel_amc':
        from report_gen_excel_amc import build_excel_amc
        out = build_excel_amc(report_json)

    size_kb = os.path.getsize(out) // 1024
    ok(f'Report saved: {out}  ({size_kb} KB)')

    # Open the file automatically
    try:
        subprocess.Popen(['open', out])
        info('Opened in default app ↗')
    except Exception:
        pass

if __name__ == '__main__':
    main()
