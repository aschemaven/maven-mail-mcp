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

"""MCP tool implementations for Maven mailing list operations."""

from datetime import datetime
from typing import Any

import structlog

from mail_mcp.config import settings
from mail_mcp.ponymail import PonymailResolver, get_archive_url
from mail_mcp.storage.elasticsearch import ElasticsearchClient

logger = structlog.get_logger(__name__)


def format_archive_url(source: dict) -> str | None:
    """
    Format archive URL from cached mid if available.

    Args:
        source: Document source from Elasticsearch

    Returns:
        Archive URL string if mid is cached, None otherwise
    """
    archive_mid = source.get("archive_mid")
    if archive_mid:
        return get_archive_url(archive_mid)
    return None


async def resolve_archive_url(
    source: dict,
    list_name: str,
    es_client: ElasticsearchClient
) -> str | None:
    """
    Resolve and cache archive URL for a message if not already cached.

    When settings.resolve_archive_urls is enabled, this function will:
    1. Check if archive_mid is already cached
    2. If not, query Pony Mail API to look up the mid
    3. Cache the result in Elasticsearch
    4. Return the archive URL

    Args:
        source: Document source from Elasticsearch
        list_name: Mailing list address
        es_client: Elasticsearch client for caching

    Returns:
        Archive URL string if resolved/cached, None otherwise
    """
    # First check if already cached
    archive_mid = source.get("archive_mid")
    if archive_mid:
        return get_archive_url(archive_mid)

    # Check if resolution is enabled
    if not settings.resolve_archive_urls:
        return None

    # Try to resolve via Pony Mail API
    resolver = PonymailResolver(es_client)

    # Parse date from source for search optimization
    date = None
    date_str = source.get("date")
    if date_str:
        try:
            # Handle ISO format date string
            if isinstance(date_str, str):
                date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    message_id = source.get("message_id")
    subject = source.get("subject")

    if not message_id:
        return None

    try:
        url = await resolver.resolve_url(
            message_id=message_id,
            list_name=list_name,
            date=date,
            subject=subject
        )
        if url:
            # Update the source dict so caller sees it
            source["archive_mid"] = url.split("/")[-1]  # Extract mid from URL
        return url
    except Exception as e:
        logger.warning(
            "archive_url_resolution_failed",
            message_id=message_id,
            error=str(e)
        )
        return None


# Global Elasticsearch client (will be initialized on first use)
_es_client: ElasticsearchClient | None = None


async def get_es_client() -> ElasticsearchClient:
    """
    Get or create Elasticsearch client.

    Returns:
        Connected Elasticsearch client
    """
    global _es_client

    if _es_client is None:
        _es_client = ElasticsearchClient(
            url=settings.elasticsearch_url
        )
        await _es_client.connect()
        logger.info("elasticsearch_client_connected", url=settings.elasticsearch_url)

    return _es_client


async def list_available_lists() -> str:
    """
    List all available mailing lists that have been indexed.

    Returns:
        Formatted list of available mailing lists with document counts
    """
    logger.info("list_available_lists_called")

    try:
        client = await get_es_client()
        indices = await client.list_indices()

        if not indices:
            return (
                "No mailing lists are currently indexed.\n\n"
                "To index a mailing list, use the CLI tools:\n"
                "1. retrieve-mbox --date YYYY-MM --list <list@domain>\n"
                "2. index-mbox --directory <path> --list <list@domain>"
            )

        output = ["Available Mailing Lists", "=" * 50, ""]

        total_docs = 0
        for idx in indices:
            list_name = idx["list_name"]
            doc_count = idx["doc_count"]
            total_docs += doc_count
            output.append(f"• {list_name}")
            output.append(f"  Documents: {doc_count:,}")
            output.append("")

        output.append("-" * 50)
        output.append(f"Total: {len(indices)} list(s), {total_docs:,} documents")
        output.append("")
        output.append("Use list_name parameter in other tools to query a specific list.")
        output.append("Example: search_emails(query='maven 4', list_name='users@maven.apache.org')")

        return "\n".join(output)

    except Exception as e:
        logger.error("list_available_lists_failed", error=str(e), exc_info=True)
        return f"Error listing available lists: {e}"


