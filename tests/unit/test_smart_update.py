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

"""Unit tests for smart update logic."""

from datetime import datetime
from unittest.mock import patch

from mail_mcp.cli.update_current_month import (
    MonthUpdateResult,
    get_current_month,
    get_list_subdir,
    get_previous_month,
)


class TestDateHelpers:
    """Tests for date helper functions."""

    def test_get_current_month(self):
        """Test get_current_month returns correct format."""
        with patch("mail_mcp.cli.update_current_month.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2025, 12, 15)
            assert get_current_month() == "2025-12"

    def test_get_previous_month_normal(self):
        """Test get_previous_month for non-January months."""
        with patch("mail_mcp.cli.update_current_month.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2025, 12, 15)
            assert get_previous_month() == "2025-11"

    def test_get_previous_month_january(self):
        """Test get_previous_month for January (wraps to December of previous year)."""
        with patch("mail_mcp.cli.update_current_month.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2025, 1, 15)
            assert get_previous_month() == "2024-12"


class TestListSubdir:
    """Tests for list subdirectory extraction."""

    def test_get_list_subdir_with_at(self):
        """Test extracting subdir from full list address."""
        assert get_list_subdir("dev@maven.apache.org") == "dev"
        assert get_list_subdir("users@maven.apache.org") == "users"

    def test_get_list_subdir_without_at(self):
        """Test returning the input when no @ present."""
        assert get_list_subdir("dev") == "dev"


class TestMonthUpdateResult:
    """Tests for MonthUpdateResult dataclass."""

    def test_result_skipped(self):
        """Test creating a skipped result."""
        result = MonthUpdateResult(
            year_month="2025-11",
            action="skipped",
            expected_count=100,
            indexed_count=-1,
            message="Month already marked complete"
        )
        assert result.action == "skipped"
        assert result.indexed_count == -1

    def test_result_up_to_date(self):
        """Test creating an up-to-date result."""
        result = MonthUpdateResult(
            year_month="2025-12",
            action="up_to_date",
            expected_count=50,
            indexed_count=50,
            message="Counts match"
        )
        assert result.action == "up_to_date"
        assert result.expected_count == result.indexed_count

    def test_result_fetched(self):
        """Test creating a fetched result."""
        result = MonthUpdateResult(
            year_month="2025-12",
            action="fetched",
            expected_count=60,
            indexed_count=60,
            message="Fetched and indexed"
        )
        assert result.action == "fetched"

    def test_result_error(self):
        """Test creating an error result."""
        result = MonthUpdateResult(
            year_month="2025-12",
            action="error",
            expected_count=50,
            indexed_count=40,
            message="Download failed"
        )
        assert result.action == "error"
