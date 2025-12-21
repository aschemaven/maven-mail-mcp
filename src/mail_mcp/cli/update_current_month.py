#!/usr/bin/env python3
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

"""
Update mailing list archives with smart fetching.

This script uses the Apache Lists stats API to determine which months need
updating. It compares expected message counts with indexed counts and only
fetches data when there's a discrepancy.

Months are marked as "complete" when:
- The month is in the past (not the current month)
- The indexed count matches the expected count from the stats API

Usage:
    update-current-month [--list <list@domain>] [--data-dir <path>]
    update-current-month --all  # Update all configured lists

Environment variables:
    MAIL_MCP_MAILING_LISTS: Comma-separated list of mailing lists to update
                           (default: dev@maven.apache.org)
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
import structlog

from mail_mcp.api.apache_lists import ApacheListsClient
from mail_mcp.config import settings
from mail_mcp.indexing import EmailIndexer
from mail_mcp.storage.elasticsearch import ElasticsearchClient

# ---- Constants ----
DEFAULT_MAILING_LIST = "dev@maven.apache.org"
MBOX_API_URL = "https://lists.apache.org/api/mbox.lua"
USER_AGENT = "mail-mcp-updater/1.0"

logger = structlog.get_logger(__name__)


@dataclass
class MonthUpdateResult:
    """Result of updating a single month."""

    year_month: str
    action: str  # "skipped", "fetched", "up_to_date", "error"
    expected_count: int
    indexed_count: int
    message: str


def get_current_month() -> str:
    """Get the current year-month string in yyyy-mm format."""
    now = datetime.now()
    return f"{now.year:04d}-{now.month:02d}"


def get_previous_month() -> str:
    """Get the previous year-month string in yyyy-mm format."""
    now = datetime.now()
    if now.month == 1:
        return f"{now.year - 1:04d}-12"
    else:
        return f"{now.year:04d}-{now.month - 1:02d}"


def download_mbox(list_addr: str, date_str: str, output_path: Path) -> bool:
    """
    Download mbox file from Apache mailing list API.

    Args:
        list_addr: Mailing list address (e.g., dev@maven.apache.org)
        date_str: Date in yyyy-mm format
        output_path: Destination file path

    Returns:
        True if download successful, False otherwise
    """
    params = {"list": list_addr, "date": date_str}
    url = f"{MBOX_API_URL}?{urlencode(params)}"

    logger.info("downloading_mbox", list=list_addr, date=date_str, url=url)

    tmp_path = output_path.with_suffix('.mbox.tmp')

    try:
        with httpx.Client(follow_redirects=True, timeout=60.0) as client:
            response = client.get(url, headers={"User-Agent": USER_AGENT})

            if response.status_code >= 400:
                logger.error(
                    "download_failed",
                    url=url,
                    status_code=response.status_code
                )
                return False

            # Write to temporary file
            tmp_path.write_bytes(response.content)

            # Atomic move to final location
            tmp_path.replace(output_path)

            logger.info(
                "download_complete",
                file=str(output_path),
                size_bytes=len(response.content)
            )
            return True

    except httpx.HTTPError as e:
        tmp_path.unlink(missing_ok=True)
        logger.error("download_http_error", url=url, error=str(e))
        return False
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        logger.error("download_io_error", error=str(e))
        return False


async def index_mbox(
    mbox_path: Path,
    list_name: str,
    es_client: ElasticsearchClient
) -> bool:
    """
    Index a single mbox file into Elasticsearch.

    Args:
        mbox_path: Path to the mbox file
        list_name: Name of the mailing list
        es_client: Connected Elasticsearch client

    Returns:
        True if indexing successful, False otherwise
    """
    indexer = EmailIndexer(
        es_client=es_client,
        index_prefix=settings.elasticsearch_index_prefix,
        batch_size=100
    )

    try:
        logger.info("indexing_file", file=str(mbox_path), list_name=list_name)
        stats = await indexer.index_mbox_file(
            mbox_path=mbox_path,
            list_name=list_name,
            create_index=True
        )

        logger.info(
            "indexing_complete",
            indexed=stats.get("indexed", 0),
            errors=stats.get("errors", 0)
        )

        return stats.get("errors", 0) == 0

    except Exception as e:
        logger.error("indexing_failed", error=str(e), exc_info=True)
        return False


async def update_month(
    list_addr: str,
    year_month: str,
    expected_count: int,
    data_dir: Path,
    list_subdir: str,
    es_client: ElasticsearchClient,
    is_current_month: bool
) -> MonthUpdateResult:
    """
    Update a single month if needed.

    Args:
        list_addr: Mailing list address
        year_month: Month in yyyy-mm format
        expected_count: Expected message count from stats API
        data_dir: Base data directory
        list_subdir: Subdirectory for this list
        es_client: Connected Elasticsearch client
        is_current_month: Whether this is the current month

    Returns:
        MonthUpdateResult with outcome details
    """
    # Check if month is already marked complete (skip for current month)
    if not is_current_month:
        is_complete = await es_client.is_month_complete(list_addr, year_month)
        if is_complete:
            logger.info(
                "month_already_complete",
                list=list_addr,
                year_month=year_month
            )
            return MonthUpdateResult(
                year_month=year_month,
                action="skipped",
                expected_count=expected_count,
                indexed_count=-1,  # Not checked
                message="Month already marked complete"
            )

    # Get current indexed count
    indexed_count = await es_client.count_by_month(list_addr, year_month)

    logger.info(
        "comparing_counts",
        list=list_addr,
        year_month=year_month,
        expected=expected_count,
        indexed=indexed_count,
        is_current=is_current_month
    )

    # Check if update is needed
    if indexed_count >= expected_count and expected_count > 0:
        # Counts match (or we have more than expected - shouldn't happen)
        if not is_current_month:
            # Mark past months as complete
            await es_client.mark_month_complete(
                list_addr, year_month, expected_count
            )
            return MonthUpdateResult(
                year_month=year_month,
                action="up_to_date",
                expected_count=expected_count,
                indexed_count=indexed_count,
                message="Counts match, marked as complete"
            )
        else:
            return MonthUpdateResult(
                year_month=year_month,
                action="up_to_date",
                expected_count=expected_count,
                indexed_count=indexed_count,
                message="Current month up to date"
            )

    # Need to fetch and index
    list_dir = data_dir / list_subdir
    list_dir.mkdir(parents=True, exist_ok=True)

    mbox_filename = f"{year_month}.mbox"
    mbox_path = list_dir / mbox_filename

    # Download mbox
    if not download_mbox(list_addr, year_month, mbox_path):
        return MonthUpdateResult(
            year_month=year_month,
            action="error",
            expected_count=expected_count,
            indexed_count=indexed_count,
            message="Download failed"
        )

    # Index into Elasticsearch
    if not await index_mbox(mbox_path, list_addr, es_client):
        return MonthUpdateResult(
            year_month=year_month,
            action="error",
            expected_count=expected_count,
            indexed_count=indexed_count,
            message="Indexing failed"
        )

    # Verify new count
    new_indexed_count = await es_client.count_by_month(list_addr, year_month)

    # Mark complete if past month and counts now match
    if not is_current_month and new_indexed_count >= expected_count:
        await es_client.mark_month_complete(
            list_addr, year_month, expected_count
        )
        return MonthUpdateResult(
            year_month=year_month,
            action="fetched",
            expected_count=expected_count,
            indexed_count=new_indexed_count,
            message="Fetched, indexed, and marked complete"
        )

    return MonthUpdateResult(
        year_month=year_month,
        action="fetched",
        expected_count=expected_count,
        indexed_count=new_indexed_count,
        message="Fetched and indexed"
    )


async def smart_update_list(
    list_addr: str,
    data_dir: Path,
    list_subdir: str
) -> int:
    """
    Perform smart update for a mailing list.

    Checks stats API and only fetches months that need updating.

    Args:
        list_addr: Mailing list address
        data_dir: Base data directory
        list_subdir: Subdirectory for this list

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    current_month = get_current_month()
    previous_month = get_previous_month()

    logger.info(
        "smart_update_starting",
        list=list_addr,
        current_month=current_month,
        previous_month=previous_month,
        data_dir=str(data_dir)
    )

    # Fetch stats from Apache Lists API
    stats_client = ApacheListsClient()
    try:
        stats = await stats_client.get_stats(list_addr)
    except Exception as e:
        logger.error("stats_fetch_failed", list=list_addr, error=str(e))
        return 1

    # Get expected counts
    current_expected = stats.months.get(current_month, 0)
    previous_expected = stats.months.get(previous_month, 0)

    logger.info(
        "expected_counts",
        list=list_addr,
        current_month=current_month,
        current_expected=current_expected,
        previous_month=previous_month,
        previous_expected=previous_expected
    )

    # Connect to Elasticsearch
    es_client = ElasticsearchClient(url=settings.elasticsearch_url)
    try:
        await es_client.connect()
    except Exception as e:
        logger.error("elasticsearch_connection_failed", error=str(e))
        return 1

    results = []
    has_error = False

    try:
        # Update previous month first (might be newly complete)
        if previous_expected > 0:
            result = await update_month(
                list_addr=list_addr,
                year_month=previous_month,
                expected_count=previous_expected,
                data_dir=data_dir,
                list_subdir=list_subdir,
                es_client=es_client,
                is_current_month=False
            )
            results.append(result)
            if result.action == "error":
                has_error = True

        # Update current month
        if current_expected > 0:
            result = await update_month(
                list_addr=list_addr,
                year_month=current_month,
                expected_count=current_expected,
                data_dir=data_dir,
                list_subdir=list_subdir,
                es_client=es_client,
                is_current_month=True
            )
            results.append(result)
            if result.action == "error":
                has_error = True

    finally:
        await es_client.close()

    # Log summary
    for result in results:
        logger.info(
            "month_update_result",
            year_month=result.year_month,
            action=result.action,
            expected=result.expected_count,
            indexed=result.indexed_count,
            message=result.message
        )

    logger.info(
        "smart_update_complete",
        list=list_addr,
        months_processed=len(results),
        has_error=has_error
    )

    return 1 if has_error else 0