async def search_emails(
    query: str,
    list_name: str = "dev@maven.apache.org",
    from_address: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    has_jira: bool | None = None,
    has_vote: bool | None = None,
    size: int = 10,
    sort_by: str = "relevance",
) -> str:
    """
    Search Maven mailing list archives.

    Args:
        query: Search query (full-text search on subject and body)
        list_name: Mailing list to search (default: dev@maven.apache.org)
        from_address: Filter by sender email address (partial match supported)
        from_date: Start date filter (ISO format: YYYY-MM-DD)
        to_date: End date filter (ISO format: YYYY-MM-DD)
        has_jira: Filter for emails with JIRA references
        has_vote: Filter for emails with votes
        size: Maximum number of results (default: 10, max: 100)
        sort_by: "relevance" (default) ranks by match quality;
            "date" returns the most recent matches instead

    Returns:
        Formatted search results with message details
    """
    logger.info(
        "search_emails_called",
        query=query,
        list_name=list_name,
        from_address=from_address,
        from_date=from_date,
        to_date=to_date,
        has_jira=has_jira,
        has_vote=has_vote,
        size=size,
    )

    # Validate size
    size = min(size, 100)

    # Build Elasticsearch query
    must_conditions = [
        {
            "multi_match": {
                "query": query,
                "fields": ["subject^3", "body_effective^2", "body_full"],
                "type": "best_fields",
            }
        }
    ]

    # From address filter
    if from_address:
        must_conditions.append({
            "wildcard": {
                "from_address": f"*{from_address}*"
            }
        })

    # Date range filter
    if from_date or to_date:
        date_range: dict[str, Any] = {}
        if from_date:
            date_range["gte"] = from_date
        if to_date:
            date_range["lte"] = to_date
        must_conditions.append({"range": {"date": date_range}})

    # JIRA filter
    if has_jira is not None:
        if has_jira:
            must_conditions.append({"exists": {"field": "jira_references"}})
        else:
            must_conditions.append(
                {"bool": {"must_not": {"exists": {"field": "jira_references"}}}}
            )

    # Vote filter
    if has_vote is not None:
        must_conditions.append({"term": {"has_vote": has_vote}})

    # Build Elasticsearch query (just the query part, not full request body)
    es_query = {"bool": {"must": must_conditions}}

    client = await get_es_client()

    # Relevance by default. Sorting on date discards the score the multi_match
    # above computes, which turns every query into "the newest mail that
    # matches anything": with best_fields over ~140k documents almost
    # everything matches weakly, so different queries returned the same recent
    # threads and the hit count was always the track_total_hits cap.
    sort_spec = None if sort_by == "relevance" else [{"date": {"order": "desc"}}]

    try:
        # Note: client.search() will call get_index_name() internally
        results = await client.search(
            list_name,
            es_query,
            size=size,
            sort=sort_spec
        )
    except Exception as e:
        logger.error("search_failed", error=str(e), exc_info=True)
        return f"Error searching emails: {e}"

    # Format results
    hits = results.get("hits", {}).get("hits", [])
    total = results.get("hits", {}).get("total", {}).get("value", 0)

    if not hits:
        return f"No results found for query: {query}"

    output = [f"Found {total} results (showing {len(hits)}):\n"]

    for i, hit in enumerate(hits, 1):
        source = hit["_source"]
        output.append(f"\n--- Result {i} ---")
        output.append(f"Subject: {source.get('subject', 'N/A')}")
        output.append(
            f"From: {source.get('from_name', 'Unknown')} <{source.get('from_address', 'N/A')}>"
        )
        output.append(f"Date: {source.get('date', 'N/A')}")
        output.append(f"Message-ID: {source.get('message_id', 'N/A')}")

        # Archive URL (resolve on-demand if not cached)
        archive_url = await resolve_archive_url(source, list_name, client)
        if archive_url:
            output.append(f"Archive: {archive_url}")

        if source.get("jira_references"):
            output.append(f"JIRA: {', '.join(source['jira_references'])}")
        if source.get("github_pr_references"):
            output.append(f"GitHub PRs: {', '.join(source['github_pr_references'])}")
        if source.get("version_numbers"):
            output.append(f"Versions: {', '.join(source['version_numbers'])}")
        if source.get("has_vote"):
            output.append(f"Vote: {source.get('vote_value', 'yes')}")

        # Body preview (first 200 chars)
        body = source.get("body_effective", "")
        if body:
            preview = body[:200].replace("\n", " ").strip()
            if len(body) > 200:
                preview += "..."
            output.append(f"Preview: {preview}")

    return "\n".join(output)


