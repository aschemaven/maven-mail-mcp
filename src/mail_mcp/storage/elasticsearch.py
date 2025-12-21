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

"""Elasticsearch client wrapper for mail archive storage."""

from datetime import datetime

import structlog
from elasticsearch import AsyncElasticsearch, NotFoundError
from elasticsearch.helpers import async_bulk

from mail_mcp.config import settings
from mail_mcp.storage.schema import get_index_config, get_index_name

logger = structlog.get_logger(__name__)


class ElasticsearchClient:
    """Async Elasticsearch client for mail archive operations."""

    def __init__(self, url: str | None = None):
        """
        Initialize Elasticsearch client.

        Args:
            url: Elasticsearch URL (defaults to settings)
        """
        self.url = url or settings.elasticsearch_url
        self._client: AsyncElasticsearch | None = None
        logger.info("elasticsearch_client_initialized", url=self.url)

    async def connect(self) -> None:
        """Establish connection to Elasticsearch."""
        self._client = AsyncElasticsearch(
            hosts=[self.url],
            request_timeout=settings.elasticsearch_timeout,
            max_retries=settings.elasticsearch_max_retries,
            retry_on_timeout=True
        )
        logger.info("elasticsearch_connected", url=self.url)

    async def close(self) -> None:
        """Close Elasticsearch connection."""
        if self._client:
            await self._client.close()
            logger.info("elasticsearch_disconnected")

    async def health(self) -> dict:
        """
        Get cluster health status.

        Returns:
            Health information dictionary

        Raises:
            Exception: If client not connected or health check fails
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        health = await self._client.cluster.health()
        logger.debug("elasticsearch_health_check", status=health["status"])
        return health

    async def create_index(self, list_name: str) -> bool:
        """
        Create index for a mailing list if it doesn't exist.

        Args:
            list_name: Mailing list address (e.g., "dev@maven.apache.org")

        Returns:
            True if index was created, False if it already existed

        Raises:
            Exception: If index creation fails
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        config = get_index_config(settings.elasticsearch_index_prefix, list_name)
        index_name = config["index"]

        # Check if index already exists
        exists = await self._client.indices.exists(index=index_name)
        if exists:
            logger.info("index_already_exists", index=index_name)
            return False

        # Create index
        await self._client.indices.create(
            index=index_name,
            settings=config["settings"],
            mappings=config["mappings"]
        )
        logger.info("index_created", index=index_name, list=list_name)
        return True

    async def index_exists(self, list_name: str) -> bool:
        """
        Check if index exists for a mailing list.

        Args:
            list_name: Mailing list address

        Returns:
            True if index exists, False otherwise
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        index_name = get_index_name(settings.elasticsearch_index_prefix, list_name)
        return await self._client.indices.exists(index=index_name)

    async def list_indices(self) -> list[dict]:
        """
        List all mailing list indices with document counts.

        Returns:
            List of dicts with index info: name, list_name, doc_count
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        prefix = settings.elasticsearch_index_prefix
        pattern = f"{prefix}-*"

        # Get index stats
        try:
            stats = await self._client.indices.stats(index=pattern)
        except Exception as e:
            logger.warning("list_indices_failed", error=str(e))
            return []

        indices = []
        for index_name, index_stats in stats.get("indices", {}).items():
            # Extract list name from index name (e.g., "maven-dev" -> "dev")
            if index_name.startswith(f"{prefix}-"):
                list_part = index_name[len(prefix) + 1:]
                # Reconstruct full list name
                list_name = f"{list_part}@maven.apache.org"
                doc_count = index_stats.get("primaries", {}).get("docs", {}).get("count", 0)

                indices.append({
                    "index": index_name,
                    "list_name": list_name,
                    "doc_count": doc_count
                })

        # Sort by list name
        indices.sort(key=lambda x: x["list_name"])
        return indices

    async def index_document(
        self,
        list_name: str,
        message_id: str,
        document: dict
    ) -> str:
        """
        Index a single email document.

        Args:
            list_name: Mailing list address
            message_id: Email message ID (used as document ID)
            document: Email document to index

        Returns:
            Document ID

        Raises:
            Exception: If indexing fails
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        index_name = get_index_name(settings.elasticsearch_index_prefix, list_name)

        result = await self._client.index(
            index=index_name,
            id=message_id,
            document=document
        )

        logger.debug(
            "document_indexed",
            index=index_name,
            doc_id=message_id,
            result=result["result"]
        )
        return result["_id"]

    async def bulk_index(
        self,
        list_name: str,
        documents: list[dict]
    ) -> tuple[int, list]:
        """
        Bulk index multiple email documents.

        Args:
            list_name: Mailing list address
            documents: List of documents, each must have 'message_id' field

        Returns:
            Tuple of (success_count, errors)

        Raises:
            Exception: If bulk indexing fails
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        index_name = get_index_name(settings.elasticsearch_index_prefix, list_name)

        # Prepare actions for bulk API
        actions = []
        for doc in documents:
            # Handle both formats: {"_id": ..., "_source": ...} or plain document
            if "_id" in doc and "_source" in doc:
                # Already in correct format
                action = {
                    "_index": index_name,
                    "_id": doc["_id"],
                    "_source": doc["_source"]
                }
            else:
                # Plain document format
                action = {
                    "_index": index_name,
                    "_id": doc["message_id"],
                    "_source": doc
                }
            actions.append(action)

        success, errors = await async_bulk(
            self._client,
            actions,
            raise_on_error=False,
            raise_on_exception=False
        )

        logger.info(
            "bulk_index_completed",
            index=index_name,
            success=success,
            errors=len(errors)
        )

        return success, errors

    async def get_document(self, list_name: str, message_id: str) -> dict | None:
        """
        Retrieve a document by message ID.

        Args:
            list_name: Mailing list address
            message_id: Email message ID

        Returns:
            Document if found, None otherwise
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        index_name = get_index_name(settings.elasticsearch_index_prefix, list_name)

        try:
            result = await self._client.get(index=index_name, id=message_id)
            return result["_source"]
        except NotFoundError:
            logger.debug("document_not_found", index=index_name, message_id=message_id)
            return None

    async def update_document(
        self,
        list_name: str,
        message_id: str,
        partial_doc: dict
    ) -> bool:
        """
        Partially update a document.

        Args:
            list_name: Mailing list address
            message_id: Email message ID (document ID)
            partial_doc: Fields to update

        Returns:
            True if update was successful, False if document not found

        Raises:
            Exception: If update fails for reasons other than not found
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        index_name = get_index_name(settings.elasticsearch_index_prefix, list_name)

        try:
            await self._client.update(
                index=index_name,
                id=message_id,
                doc=partial_doc
            )
            logger.debug(
                "document_updated",
                index=index_name,
                message_id=message_id,
                fields=list(partial_doc.keys())
            )
            return True
        except NotFoundError:
            logger.debug(
                "document_not_found_for_update",
                index=index_name,
                message_id=message_id
            )
            return False

    async def search(
        self,
        list_name: str,
        query: dict,
        size: int = 10,
        from_: int = 0,
        sort: list | None = None
    ) -> dict:
        """
        Execute a search query.

        Args:
            list_name: Mailing list address
            query: Elasticsearch query DSL
            size: Number of results to return
            from_: Offset for pagination
            sort: Optional sort specification (e.g., [{"date": {"order": "desc"}}])

        Returns:
            Search results

        Raises:
            Exception: If search fails
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        index_name = get_index_name(settings.elasticsearch_index_prefix, list_name)

        search_params = {
            "index": index_name,
            "query": query,
            "size": size,
            "from_": from_
        }

        if sort:
            search_params["sort"] = sort

        result = await self._client.search(**search_params)

        logger.debug(
            "search_executed",
            index=index_name,
            hits=result["hits"]["total"]["value"]
        )

        return result

    async def count_by_month(
        self,
        list_name: str,
        year_month: str
    ) -> int:
        """
        Count documents for a specific month.

        Args:
            list_name: Mailing list address
            year_month: Month in yyyy-mm format (e.g., "2025-12")

        Returns:
            Number of documents for that month
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        index_name = get_index_name(settings.elasticsearch_index_prefix, list_name)

        # Check if index exists first
        exists = await self._client.indices.exists(index=index_name)
        if not exists:
            return 0

        # Parse year-month and create date range
        year, month = map(int, year_month.split("-"))
        # Calculate next month for range query
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1

        start_date = f"{year:04d}-{month:02d}-01"
        end_date = f"{next_year:04d}-{next_month:02d}-01"

        query = {
            "range": {
                "date": {
                    "gte": start_date,
                    "lt": end_date
                }
            }
        }

        result = await self._client.count(index=index_name, query=query)
        count = result.get("count", 0)

        logger.debug(
            "count_by_month",
            index=index_name,
            year_month=year_month,
            count=count
        )

        return count

    async def get_month_status(
        self,
        list_name: str,
        year_month: str
    ) -> dict | None:
        """
        Get completion status for a specific month.

        Args:
            list_name: Mailing list address
            year_month: Month in yyyy-mm format

        Returns:
            Status dict with 'complete', 'expected_count', 'checked_at' or None
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        meta_index = f"{settings.elasticsearch_index_prefix}-meta"

        # Check if meta index exists
        exists = await self._client.indices.exists(index=meta_index)
        if not exists:
            return None

        doc_id = f"{list_name}:{year_month}"

        try:
            result = await self._client.get(index=meta_index, id=doc_id)
            return result["_source"]
        except NotFoundError:
            return None

    async def mark_month_complete(
        self,
        list_name: str,
        year_month: str,
        expected_count: int
    ) -> None:
        """
        Mark a month as complete (no more emails expected).

        Args:
            list_name: Mailing list address
            year_month: Month in yyyy-mm format
            expected_count: Expected message count from stats API
        """
        if not self._client:
            raise RuntimeError("Client not connected. Call connect() first.")

        meta_index = f"{settings.elasticsearch_index_prefix}-meta"

        # Ensure meta index exists with minimal mapping
        exists = await self._client.indices.exists(index=meta_index)
        if not exists:
            await self._client.indices.create(
                index=meta_index,
                settings={"number_of_shards": 1, "number_of_replicas": 0},
                mappings={
                    "properties": {
                        "list_name": {"type": "keyword"},
                        "year_month": {"type": "keyword"},
                        "complete": {"type": "boolean"},
                        "expected_count": {"type": "integer"},
                        "checked_at": {"type": "date"}
                    }
                }
            )
            logger.info("meta_index_created", index=meta_index)

        doc_id = f"{list_name}:{year_month}"
        document = {
            "list_name": list_name,
            "year_month": year_month,
            "complete": True,
            "expected_count": expected_count,
            "checked_at": datetime.utcnow().isoformat()
        }

        await self._client.index(
            index=meta_index,
            id=doc_id,
            document=document
        )

        logger.info(
            "month_marked_complete",
            list=list_name,
            year_month=year_month,
            expected_count=expected_count
        )

    async def is_month_complete(
        self,
        list_name: str,
        year_month: str
    ) -> bool:
        """
        Check if a month is marked as complete.

        Args:
            list_name: Mailing list address
            year_month: Month in yyyy-mm format

        Returns:
            True if month is marked complete, False otherwise
        """
        status = await self.get_month_status(list_name, year_month)
        return status is not None and status.get("complete", False)

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
