import hashlib
import logging
import math

from django.conf import settings

logger = logging.getLogger(__name__)


def _dummy_mode():
    return bool(settings.MOCK_INTERVIEW.get("DUMMY_MODE", False))


class EmbeddingService:
    """Embedding service.

    In normal mode this wraps ``sentence-transformers`` (BAAI/bge-m3). In
    DUMMY_MODE it produces deterministic pseudo-embeddings so document
    ingestion and RAG retrieval work without downloading any model.
    """

    def __init__(self):
        config = settings.RAG_CONFIG
        self._dimension = config["EMBEDDING_DIMENSION"]
        self._model_name = config["EMBEDDING_MODEL"]
        self._dummy = _dummy_mode()
        self._model = None
        if not self._dummy:
            self._model = self._load_model()

    def _load_model(self):
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model: %s", self._model_name)
        return SentenceTransformer(self._model_name)

    def _pseudo_vector(self, text):
        digest = hashlib.sha256(str(text).encode("utf-8", errors="replace")).digest()
        values = []
        for index in range(self._dimension):
            byte = digest[index % len(digest)]
            values.append((byte / 255.0) * 2.0 - 1.0)
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]

    def embed_texts(self, texts):
        if self._dummy:
            return [self._pseudo_vector(text) for text in texts]
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, vector)) for vector in vectors]

    def embed_query(self, text):
        if self._dummy:
            return self._pseudo_vector(text)
        return [float(value) for value in self._model.encode([text], normalize_embeddings=True)[0]]

    @property
    def dimension(self):
        return self._dimension

    def health(self):
        if self._dummy:
            return True
        try:
            test = self.embed_texts(["test"])
            return len(test) > 0 and len(test[0]) == self._dimension
        except Exception:
            return False