async def get_message(
    message_id: str,
    list_name: str = "dev@maven.apache.org",
) -> str:
    """
    Retrieve a specific email message by Message-ID.

    Args:
        message_id: Message-ID to retrieve (with or without angle brackets)
        list_name: Mailing list name (default: dev@maven.apache.org)

    Returns:
        Full message details
    """
    logger.info("get_message_called", message_id=message_id, list_name=list_name)

    # Normalize message ID (ensure angle brackets)
    if not message_id.startswith("<"):
        message_id = f"<{message_id}>"

    client = await get_es_client()

    try:
        # client.get_document() expects list_name and will call get_index_name() internally
        # Returns the document source directly (not wrapped in _source)
        source = await client.get_document(list_name, message_id)
    except Exception as e:
        logger.error("get_message_failed", error=str(e), exc_info=True)
        return f"Error retrieving message: {e}"

    if not source:
        return f"Message not found: {message_id}"

    # Format message
    output = ["=== Email Message ===\n"]
    output.append(f"Message-ID: {source.get('message_id', 'N/A')}")

    # Archive URL (resolve on-demand if enabled, otherwise use cached)
    archive_url = await resolve_archive_url(source, list_name, client)
    if archive_url:
        output.append(f"Archive: {archive_url}")

    output.append(f"Subject: {source.get('subject', 'N/A')}")
    output.append(
        f"From: {source.get('from_name', 'Unknown')} <{source.get('from_address', 'N/A')}>"
    )
    output.append(f"Date: {source.get('date', 'N/A')}")

    if source.get("to"):
        output.append(f"To: {', '.join(source['to'])}")
    if source.get("cc"):
        output.append(f"Cc: {', '.join(source['cc'])}")

    output.append(f"\nList: {source.get('list_address', 'N/A')}")

    # Threading
    if source.get("in_reply_to"):
        output.append(f"In-Reply-To: {source['in_reply_to']}")
    if source.get("references"):
        output.append(f"References: {', '.join(source['references'][:3])}")

    # Metadata
    output.append("\n--- Metadata ---")
    if source.get("jira_references"):
        output.append(f"JIRA: {', '.join(source['jira_references'])}")
    if source.get("github_pr_references"):
        output.append(f"GitHub PRs: {', '.join(source['github_pr_references'])}")
    if source.get("github_commit_references"):
        output.append(f"GitHub Commits: {', '.join(source['github_commit_references'][:5])}")
    if source.get("version_numbers"):
        output.append(f"Versions: {', '.join(source['version_numbers'])}")
    if source.get("decision_keywords"):
        output.append(f"Decisions: {', '.join(source['decision_keywords'])}")
    if source.get("has_vote"):
        output.append(f"Vote: {source.get('vote_value', 'yes')}")

    output.append(f"\nQuoted Content: {source.get('quote_percentage', 0):.1%}")

    # Body
    output.append("\n--- Message Body ---")
    output.append(source.get("body_effective", source.get("body_full", "")))

    return "\n".join(output)


