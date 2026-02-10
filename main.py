"""
AroFlo Integration Suite - Main Entry Point

This script provides the main interface for:
1. Fetching monthly financial data from AroFlo API
2. Generating financial reports with client segmentation
3. Proofreading job cards for spelling and grammar errors
4. Bulk-marking completed jobs as Ready to Invoice
"""

import argparse
import calendar
import sys
from datetime import datetime

from aroflo_connector import create_connector
from config import (
    AROFLO_ORG_NAME,
    AROFLO_SECRET_KEY,
    AROFLO_USERNAME,
    PRIMARY_CLIENT,
)
from data_extractor import DataExtractor
from proofreader import Proofreader, LANGUAGE_TOOL_AVAILABLE, SPELLCHECKER_AVAILABLE


def check_credentials() -> bool:
    """Check if API credentials are configured."""
    missing = []
    if not AROFLO_ORG_NAME:
        missing.append("AROFLO_ORG_NAME")
    if not AROFLO_USERNAME:
        missing.append("AROFLO_USERNAME")
    if not AROFLO_SECRET_KEY:
        missing.append("AROFLO_SECRET_KEY")

    if missing:
        print("Error: Missing API credentials!")
        print(f"Please set the following environment variables: {', '.join(missing)}")
        print("\nYou can set them in a .env file:")
        print("  AROFLO_ORG_NAME=your_org_name")
        print("  AROFLO_USERNAME=your_username")
        print("  AROFLO_PASSWORD=your_password")
        print("  AROFLO_SECRET_KEY=your_secret_key")
        return False
    return True


def cmd_update(args):
    """Handle the 'update' command to fetch data and display metrics."""
    if not check_credentials():
        return 1

    # Determine year and month
    now = datetime.now()
    year = args.year or now.year
    month = args.month or now.month

    print(f"\nAroFlo Monthly Update")
    print(f"{'=' * 50}")
    print(f"Period: {calendar.month_name[month]} {year}")

    # Test connection first
    print("\nTesting API connection...")
    connector = create_connector()
    if not connector.test_connection():
        print("Error: Failed to connect to AroFlo API")
        return 1
    print("Connection successful!")

    # Fetch data
    print("\nFetching monthly data...")
    extractor = DataExtractor(connector)
    metrics = extractor.get_monthly_report(year, month)

    client_label = PRIMARY_CLIENT or "Primary Client"
    print("\nMetrics:")
    print(f"  Revenue: ${metrics.revenue:,.2f}")
    print(f"  Gross Profit $: ${metrics.gross_profit_dollars:,.2f}")
    print(f"  Net Profit $: ${metrics.net_profit_dollars:,.2f}")
    print(f"  Completed Jobs: {metrics.completed_jobs}")
    print(f"  Average Job Value: ${metrics.average_job_value:,.2f}")
    print(f"  {client_label} Jobs: {metrics.primary_client_jobs}")
    print(f"  {client_label} Value: ${metrics.primary_client_value:,.2f}")
    print(f"  Other Client Jobs: {metrics.other_client_jobs}")
    print(f"  Other Client Value: ${metrics.other_client_value:,.2f}")
    return 0


def cmd_proofread(args):
    """Handle the 'proofread' command to check job card spelling/grammar."""
    if not check_credentials():
        return 1

    if not SPELLCHECKER_AVAILABLE and not LANGUAGE_TOOL_AVAILABLE:
        print("Error: No spell checker is installed.")
        print("Run: pip install pyspellchecker")
        return 1

    print("\nJob Card Proofreader")
    print(f"{'=' * 50}")

    # Test connection first
    print("\nTesting API connection...")
    connector = create_connector()
    if not connector.test_connection():
        print("Error: Failed to connect to AroFlo API")
        return 1
    print("Connection successful!")

    # Run proofreading
    proofreader = Proofreader(connector)
    results = proofreader.proofread_uninvoiced_jobs()
    proofreader.print_results(results, show_all=args.show_all)

    errors_found = sum(1 for r in results if r.has_errors)
    if errors_found > 0:
        print(f"\nFound errors in {errors_found} job(s)")
    else:
        print("\nNo spelling or grammar errors found")

    return 0


