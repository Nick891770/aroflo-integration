"""
AroFlo Data Extractor

Extracts monthly financial data from AroFlo API and calculates metrics
for the scorecard spreadsheet.
"""

import calendar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from aroflo_connector import AroFloConnector, create_connector
from config import PRIMARY_CLIENT


@dataclass
class MonthlyMetrics:
    """Container for all monthly metrics."""

    # Lagging/Past metrics
    revenue: float = 0.0
    materials_cost: float = 0.0
    labour_cost: float = 0.0
    gross_profit_dollars: float = 0.0
    net_profit_dollars: float = 0.0
    gross_profit_percent: float = 0.0
    net_profit_percent: float = 0.0
    completed_jobs: int = 0
    average_job_value: float = 0.0

    # Leading/Predictive metrics
    primary_client_jobs: int = 0
    primary_client_value: float = 0.0
    other_client_jobs: int = 0
    other_client_value: float = 0.0
    jobs_total: float = 0.0
    primary_client_percent: float = 0.0
    other_client_percent: float = 0.0

    def calculate_derived_metrics(self):
        """Calculate derived metrics from base values."""
        # Gross Profit = Revenue - Materials
        self.gross_profit_dollars = self.revenue - self.materials_cost

        # Net Profit = Revenue - Materials - Labour
        self.net_profit_dollars = self.revenue - self.materials_cost - self.labour_cost

        # Percentages
        if self.revenue > 0:
            self.gross_profit_percent = (self.gross_profit_dollars / self.revenue) * 100
            self.net_profit_percent = (self.net_profit_dollars / self.revenue) * 100
        else:
            self.gross_profit_percent = 0.0
            self.net_profit_percent = 0.0

        # Average job value
        if self.completed_jobs > 0:
            self.average_job_value = self.revenue / self.completed_jobs
        else:
            self.average_job_value = 0.0

        # Jobs total
        self.jobs_total = self.primary_client_value + self.other_client_value

        # Client segmentation percentages
        if self.jobs_total > 0:
            self.primary_client_percent = (self.primary_client_value / self.jobs_total) * 100
            self.other_client_percent = (self.other_client_value / self.jobs_total) * 100
        else:
            self.primary_client_percent = 0.0
            self.other_client_percent = 0.0