async def get_thread(
    message_id: str,
    list_name: str = "dev@maven.apache.org",
    max_messages: int = 50,
) -> str:
    """
    Retrieve an email thread containing the specified message.

    Args:
        message_id: Message-ID of any message in the thread
        list_name: Mailing list name (default: dev@maven.apache.org)
        max_messages: Maximum messages to retrieve (default: 50)

    Returns:
        Thread with all messages in chronological order
    """
    logger.info(
        "get_thread_called",
        message_id=message_id,
        list_name=list_name,
        max_messages=max_messages,
    )

    # Normalize message ID
    if not message_id.startswith("<"):
        message_id = f"<{message_id}>"

    client = await get_es_client()

    # First, get the original message to find thread root
    try:
        # client.get_document() expects list_name and will call get_index_name() internally
        # Returns the document source directly (not wrapped in _source)
        source = await client.get_document(list_name, message_id)
        if not source:
            return f"Message not found: {message_id}"
    except Exception as e:
        return f"Error retrieving message: {e}"

    # Find thread root (either this message or walk up references)
    thread_root = message_id
    if source.get("in_reply_to"):
        # This is a reply, try to find the root
        references = source.get("references", [])
        if references:
            thread_root = references[0]  # First reference is usually the thread root

    # Search for all messages in thread
    # Messages in same thread either:
    # 1. Reference the root message
    # 2. Are referenced by the root message
    # 3. Share the same references
    query = {
        "bool": {
            "should": [
                {"term": {"message_id": thread_root}},
                {"term": {"in_reply_to": thread_root}},
                {"term": {"references": thread_root}},
                {"term": {"references": message_id}},
            ]
        }
    }

    try:
        # client.search() will call get_index_name() internally
        # Sort by date ascending for chronological thread order
        results = await client.search(
            list_name,
            query,
            size=max_messages,
            sort=[{"date": {"order": "asc"}}]
        )
    except Exception as e:
        return f"Error searching thread: {e}"

    hits = results.get("hits", {}).get("hits", [])

    if not hits:
        # No thread found, return just the single message
        return await get_message(message_id, list_name)

    # Format thread
    output = [f"=== Email Thread ({len(hits)} messages) ===\n"]

    for i, hit in enumerate(hits, 1):
        source = hit["_source"]
        output.append(f"\n--- Message {i} ---")
        output.append(f"Message-ID: {source.get('message_id', 'N/A')}")

        # Archive URL (resolve on-demand if enabled, otherwise use cached)
        archive_url = await resolve_archive_url(source, list_name, client)
        if archive_url:
            output.append(f"Archive: {archive_url}")

        output.append(f"Subject: {source.get('subject', 'N/A')}")
        output.append(
            f"From: {source.get('from_name', 'Unknown')} <{source.get('from_address', 'N/A')}>"
        )
        output.append(f"Date: {source.get('date', 'N/A')}")

        if source.get("in_reply_to"):
            output.append(f"In-Reply-To: {source['in_reply_to']}")

        # Metadata highlights
        if source.get("jira_references"):
            output.append(f"JIRA: {', '.join(source['jira_references'])}")
        if source.get("has_vote"):
            output.append(f"Vote: {source.get('vote_value', 'yes')}")

        # Body preview
        body = source.get("body_effective", "")
        if body:
            preview = body[:300].replace("\n", " ").strip()
            if len(body) > 300:
                preview += "..."
            output.append(f"\n{preview}")

    return "\n".join(output)


async def search_by_contributor(
    contributor: str,
    list_name: str = "dev@maven.apache.org",
    from_date: str | None = None,
    to_date: str | None = None,
    size: int = 20,
) -> str:
    """
    Find emails from a specific contributor.

    Args:
        contributor: Email address or name of contributor (partial match supported)
        list_name: Mailing list name (default: dev@maven.apache.org)
        from_date: Start date filter (ISO format: YYYY-MM-DD)
        to_date: End date filter (ISO format: YYYY-MM-DD)
        size: Maximum number of results (default: 20)

    Returns:
        List of emails from the contributor
    """
    logger.info(
        "search_by_contributor_called",
        contributor=contributor,
        list_name=list_name,
        from_date=from_date,
        to_date=to_date,
        size=size,
    )

    # Build query
    must_conditions = []

    # Contributor can match either email address or name
    # Use case_insensitive for keyword fields to match regardless of case
    contributor_pattern = f"*{contributor}*"
    must_conditions.append({
        "bool": {
            "should": [
                {"wildcard": {
                    "from_address": {"value": contributor_pattern, "case_insensitive": True}
                }},
                {"wildcard": {
                    "from_name.keyword": {"value": contributor_pattern, "case_insensitive": True}
                }},
            ],
            "minimum_should_match": 1
        }
    })

    # Date range filter
    if from_date or to_date:
        date_range: dict[str, Any] = {}
        if from_date:
            date_range["gte"] = from_date
        if to_date:
            date_range["lte"] = to_date
        must_conditions.append({"range": {"date": date_range}})

    # Build Elasticsearch query (just the query part)
    query = {"bool": {"must": must_conditions}}

    client = await get_es_client()

    try:
        # client.search() will call get_index_name() internally
        # Sort by date descending to get most recent emails first
        results = await client.search(
            list_name,
            query,
            size=min(size, 100),
            sort=[{"date": {"order": "desc"}}]
        )
    except Exception as e:
        return f"Error searching for contributor: {e}"

    hits = results.get("hits", {}).get("hits", [])
    total = results.get("hits", {}).get("total", {}).get("value", 0)

    if not hits:
        return f"No emails found from contributor: {contributor}"

    output = [f"Found {total} emails from {contributor} (showing {len(hits)}):\n"]

    for i, hit in enumerate(hits, 1):
        source = hit["_source"]
        output.append(f"\n--- Email {i} ---")
        output.append(f"Subject: {source.get('subject', 'N/A')}")
        output.append(
            f"From: {source.get('from_name', 'Unknown')} <{source.get('from_address', 'N/A')}>"
        )
        output.append(f"Date: {source.get('date', 'N/A')}")
        output.append(f"Message-ID: {source.get('message_id', 'N/A')}")

        # Archive URL (resolve on-demand if not cached)
        archive_url = await resolve_archive_url(source, list_name, client)
        if archive_url:
            output.append(f"Archive: {archive_url}")

        if source.get("jira_references"):
            output.append(f"JIRA: {', '.join(source['jira_references'])}")
        if source.get("has_vote"):
            output.append(f"Vote: {source.get('vote_value', 'yes')}")

        # Body preview
        body = source.get("body_effective", "")
        if body:
            preview = body[:200].replace("\n", " ").strip()
            if len(body) > 200:
                preview += "..."
            output.append(f"Preview: {preview}")

    return "\n".join(output)