def get_list_subdir(list_addr: str) -> str:
    """
    Get subdirectory name from list address.

    Args:
        list_addr: Mailing list address (e.g., dev@maven.apache.org)

    Returns:
        Subdirectory name (e.g., 'dev')
    """
    return list_addr.split("@")[0] if "@" in list_addr else list_addr


def get_configured_lists() -> list[str]:
    """
    Get list of mailing lists from configuration.

    Returns:
        List of mailing list addresses
    """
    lists_str = settings.mailing_lists
    return [lst.strip() for lst in lists_str.split(",") if lst.strip()]


async def update_all_lists(data_dir: Path) -> int:
    """
    Update all configured mailing lists.

    Args:
        data_dir: Base data directory

    Returns:
        Exit code (0 if all succeeded, 1 if any failed)
    """
    lists = get_configured_lists()
    logger.info("updating_all_lists", lists=lists, count=len(lists))

    failed = 0
    for list_addr in lists:
        list_subdir = get_list_subdir(list_addr)
        logger.info("updating_list", list=list_addr)

        result = await smart_update_list(list_addr, data_dir, list_subdir)
        if result != 0:
            failed += 1
            logger.error("list_update_failed", list=list_addr)

    logger.info(
        "all_lists_update_complete",
        total=len(lists),
        succeeded=len(lists) - failed,
        failed=failed
    )

    return 1 if failed > 0 else 0


def main() -> None:
    """Main entry point for update-current-month command."""
    parser = argparse.ArgumentParser(
        description="Update mailing list archives with smart fetching.",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--list",
        default=None,
        metavar="list@domain",
        help=f"Apache mailing list address (default: {DEFAULT_MAILING_LIST})"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="update_all",
        help="Update all configured mailing lists (from MAIL_MCP_MAILING_LISTS)"
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        metavar="PATH",
        help=f"Base data directory (default: {settings.data_path}, or MAIL_MCP_DATA_PATH)"
    )

    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else settings.data_path

    if args.update_all:
        # Update all configured lists
        exit_code = asyncio.run(update_all_lists(data_dir))
    else:
        # Update single list (default or specified)
        list_addr = args.list if args.list else DEFAULT_MAILING_LIST
        list_subdir = get_list_subdir(list_addr)
        exit_code = asyncio.run(
            smart_update_list(list_addr, data_dir, list_subdir)
        )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