class DataExtractor:
    """Extracts and processes data from AroFlo API."""

    def __init__(self, connector: Optional[AroFloConnector] = None):
        """
        Initialize the data extractor.

        Args:
            connector: AroFloConnector instance (creates one if not provided)
        """
        self.connector = connector or create_connector()

    def _get_month_date_range(
        self, year: int, month: int
    ) -> tuple[datetime, datetime]:
        """
        Get the start and end dates for a given month.

        Args:
            year: Year (e.g., 2026)
            month: Month number (1-12)

        Returns:
            Tuple of (start_date, end_date)
        """
        start_date = datetime(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        end_date = datetime(year, month, last_day)
        return start_date, end_date

    def _is_primary_client(self, client_name: str) -> bool:
        """
        Check if a client matches the configured primary client.

        Args:
            client_name: Name of the client

        Returns:
            True if client name contains PRIMARY_CLIENT (case-insensitive)
        """
        if not client_name:
            return False
        return bool(PRIMARY_CLIENT) and PRIMARY_CLIENT.lower() in client_name.lower()

    def _fetch_all_pages(
        self,
        fetch_func,
        start_date: datetime,
        end_date: datetime,
        **kwargs,
    ) -> list[dict[str, Any]]:
        """
        Fetch all pages of data from an API endpoint.

        Args:
            fetch_func: Function to call for each page
            start_date: Start date filter
            end_date: End date filter
            **kwargs: Additional arguments for the fetch function

        Returns:
            List of all items across all pages
        """
        all_items = []
        page = 1

        while True:
            response = fetch_func(
                start_date=start_date,
                end_date=end_date,
                page=page,
                **kwargs,
            )

            # Handle different response formats
            # Note: get_invoices already unwraps zoneresponse
            items = []
            if isinstance(response, dict):
                # Check common response structures
                if "invoices" in response:
                    items = response["invoices"]
                elif "projects" in response:
                    items = response["projects"]
                elif "tasks" in response:
                    items = response["tasks"]
                elif "data" in response:
                    items = response["data"]
                elif "items" in response:
                    items = response["items"]

            if not items:
                break

            all_items.extend(items)

            # Check if there are more pages
            total_pages = int(response.get("totalpages", 1) or 1)
            if page >= total_pages:
                break

            page += 1

        return all_items

    def _process_invoice_line_items(
        self, invoice: dict[str, Any]
    ) -> tuple[float, float, float]:
        """
        Process line items from an invoice to extract costs.

        AroFlo API field names:
        - totalex (not totalexgst)
        - lines (not lineitems), each with totalex and type

        Args:
            invoice: Invoice data dictionary

        Returns:
            Tuple of (total_ex_gst, materials_cost, labour_cost)
        """
        total_ex_gst = float(invoice.get("totalex", 0) or invoice.get("totalexgst", 0) or 0)
        materials_cost = 0.0
        labour_cost = 0.0

        # Get line items from invoice (AroFlo uses 'lines' key)
        line_items = invoice.get("lines", []) or invoice.get("lineitems", []) or []
        if isinstance(line_items, dict):
            line_items = line_items.get("lineitem", []) or line_items.get("line", []) or []

        for item in line_items:
            item_type = str(item.get("type", "")).lower()
            amount = float(item.get("totalex", 0) or item.get("totalexgst", 0) or item.get("amount", 0) or 0)

            if item_type in ("material", "materials", "stock"):
                materials_cost += amount
            elif item_type in ("labour", "labor", "time"):
                labour_cost += amount

        return total_ex_gst, materials_cost, labour_cost

    def get_monthly_report(self, year: int, month: int) -> MonthlyMetrics:
        """
        Fetch and calculate all metrics for a specific month.

        Args:
            year: Year (e.g., 2026)
            month: Month number (1-12)

        Returns:
            MonthlyMetrics object with all calculated values
        """
        metrics = MonthlyMetrics()
        start_date, end_date = self._get_month_date_range(year, month)

        print(f"Fetching data for {calendar.month_name[month]} {year}...")

        # Fetch invoices (server-side >= filter, client-side <= filter)
        print("  Fetching invoices...")
        all_invoices = self._fetch_all_pages(
            self.connector.get_invoices,
            start_date,
            end_date,
        )

        # Client-side date filter (AroFlo WHERE doesn't support multi-condition dates)
        # AroFlo date format: YYYY/MM/DD
        start_str = start_date.strftime('%Y/%m/%d')
        end_str = end_date.strftime('%Y/%m/%d')
        invoices = []
        for inv in all_invoices:
            inv_date = inv.get("dateinvoiced", "")
            if inv_date and start_str <= inv_date <= end_str:
                invoices.append(inv)

        print(f"  Found {len(invoices)} invoices (filtered from {len(all_invoices)} total)")

        # Process invoices
        for invoice in invoices:
            total_ex_gst, materials, labour = self._process_invoice_line_items(invoice)

            # Add to totals
            metrics.revenue += total_ex_gst
            metrics.materials_cost += materials
            metrics.labour_cost += labour
            metrics.completed_jobs += 1

            # Determine if primary client
            # AroFlo nests client under client.orgname (not clientname)
            client_obj = invoice.get("client", {})
            client_name = client_obj.get("orgname", "") if isinstance(client_obj, dict) else ""
            if not client_name:
                client_name = invoice.get("clientname", "")

            if self._is_primary_client(client_name):
                metrics.primary_client_jobs += 1
                metrics.primary_client_value += total_ex_gst
            else:
                metrics.other_client_jobs += 1
                metrics.other_client_value += total_ex_gst

        # Calculate derived metrics
        metrics.calculate_derived_metrics()

        print(f"  Revenue: ${metrics.revenue:,.2f}")
        print(f"  Gross Profit: ${metrics.gross_profit_dollars:,.2f} ({metrics.gross_profit_percent:.1f}%)")
        print(f"  Net Profit: ${metrics.net_profit_dollars:,.2f} ({metrics.net_profit_percent:.1f}%)")
        print(f"  Completed Jobs: {metrics.completed_jobs}")
        print(f"  Primary Client Jobs: {metrics.primary_client_jobs} (${metrics.primary_client_value:,.2f})")
        print(f"  Other Clients: {metrics.other_client_jobs} (${metrics.other_client_value:,.2f})")

        return metrics

    def get_completed_uninvoiced_jobs(self) -> list[dict[str, Any]]:
        """
        Fetch jobs that are completed but not yet invoiced.
        Used for proofreading job cards.

        Returns:
            List of job dictionaries
        """
        print("Fetching completed jobs...")

        # Try tasks - filter completed ones client-side for reliability
        try:
            response = self.connector.request("tasks", {"page": 1})

            # Extract tasks from zoneresponse structure
            zr = response.get("zoneresponse", {})
            all_tasks = zr.get("tasks", [])
            if not isinstance(all_tasks, list):
                all_tasks = [all_tasks] if all_tasks else []

            # Filter for completed status
            completed_tasks = [
                t for t in all_tasks
                if isinstance(t, dict) and t.get("status", "").lower() == "completed"
            ]

            # Fetch timesheets and attach labour notes to tasks
            completed_tasks = self._attach_timesheet_notes(completed_tasks)

            print(f"Found {len(completed_tasks)} completed tasks")
            return completed_tasks
        except Exception as e:
            print(f"Error fetching tasks: {e}")

        return []

    def _attach_timesheet_notes(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Fetch timesheets and attach labour notes to matching tasks.

        Args:
            tasks: List of task dictionaries

        Returns:
            Tasks with timesheet notes attached
        """
        try:
            response = self.connector.request("timesheets", {"page": 1})
            zr = response.get("zoneresponse", {})
            timesheets = zr.get("timesheets", [])
            if not isinstance(timesheets, list):
                timesheets = [timesheets] if timesheets else []

            # Build lookup by job number
            notes_by_job = {}
            for ts in timesheets:
                task_info = ts.get("task", {})
                job_no = task_info.get("jobnumber", "") if isinstance(task_info, dict) else ""
                note = ts.get("note", "")
                if job_no and note:
                    if job_no not in notes_by_job:
                        notes_by_job[job_no] = []
                    notes_by_job[job_no].append(note)

            # Attach notes to tasks
            for task in tasks:
                job_no = task.get("jobnumber", "")
                if job_no in notes_by_job:
                    task["labour_notes"] = "\n\n".join(notes_by_job[job_no])

        except Exception as e:
            print(f"  Warning: Could not fetch timesheets: {e}")

        return tasks


def get_monthly_report(year: int, month: int) -> MonthlyMetrics:
    """
    Convenience function to get monthly report data.

    Args:
        year: Year (e.g., 2026)
        month: Month number (1-12)

    Returns:
        MonthlyMetrics object with all calculated values
    """
    extractor = DataExtractor()
    return extractor.get_monthly_report(year, month)


if __name__ == "__main__":
    # Test data extraction for current month
    from datetime import datetime

    now = datetime.now()
    metrics = get_monthly_report(now.year, now.month)

    print("\n" + "=" * 50)
    print("Monthly Report Summary")
    print("=" * 50)
    print(f"Revenue: ${metrics.revenue:,.2f}")
    print(f"Gross Profit $: ${metrics.gross_profit_dollars:,.2f}")
    print(f"Gross Profit %: {metrics.gross_profit_percent:.2f}%")
    print(f"Net Profit $: ${metrics.net_profit_dollars:,.2f}")
    print(f"Net Profit %: {metrics.net_profit_percent:.2f}%")
    print(f"Completed Jobs: {metrics.completed_jobs}")
    print(f"Average Job Value: ${metrics.average_job_value:,.2f}")
    print(f"Primary Client Jobs: {metrics.primary_client_jobs}")
    print(f"Primary Client Value: ${metrics.primary_client_value:,.2f}")
    print(f"Other Clients Jobs: {metrics.other_client_jobs}")
    print(f"Other Clients Value: ${metrics.other_client_value:,.2f}")
    print(f"Jobs Total: ${metrics.jobs_total:,.2f}")
    print(f"Primary Client %: {metrics.primary_client_percent:.2f}%")
    print(f"Outside %: {metrics.other_client_percent:.2f}%")