async def find_references(
    reference: str,
    reference_type: str = "jira",
    list_name: str = "dev@maven.apache.org",
    size: int = 20,
) -> str:
    """
    Find emails referencing a specific JIRA issue or GitHub PR.

    Args:
        reference: Reference to search for (e.g., "MNG-1234" or "567")
        reference_type: Type of reference ("jira" or "github_pr")
        list_name: Mailing list name (default: dev@maven.apache.org)
        size: Maximum number of results (default: 20)

    Returns:
        List of emails referencing the specified item
    """
    logger.info(
        "find_references_called",
        reference=reference,
        reference_type=reference_type,
        list_name=list_name,
        size=size,
    )

    # Build query based on reference type
    if reference_type == "jira":
        field = "jira_references"
    elif reference_type == "github_pr":
        field = "github_pr_references"
    else:
        return f"Invalid reference_type: {reference_type}. Use 'jira' or 'github_pr'"

    # Build Elasticsearch query (just the query part)
    query = {"term": {field: reference}}

    client = await get_es_client()

    try:
        # client.search() will call get_index_name() internally
        # Sort by date descending to get most recent emails first
        results = await client.search(
            list_name,
            query,
            size=min(size, 100),
            sort=[{"date": {"order": "desc"}}]
        )
    except Exception as e:
        return f"Error searching references: {e}"

    hits = results.get("hits", {}).get("hits", [])
    total = results.get("hits", {}).get("total", {}).get("value", 0)

    if not hits:
        return f"No emails found referencing {reference}"

    output = [f"Found {total} emails referencing {reference} (showing {len(hits)}):\n"]

    for i, hit in enumerate(hits, 1):
        source = hit["_source"]
        output.append(f"\n--- Email {i} ---")
        output.append(f"Subject: {source.get('subject', 'N/A')}")
        output.append(
            f"From: {source.get('from_name', 'Unknown')} <{source.get('from_address', 'N/A')}>"
        )
        output.append(f"Date: {source.get('date', 'N/A')}")
        output.append(f"Message-ID: {source.get('message_id', 'N/A')}")

        # Archive URL (resolve on-demand if not cached)
        archive_url = await resolve_archive_url(source, list_name, client)
        if archive_url:
            output.append(f"Archive: {archive_url}")

        # Show all references
        if source.get("jira_references"):
            output.append(f"JIRA: {', '.join(source['jira_references'])}")
        if source.get("github_pr_references"):
            output.append(f"GitHub PRs: {', '.join(source['github_pr_references'])}")

        # Body preview
        body = source.get("body_effective", "")
        if body:
            preview = body[:200].replace("\n", " ").strip()
            if len(body) > 200:
                preview += "..."
            output.append(f"Preview: {preview}")

    return "\n".join(output)
