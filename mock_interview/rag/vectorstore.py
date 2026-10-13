import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _dummy_mode():
    return bool(settings.MOCK_INTERVIEW.get("DUMMY_MODE", False))


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5 or 1.0
    nb = sum(y * y for y in b) ** 0.5 or 1.0
    return dot / (na * nb)


class _InMemoryVectorStore:
    """Minimal in-memory stand-in for Qdrant used in DUMMY_MODE."""

    def __init__(self):
        self._points = {}

    def upsert(self, chunk_ids, vectors, payloads):
        for index, chunk_id in enumerate(chunk_ids):
            self._points[str(chunk_id)] = {
                "vector": vectors[index],
                "payload": payloads[index],
            }

    def search(self, query_vector, top_k, metadata_filters):
        hits = []
        for point_id, point in self._points.items():
            payload = point["payload"] or {}
            if metadata_filters and any(
                value is not None and payload.get(key) != value
                for key, value in metadata_filters.items()
            ):
                continue
            hits.append(
                {
                    "id": point_id,
                    "score": _cosine(query_vector, point["vector"]),
                    "payload": payload,
                }
            )
        hits.sort(key=lambda item: item["score"], reverse=True)
        return hits[:top_k]

    def delete_by_document(self, document_id):
        for point_id in [
            point_id
            for point_id, point in self._points.items()
            if (point["payload"] or {}).get("document_id") == document_id
        ]:
            self._points.pop(point_id, None)

    def health(self):
        return True


class QdrantVectorStore:
    """Vector store for document embeddings.

    In normal mode this talks to Qdrant; in DUMMY_MODE it uses a process-local
    in-memory index so nothing external is required.
    """

    def __init__(self):
        self._dummy = _dummy_mode()
        config = settings.RAG_CONFIG
        self._collection = config["QDRANT_COLLECTION"]
        self._dimension = config["EMBEDDING_DIMENSION"]
        if self._dummy:
            self._client = _InMemoryVectorStore()
            logger.info("Using in-memory vector store (DUMMY_MODE).")
            return

        from qdrant_client import QdrantClient

        self._client = QdrantClient(url=config["QDRANT_URL"])
        self._ensure_collection()

    def _ensure_collection(self):
        from qdrant_client.models import Distance, VectorParams

        try:
            collections = self._client.get_collections().collections
            names = {c.name for c in collections}
            if self._collection not in names:
                self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=self._dimension,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("Created Qdrant collection: %s", self._collection)
        except Exception as exc:
            logger.warning("Could not ensure Qdrant collection: %s", exc)

    def upsert_chunks(self, chunk_ids, vectors, payloads):
        if self._dummy:
            self._client.upsert(chunk_ids, vectors, payloads)
            return
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=chunk_ids[i],
                vector=vectors[i],
                payload=payloads[i],
            )
            for i in range(len(chunk_ids))
        ]
        self._client.upsert(
            collection_name=self._collection,
            points=points,
        )

    def search(self, query_vector, top_k=5, metadata_filters=None):
        if self._dummy:
            return self._client.search(query_vector, top_k, metadata_filters)
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        if metadata_filters:
            conditions = []
            for key, value in metadata_filters.items():
                if value is not None:
                    conditions.append(
                        FieldCondition(
                            key=key,
                            match=MatchValue(value=value),
                        )
                    )
            if conditions:
                query_filter = Filter(must=conditions)

        results = self._client.query_points(
            collection_name=self._collection,
            query=query_vector,
            limit=top_k,
            query_filter=query_filter,
        )
        return [
            {
                "id": str(hit.id),
                "score": hit.score,
                "payload": hit.payload or {},
            }
            for hit in results.points
        ]

    def delete_by_document(self, document_id):
        if self._dummy:
            self._client.delete_by_document(document_id)
            return
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._client.delete(
            collection_name=self._collection,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="document_id",
                        match=MatchValue(value=document_id),
                    )
                ]
            ),
        )

    def health(self):
        if self._dummy:
            return True
        try:
            self._client.get_collections()
            return True
        except Exception:
            return False