def cmd_test(args):
    """Handle the 'test' command to verify API connection."""
    print("\nAroFlo API Connection Test")
    print(f"{'=' * 50}")

    if not check_credentials():
        return 1

    print("\nTesting connection...")
    connector = create_connector()

    if connector.test_connection():
        print("SUCCESS: Connected to AroFlo API")
        return 0
    else:
        print("FAILED: Could not connect to AroFlo API")
        print("\nPlease verify:")
        print("  1. Your credentials are correct")
        print("  2. You have network access to api.aroflo.com")
        print("  3. Your API access is enabled in AroFlo")
        return 1


def cmd_mark_ready(args):
    """Handle the 'mark-ready' command to mark completed tasks as Ready to Invoice."""
    if not check_credentials():
        return 1

    print("\nMark Completed Tasks - Ready to Invoice")
    print("=" * 50)

    connector = create_connector()

    # Get substatus ID
    substatus_id = connector.get_substatus_id("Ready to Invoice")
    if not substatus_id:
        print("Error: Could not find 'Ready to Invoice' substatus")
        return 1

    # Get completed tasks
    response = connector.request("tasks", {"where": "and|status|=|Completed"})
    zone_response = response.get("zoneresponse", response)
    tasks = zone_response.get("tasks", [])

    if not tasks:
        print("No completed tasks found")
        return 0

    # Filter to those not already marked
    tasks_to_update = []
    for task in tasks:
        substatus = task.get("substatus", {})
        current = substatus.get("substatus", "") if isinstance(substatus, dict) else ""
        if current != "Ready to Invoice":
            tasks_to_update.append({
                "taskid": task.get("taskid"),
                "taskname": task.get("taskname"),
                "current": current or "(none)",
            })

    print(f"Found {len(tasks)} completed tasks")
    print(f"{len(tasks_to_update)} need substatus update")

    if not tasks_to_update:
        print("\nAll completed tasks already marked Ready to Invoice!")
        return 0

    print("\nTasks to update:")
    for t in tasks_to_update:
        print(f"  - {t['taskname'][:50]} [{t['current']}]")

    if not args.apply:
        print("\n" + "-" * 50)
        print("DRY RUN - No changes made")
        print("Run with --apply to update substatus")
        return 0

    # Apply changes
    print("\nUpdating tasks...")
    success = 0
    failed = 0

    for t in tasks_to_update:
        try:
            connector.update_task_substatus(t["taskid"], substatus_id)
            print(f"  [OK] {t['taskname'][:40]}")
            success += 1
        except Exception as e:
            print(f"  [FAIL] {t['taskname'][:40]}: {e}")
            failed += 1

    print(f"\nDone: {success} updated, {failed} failed")
    return 0 if failed == 0 else 1


