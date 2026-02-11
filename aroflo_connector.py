"""
AroFlo API Connector

Handles authentication and API requests to the AroFlo API using HMAC-SHA512 authentication.
"""

import base64
import hashlib
import hmac
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from config import (
    AROFLO_BASE_URL,
    AROFLO_HOST_IP,
    AROFLO_ORG_NAME,
    AROFLO_PASSWORD,
    AROFLO_SECRET_KEY,
    AROFLO_USERNAME,
    API_CALLS_PER_MINUTE,
)


class AroFloConnector:
    """Handles authentication and requests to the AroFlo API."""

    def __init__(
        self,
        org_encoded: str = AROFLO_ORG_NAME,
        u_encoded: str = AROFLO_USERNAME,
        p_encoded: str = AROFLO_PASSWORD,
        secret_key: str = AROFLO_SECRET_KEY,
        host_ip: Optional[str] = None,
    ):
        """
        Initialize the AroFlo API connector.

        Args:
            org_encoded: orgEncoded value from AroFlo admin
            u_encoded: uEncoded value from AroFlo admin
            p_encoded: pEncoded value from AroFlo admin
            secret_key: Secret key for HMAC authentication
            host_ip: Your public IP (set to None to disable)
        """
        self.base_url = AROFLO_BASE_URL
        self.org_encoded = org_encoded
        self.u_encoded = u_encoded
        self.p_encoded = p_encoded
        self.secret_key = secret_key
        self.host_ip = host_ip

        # Rate limiting
        self._last_request_time = 0
        self._min_request_interval = 60 / API_CALLS_PER_MINUTE

        # Session for connection pooling
        self.session = requests.Session()

    def _generate_auth(self, var_string: str, accept: str = "text/json") -> tuple[dict[str, str], str]:
        """
        Generate authentication headers for AroFlo API.

        Based on the official Postman pre-request script, the HMAC payload is:
        requestType + HostIP(if set) + urlPath + accept + Authorization + timestamp + VarString
        All joined with '+' signs.

        Args:
            var_string: The URL query string (e.g., 'zone=invoices&page=1')
            accept: Accept header value ('text/json' or 'text/xml')

        Returns:
            Tuple of (headers dict, full URL with query string)
        """
        # 1. Get UTC timestamp in ISO 8601 format with milliseconds
        now = datetime.now(timezone.utc)
        timestamp = now.strftime('%Y-%m-%dT%H:%M:%S') + f'.{now.microsecond // 1000:03d}Z'

        # 2. Construct the Authorization string (query string format, URL encoded)
        u_encoded_url = urllib.parse.quote(self.u_encoded, safe='')
        p_encoded_url = urllib.parse.quote(self.p_encoded, safe='')
        org_encoded_url = urllib.parse.quote(self.org_encoded, safe='')

        auth_string = f"uencoded={u_encoded_url}&pencoded={p_encoded_url}&orgEncoded={org_encoded_url}"

        # 3. Build the payload array (matching Postman's pre-request script exactly)
        # Format: requestType + HostIP(if set) + urlPath + accept + Authorization + timestamp + VarString
        request_type = 'GET'
        url_path = ''  # Always empty

        payload = [request_type]

        # Only include HostIP if it's set
        if self.host_ip:
            payload.append(self.host_ip)

        payload.append(url_path)
        payload.append(accept)
        payload.append(auth_string)
        payload.append(timestamp)
        payload.append(var_string)

        # 4. Create the string to sign by joining with '+'
        string_to_sign = '+'.join(payload)

        # 5. Create HMAC-SHA512 signature (hex encoded)
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()

        # 6. Build headers
        headers = {
            "Accept": accept,
            "Authentication": f"HMAC {signature}",
            "Authorization": auth_string,
            "afdatetimeutc": timestamp,
        }

        # Only include HostIP header if provided
        if self.host_ip:
            headers["HostIP"] = self.host_ip

        return headers

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def request(
        self,
        zone: str,
        params: Optional[dict] = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        """
        Make an authenticated request to the AroFlo API.

        Args:
            zone: API zone/endpoint (e.g., 'invoices', 'projects', 'tasks')
            params: Additional query parameters
            retries: Number of retry attempts on failure

        Returns:
            JSON response from the API

        Raises:
            requests.RequestException: On network or API errors
            ValueError: On authentication errors
        """
        if not all([self.org_encoded, self.u_encoded, self.secret_key]):
            raise ValueError(
                "Missing credentials. Please set AROFLO_ORG_NAME (orgEncoded), "
                "AROFLO_USERNAME (uEncoded), AROFLO_PASSWORD (pEncoded), "
                "and AROFLO_SECRET_KEY environment variables."
            )

        # Build URL query parameters (URL encoded)
        url_params = [f"zone={urllib.parse.quote(zone, safe='')}"]
        if params:
            for key, value in params.items():
                url_params.append(f"{key}={urllib.parse.quote(str(value), safe='')}")

        # Build the var_string (query string without leading ?)
        var_string = '&'.join(url_params)

        # Full URL
        url = f"{self.base_url}?{var_string}"

        # Apply rate limiting
        self._rate_limit()

        # Make request with retries
        last_error = None
        for attempt in range(retries):
            try:
                # Generate fresh auth for each attempt (timestamp changes)
                headers = self._generate_auth(var_string, accept="text/json")

                # Try GET request (as shown in AroFlo docs)
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=30,
                )

                response.raise_for_status()

                # Parse JSON response
                data = response.json()

                # Check for API-level errors
                if isinstance(data, dict):
                    if data.get("error"):
                        raise ValueError(f"API Error: {data.get('error')}")
                    if data.get("status") == "-99999":
                        raise ValueError(
                            f"API Error: {data.get('statusmessage', 'Authentication failed')}"
                        )

                return data

            except requests.RequestException as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                continue

        raise last_error or ValueError("Request failed after all retries")

    def update_task_substatus(
        self,
        task_id: str,
        substatus_id: str,
        retries: int = 3,
    ) -> dict[str, Any]:
        """
        Update a task's substatus in AroFlo.

        Args:
            task_id: The task ID to update
            substatus_id: New substatus ID to set
            retries: Number of retry attempts on failure

        Returns:
            JSON response from the API
        """
        if not all([self.org_encoded, self.u_encoded, self.secret_key]):
            raise ValueError("Missing credentials.")

        # Build XML payload
        postxml = f'''<?xml version="1.0" encoding="utf-8"?>
<aroflo>
    <task>
        <taskid>{task_id}</taskid>
        <substatus>
            <substatusid>{substatus_id}</substatusid>
        </substatus>
    </task>
</aroflo>'''

        zone = "tasks"
        # For POST: var_string includes zone and URL-encoded XML
        var_string = f"zone={zone}&postxml={urllib.parse.quote(postxml, safe='')}"

        self._rate_limit()

        last_error = None
        for attempt in range(retries):
            try:
                headers = self._generate_auth_post(var_string, accept="text/json")
                headers["Content-Type"] = "application/x-www-form-urlencoded"

                response = self.session.post(
                    self.base_url,
                    headers=headers,
                    data=var_string,
                    timeout=30,
                )

                response.raise_for_status()
                data = response.json()

                if isinstance(data, dict):
                    if data.get("error"):
                        raise ValueError(f"API Error: {data.get('error')}")
                    if data.get("status") == "-99999":
                        raise ValueError(
                            f"API Error: {data.get('statusmessage', 'Authentication failed')}"
                        )

                return data

            except requests.RequestException as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                continue

        raise last_error or ValueError("Request failed after all retries")

    def _generate_auth_post(self, var_string: str, accept: str = "text/json") -> dict[str, str]:
        """
        Generate authentication headers for POST requests.

        For POST, the var_string must include all form data (zone and postxml with full content).
        """
        now = datetime.now(timezone.utc)
        timestamp = now.strftime('%Y-%m-%dT%H:%M:%S') + f'.{now.microsecond // 1000:03d}Z'

        u_encoded_url = urllib.parse.quote(self.u_encoded, safe='')
        p_encoded_url = urllib.parse.quote(self.p_encoded, safe='')
        org_encoded_url = urllib.parse.quote(self.org_encoded, safe='')

        auth_string = f"uencoded={u_encoded_url}&pencoded={p_encoded_url}&orgEncoded={org_encoded_url}"

        # Build HMAC payload
        payload = ['POST']
        if self.host_ip:
            payload.append(self.host_ip)
        payload.append('')  # url_path always empty
        payload.append(accept)
        payload.append(auth_string)
        payload.append(timestamp)
        payload.append(var_string)

        string_to_sign = '+'.join(payload)

        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()

        headers = {
            "Accept": accept,
            "Authentication": f"HMAC {signature}",
            "Authorization": auth_string,
            "afdatetimeutc": timestamp,
        }

        if self.host_ip:
            headers["HostIP"] = self.host_ip

        return headers

    def get_substatuses(self) -> list[dict]:
        """Get all available substatuses."""
        response = self.request("substatuses")
        zone_response = response.get("zoneresponse", response)
        return zone_response.get("substatuses", [])

    def get_substatus_id(self, name: str) -> Optional[str]:
        """Get substatus ID by name (case-insensitive)."""
        substatuses = self.get_substatuses()
        for s in substatuses:
            if s.get("substatus", "").lower() == name.lower():
                return s.get("substatusid")
        return None

    def mark_task_ready_to_invoice(self, task_id: str) -> dict[str, Any]:
        """Mark a task as Ready to Invoice."""
        substatus_id = self.get_substatus_id("Ready to Invoice")
        if not substatus_id:
            raise ValueError("Could not find 'Ready to Invoice' substatus")
        return self.update_task_substatus(task_id, substatus_id)

    def update_task_description(
        self,
        task_id: str,
        description: str,
        retries: int = 3,
    ) -> dict[str, Any]:
        """
        Update a task's description in AroFlo.

        Args:
            task_id: The task ID to update
            description: New description text
            retries: Number of retry attempts on failure

        Returns:
            JSON response from the API
        """
        if not all([self.org_encoded, self.u_encoded, self.secret_key]):
            raise ValueError("Missing credentials.")

        # Escape XML special characters
        description = (description
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))

        postxml = f'''<?xml version="1.0" encoding="utf-8"?>
<aroflo>
    <task>
        <taskid>{task_id}</taskid>
        <description>{description}</description>
    </task>
</aroflo>'''

        zone = "tasks"
        var_string = f"zone={zone}&postxml={urllib.parse.quote(postxml, safe='')}"

        self._rate_limit()

        last_error = None
        for attempt in range(retries):
            try:
                headers = self._generate_auth_post(var_string, accept="text/json")
                headers["Content-Type"] = "application/x-www-form-urlencoded"

                response = self.session.post(
                    self.base_url,
                    headers=headers,
                    data=var_string,
                    timeout=30,
                )

                response.raise_for_status()
                data = response.json()

                if isinstance(data, dict):
                    if data.get("status") == "-99999":
                        raise ValueError(
                            f"API Error: {data.get('statusmessage', 'Authentication failed')}"
                        )

                return data

            except requests.RequestException as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                continue

        raise last_error or ValueError("Request failed after all retries")

    def update_timesheet_note(self, *args, **kwargs):
        """
        NOT SUPPORTED by the AroFlo API.

        The API returns misleading success responses (updatetotal=1) but does
        not actually persist changes to timesheet notes. Tested with multiple
        XML structures (direct timesheets zone, nested under tasks, etc.) —
        none work. Timesheet notes must be edited manually in the AroFlo UI.

        See proofread_and_mark_ready.py which prints a manual correction list
        for timesheet notes instead.
        """
        raise NotImplementedError(
            "AroFlo API does not support updating timesheet notes. "
            "Edit them manually in the AroFlo UI."
        )

    def get_invoices(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """
        Fetch invoices from the AroFlo API.

        Args:
            start_date: Filter invoices from this date
            end_date: Filter invoices until this date
            page: Page number for pagination

        Returns:
            Invoice data from the API
        """
        params = {"page": page}

        where_clauses = []
        if start_date:
            where_clauses.append(f"invoicedate>={start_date.strftime('%Y-%m-%d')}")
        if end_date:
            where_clauses.append(f"invoicedate<={end_date.strftime('%Y-%m-%d')}")

        if where_clauses:
            params["where"] = " AND ".join(where_clauses)

        return self.request("invoices", params)

    def get_projects(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        status: Optional[str] = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """
        Fetch projects/jobs from the AroFlo API.

        Args:
            start_date: Filter projects from this date
            end_date: Filter projects until this date
            status: Filter by project status
            page: Page number for pagination

        Returns:
            Project data from the API
        """
        params = {"page": page}

        where_clauses = []
        if start_date:
            where_clauses.append(f"completeddate>={start_date.strftime('%Y-%m-%d')}")
        if end_date:
            where_clauses.append(f"completeddate<={end_date.strftime('%Y-%m-%d')}")
        if status:
            where_clauses.append(f"status={status}")

        if where_clauses:
            params["where"] = " AND ".join(where_clauses)

        return self.request("projects", params)

    def get_tasks(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        status: Optional[str] = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """
        Fetch tasks from the AroFlo API.

        Args:
            start_date: Filter tasks from this date
            end_date: Filter tasks until this date
            status: Filter by task status
            page: Page number for pagination

        Returns:
            Task data from the API
        """
        params = {"page": page}

        where_clauses = []
        if start_date:
            where_clauses.append(f"completeddate>={start_date.strftime('%Y-%m-%d')}")
        if end_date:
            where_clauses.append(f"completeddate<={end_date.strftime('%Y-%m-%d')}")
        if status:
            where_clauses.append(f"status={status}")

        if where_clauses:
            params["where"] = " AND ".join(where_clauses)

        return self.request("tasks", params)

    def test_connection(self) -> bool:
        """
        Test the API connection with a simple request.

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            response = self.request("invoices", {"page": 1})
            # If we get here without an exception, it worked
            return True
        except Exception as e:
            print(f"Connection test failed: {e}")
            return False


def create_connector() -> AroFloConnector:
    """
    Factory function to create an AroFloConnector with environment credentials.

    Returns:
        Configured AroFloConnector instance
    """
    return AroFloConnector(host_ip=AROFLO_HOST_IP if AROFLO_HOST_IP else None)


if __name__ == "__main__":
    # Test the connector
    connector = create_connector()

    print("Testing AroFlo API connection...")
    if connector.test_connection():
        print("Connection successful!")
    else:
        print("Connection failed. Please check your credentials.")
