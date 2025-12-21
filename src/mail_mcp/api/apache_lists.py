#
# Copyright 2025 The Apache Software Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Client for Apache Lists (Pony Mail) API."""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import structlog

logger = structlog.get_logger(__name__)

STATS_API_URL = "https://lists.apache.org/api/stats.lua"
USER_AGENT = "mail-mcp/1.0"


@dataclass
class MonthStats:
    """Statistics for a single month."""

    year_month: str
    message_count: int


@dataclass
class ListStats:
    """Statistics for a mailing list."""

    list_name: str
    first_year: int
    last_year: int
    first_month: int
    last_month: int
    months: dict[str, int]  # year_month -> message_count


class ApacheListsClient:
    """Client for Apache Lists (Pony Mail) stats API."""

    def __init__(self, timeout: float = 30.0):
        """
        Initialize the client.

        Args:
            timeout: Request timeout in seconds
        """
        self.timeout = timeout

    def _parse_list_address(self, list_addr: str) -> tuple[str, str]:
        """
        Parse list address into list name and domain.

        Args:
            list_addr: Full list address (e.g., "dev@maven.apache.org")

        Returns:
            Tuple of (list_name, domain)
        """
        if "@" not in list_addr:
            raise ValueError(f"Invalid list address: {list_addr}")

        parts = list_addr.split("@")
        return parts[0], parts[1]

    async def get_stats(self, list_addr: str) -> ListStats:
        """
        Get statistics for a mailing list.

        Args:
            list_addr: Mailing list address (e.g., "dev@maven.apache.org")

        Returns:
            ListStats with message counts per month

        Raises:
            httpx.HTTPError: If API request fails
            ValueError: If response is invalid
        """
        list_name, domain = self._parse_list_address(list_addr)

        params = {"list": list_name, "domain": domain}
        url = f"{STATS_API_URL}?{urlencode(params)}"

        logger.debug("fetching_stats", list=list_addr, url=url)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()

            data = response.json()

        if not data:
            raise ValueError(f"Empty response from stats API for {list_addr}")

        # Parse the response
        active_months = data.get("active_months", {})

        stats = ListStats(
            list_name=list_addr,
            first_year=data.get("firstYear", 0),
            last_year=data.get("lastYear", 0),
            first_month=data.get("firstMonth", 0),
            last_month=data.get("lastMonth", 0),
            months=active_months
        )

        logger.info(
            "stats_fetched",
            list=list_addr,
            total_months=len(active_months),
            first_year=stats.first_year,
            last_year=stats.last_year
        )

        return stats

    async def get_month_count(self, list_addr: str, year_month: str) -> int:
        """
        Get message count for a specific month.

        Args:
            list_addr: Mailing list address
            year_month: Month in yyyy-mm format

        Returns:
            Message count for that month, or 0 if not found
        """
        stats = await self.get_stats(list_addr)
        return stats.months.get(year_month, 0)

    async def get_months_to_check(
        self,
        list_addr: str,
        current_month: str,
        previous_month: str
    ) -> list[MonthStats]:
        """
        Get stats for current and previous month.

        Args:
            list_addr: Mailing list address
            current_month: Current month in yyyy-mm format
            previous_month: Previous month in yyyy-mm format

        Returns:
            List of MonthStats for both months
        """
        stats = await self.get_stats(list_addr)

        result = []
        for year_month in [previous_month, current_month]:
            count = stats.months.get(year_month, 0)
            result.append(MonthStats(year_month=year_month, message_count=count))

        return result