def cmd_report(args):
    """Handle the 'report' command to generate a monthly report."""
    if not check_credentials():
        return 1

    # Determine year and month
    now = datetime.now()
    year = args.year or now.year
    month = args.month or now.month

    print(f"\nAroFlo Monthly Report")
    print(f"{'=' * 50}")
    print(f"Period: {calendar.month_name[month]} {year}")

    # Fetch data
    connector = create_connector()
    extractor = DataExtractor(connector)
    metrics = extractor.get_monthly_report(year, month)

    client_label = PRIMARY_CLIENT or "Primary Client"

    # Print report
    print(f"\n{'─' * 50}")
    print("LAGGING/PAST METRICS")
    print(f"{'─' * 50}")
    print(f"  Revenue/Sales Income:    ${metrics.revenue:>12,.2f}")
    print(f"  Gross Profit $:          ${metrics.gross_profit_dollars:>12,.2f}")
    print(f"  Net Profit $:            ${metrics.net_profit_dollars:>12,.2f}")
    print(f"  Gross Profit %:          {metrics.gross_profit_percent:>12.2f}%")
    print(f"  Net Profit %:            {metrics.net_profit_percent:>12.2f}%")
    print(f"  Completed Jobs:          {metrics.completed_jobs:>12}")
    print(f"  Average Job Value:       ${metrics.average_job_value:>12,.2f}")

    print(f"\n{'─' * 50}")
    print("LEADING/PREDICTIVE METRICS")
    print(f"{'─' * 50}")
    print(f"  {client_label} - # jobs:          {metrics.primary_client_jobs:>12}")
    print(f"  {client_label} - $ value:         ${metrics.primary_client_value:>12,.2f}")
    print(f"  Other Clients - # jobs:  {metrics.other_client_jobs:>12}")
    print(f"  Other Clients - $ value: ${metrics.other_client_value:>12,.2f}")
    print(f"  Jobs Total:              ${metrics.jobs_total:>12,.2f}")
    print(f"  % Sales from {client_label}:      {metrics.primary_client_percent:>12.2f}%")
    print(f"  % Sales from Other:     {metrics.other_client_percent:>12.2f}%")

    print(f"\n{'─' * 50}")
    print("COST BREAKDOWN")
    print(f"{'─' * 50}")
    print(f"  Materials Cost:          ${metrics.materials_cost:>12,.2f}")
    print(f"  Labour Cost:             ${metrics.labour_cost:>12,.2f}")
    print(f"  Total Costs:             ${metrics.materials_cost + metrics.labour_cost:>12,.2f}")

    return 0


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="AroFlo Integration Suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py test                          # Test API connection
  python main.py report                        # Generate report for current month
  python main.py report --month 1 --year 2026  # Report for January 2026
  python main.py update                        # Fetch and display current month data
  python main.py proofread                     # Check job cards for spelling errors
  python main.py mark-ready                    # Preview jobs to mark ready
  python main.py mark-ready --apply            # Mark completed jobs as Ready to Invoice
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Update command
    update_parser = subparsers.add_parser(
        "update",
        help="Fetch data from AroFlo and display monthly metrics",
    )
    update_parser.add_argument(
        "--month", "-m",
        type=int,
        choices=range(1, 13),
        help="Month number (1-12). Defaults to current month.",
    )
    update_parser.add_argument(
        "--year", "-y",
        type=int,
        help="Year (e.g., 2026). Defaults to current year.",
    )
    update_parser.set_defaults(func=cmd_update)

    # Report command
    report_parser = subparsers.add_parser(
        "report",
        help="Generate a detailed monthly report",
    )
    report_parser.add_argument(
        "--month", "-m",
        type=int,
        choices=range(1, 13),
        help="Month number (1-12). Defaults to current month.",
    )
    report_parser.add_argument(
        "--year", "-y",
        type=int,
        help="Year (e.g., 2026). Defaults to current year.",
    )
    report_parser.set_defaults(func=cmd_report)

    # Proofread command
    proofread_parser = subparsers.add_parser(
        "proofread",
        help="Check job cards for spelling and grammar errors",
    )
    proofread_parser.add_argument(
        "--show-all",
        action="store_true",
        help="Show all jobs, not just those with errors",
    )
    proofread_parser.set_defaults(func=cmd_proofread)

    # Mark ready command
    markready_parser = subparsers.add_parser(
        "mark-ready",
        help="Mark completed tasks as Ready to Invoice",
    )
    markready_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply changes (default is dry-run)",
    )
    markready_parser.set_defaults(func=cmd_mark_ready)

    # Test command
    test_parser = subparsers.add_parser(
        "test",
        help="Test the API connection",
    )
    test_parser.set_defaults(func=cmd_test)

    # Parse arguments
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    # Run the appropriate command
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
