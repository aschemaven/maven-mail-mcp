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

"""Unit tests for Apache Lists API client."""

import pytest

from mail_mcp.api.apache_lists import ApacheListsClient, ListStats, MonthStats


class TestApacheListsClient:
    """Tests for ApacheListsClient."""

    def test_parse_list_address_valid(self):
        """Test parsing valid list addresses."""
        client = ApacheListsClient()

        list_name, domain = client._parse_list_address("dev@maven.apache.org")
        assert list_name == "dev"
        assert domain == "maven.apache.org"

        list_name, domain = client._parse_list_address("users@maven.apache.org")
        assert list_name == "users"
        assert domain == "maven.apache.org"

    def test_parse_list_address_invalid(self):
        """Test parsing invalid list addresses."""
        client = ApacheListsClient()

        with pytest.raises(ValueError, match="Invalid list address"):
            client._parse_list_address("invalid-address")


class TestListStats:
    """Tests for ListStats dataclass."""

    def test_list_stats_creation(self):
        """Test creating ListStats."""
        stats = ListStats(
            list_name="dev@maven.apache.org",
            first_year=2002,
            last_year=2025,
            first_month=7,
            last_month=12,
            months={"2025-11": 100, "2025-12": 50}
        )

        assert stats.list_name == "dev@maven.apache.org"
        assert stats.first_year == 2002
        assert stats.months["2025-11"] == 100
        assert stats.months.get("2024-01", 0) == 0


class TestMonthStats:
    """Tests for MonthStats dataclass."""

    def test_month_stats_creation(self):
        """Test creating MonthStats."""
        stats = MonthStats(year_month="2025-12", message_count=50)

        assert stats.year_month == "2025-12"
        assert stats.message_count == 50
