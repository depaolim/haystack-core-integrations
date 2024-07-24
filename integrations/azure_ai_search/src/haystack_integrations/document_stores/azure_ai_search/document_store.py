# SPDX-FileCopyrightText: 2023-present deepset GmbH <info@deepset.ai>
#
# SPDX-License-Identifier: Apache-2.0
import logging
import os
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
    VectorSearch,
    VectorSearchAlgorithmMetric,
    VectorSearchProfile,
)
from haystack import default_from_dict, default_to_dict
from haystack.dataclasses import Document
from haystack.document_stores.errors import DuplicateDocumentError
from haystack.document_stores.types import DuplicatePolicy
from haystack.utils import Secret, deserialize_secrets_inplace

type_mapping = {str: "Edm.String", bool: "Edm.Boolean", int: "Edm.Int32", float: "Edm.Double"}

MAX_UPLOAD_BATCH_SIZE = 1000

DEFAULT_VECTOR_SEARCH = VectorSearch(
    profiles=[
        VectorSearchProfile(name="default-vector-config", algorithm_configuration_name="cosine-algorithm-config")
    ],
    algorithms=[
        HnswAlgorithmConfiguration(
            name="cosine-algorithm-config",
            parameters=HnswParameters(
                metric=VectorSearchAlgorithmMetric.COSINE,
            ),
        )
    ],
)

logger = logging.getLogger(__name__)
logging.getLogger("azure").setLevel(logging.ERROR)


class AzureAISearchDocumentStore:
    def __init__(
        self,
        *,
        api_key: Secret = Secret.from_env_var("AZURE_SEARCH_API_KEY", strict=False),
        azure_endpoint: Secret = Secret.from_env_var("AZURE_SEARCH_SERVICE_ENDPOINT", strict=False),
        index_name: str = "default",
        embedding_dimension: int = 768,
        metadata_fields: Optional[Dict[str, type]] = None,
        vector_search_configuration: VectorSearch = None,
        create_index: bool = True,
        **kwargs,
    ):
        """
        A document store using [Azure AI Search](https://azure.microsoft.com/products/ai-services/ai-search/)
        as the backend.

        :param azure_endpoint: The URL endpoint of an Azure search service.
        :param api_key: The API key to use for authentication.
        :param index_name: Name of index in Azure AI Search, if it doesn't exist it will be created.
        :param embedding_dimension: Dimension of the embeddings.
        :param metadata_fields: A dictionary of metatada keys and their types to create 
        additional fields in index schema.
        :param vector_search_configuration: Configuration option related to vector search.
        Default configuration uses the HNSW algorithm with cosine similarity to handle vector searches.

        :param kwargs: Optional keyword parameters for Azure AI Search.
        Some of the supported parameters:
            - `api_version`: The Search API version to use for requests.
            - `audience`: sets the Audience to use for authentication with Azure Active Directory (AAD).
            The audience is not considered when using a shared key. If audience is not provided, the public cloud audience will be assumed.

        For more information on parameters, see the [official Azure AI Search documentation](https://learn.microsoft.com/en-us/azure/search/)
        """

        azure_endpoint = azure_endpoint or os.environ.get("AZURE_SEARCH_SERVICE_ENDPOINT")
        if not azure_endpoint:
            raise ValueError("Please provide an Azure endpoint or set the environment variable AZURE_OPENAI_ENDPOINT.")
        api_key = api_key or os.environ.get("AZURE_SEARCH_API_KEY")
        if not api_key:
            raise ValueError("Please provide an API key or an Azure Active Directory token.")

        self._client = None
        self._index_client = None
        self._index_fields = None  # stores all fields in the final schema of index
        self._api_key = api_key
        self._azure_endpoint = azure_endpoint
        self._index_name = index_name
        self._embedding_dimension = embedding_dimension
        self._metadata_fields = metadata_fields
        self._vector_search_configuration = vector_search_configuration or DEFAULT_VECTOR_SEARCH
        self._create_index = create_index
        self._kwargs = kwargs

    @property
    def client(self) -> SearchClient:

        if isinstance(self._azure_endpoint, Secret):
            self._azure_endpoint = self._azure_endpoint.resolve_value()
        if isinstance(self._api_key, Secret):
            self._api_key = self._api_key.resolve_value()

        if not self._index_client:
            self._index_client = SearchIndexClient(
                self._azure_endpoint, AzureKeyCredential(self._api_key), **self._kwargs
            )

        if not self.index_exists(self._index_name):
            # Handle the case where the index does not exist
            logger.debug(
                "The index '%s' does not exist. A new index will be created.",
                self._index_name,
            )
            self.create_index(self._index_name)

        self._client = self._index_client.get_search_client(self._index_name)
        return self._client

    def create_index(self, index_name: str, **kwargs) -> None:
        """
        Creates a new search index.
        :param index_name: Name of the index to create. If None, the index name from the constructor is used.
        :param kwargs: Optional keyword parameters.

        """

        # default fields to create index based on Haystack Document

        default_fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
            SearchableField(name="content", type=SearchFieldDataType.String),
            SearchField(
                name="embedding",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=self._embedding_dimension,
                vector_search_profile_name="default-vector-config",
            ),
        ]

        if not index_name:
            index_name = self._index_name
        fields = default_fields
        if self._metadata_fields:
            fields.extend(self._create_metadata_index_fields(self._metadata_fields))
        self._index_fields = fields
        index = SearchIndex(name=index_name, fields=fields, vector_search=self._vector_search_configuration, **kwargs)
        self._index_client.create_index(index)

    def to_dict(self) -> Dict[str, Any]:
        # This is not the best solution to serialise this class but is the fastest to implement.
        # Not all kwargs types can be serialised to text so this can fail. We must serialise each
        # type explicitly to handle this properly.
        """
        Serializes the component to a dictionary.

        :returns:
            Dictionary with serialized data.
        """
        return default_to_dict(
            self,
            azure_endpoint=self._azure_endpoint.to_dict() if self._azure_endpoint is not None else None,
            api_key=self._api_key.to_dict() if self._api_key is not None else None,
            index_name=self._index_name,
            create_index=self._create_index,
            embedding_dimension=self._embedding_dimension,
            metadata_fields=self._metadata_fields,
            vector_search_configuration=self._vector_search_configuration,
            **self._kwargs,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AzureAISearchDocumentStore":
        """
        Deserializes the component from a dictionary.

        :param data:
            Dictionary to deserialize from.

        :returns:
            Deserialized component.
        """

        deserialize_secrets_inplace(data["init_parameters"], keys=["api_key", "azure_endpoint"])
        return default_from_dict(cls, data)

    def count_documents(self, **kwargs: Any) -> int:
        """
        Returns how many documents are present in the search index.

        :param kwargs: additional keyword parameters.
        :returns: list of retrieved documents.
        """
        return self.client.get_document_count(**kwargs)

    def write_documents(self, documents: List[Document], policy: DuplicatePolicy = DuplicatePolicy.FAIL) -> int:
        """
        Writes the provided documents to search index.

        :param documents: documents to write to the index.
        :return: the number of documents added to index.
        """

        if len(documents) > 0:
            if not isinstance(documents[0], Document):
                msg = "param 'documents' must contain a list of objects of type Document"
                raise ValueError(msg)

        def _convert_input_document(documents: Document):
            document_dict = asdict(documents)
            if not isinstance(document_dict["id"], str):
                msg = f"Document id {document_dict['id']} is not a string, "
                raise Exception(msg)
            index_document = self._default_index_mapping(document_dict)
            return index_document

        documents_to_write = []
        for doc in documents:
            try:
                self.client.get_document(doc.id)
                if policy == DuplicatePolicy.SKIP:
                    logger.info(f"Document with ID {doc.id} already exists. Skipping.")
                    continue
                elif policy == DuplicatePolicy.FAIL:
                    raise DuplicateDocumentError(f"Document with ID {doc.id} already exists.")
                elif policy == DuplicatePolicy.OVERWRITE:
                    logger.info(f"Document with ID {doc.id} already exists. Overwriting.")
                    documents_to_write.append(_convert_input_document(doc))
            except ResourceNotFoundError:
                # Document does not exist, safe to add
                documents_to_write.append(_convert_input_document(doc))

        print (documents_to_write) 
        if documents_to_write != []:
            self.client.merge_or_upload_documents(documents_to_write)
        return len(documents_to_write)

    def delete_documents(self, document_ids: List[str]) -> None:
        """
        Deletes all documents with a matching document_ids from the search index.

        :param document_ids: ids of the documents to be deleted.
        """

        if self.count_documents == 0:
            return
        documents = self.get_documents(document_ids)
        if documents != []:
            self.client.delete_documents(documents)

    def get_documents(self, document_ids: List[str]):
        """
        Retrieves all documents with a matching document_ids from the document store.

        :param document_ids: ids of the documents to be retrieved.
        :returns: list of retrieved documents.
        """
        documents = []
        for doc_id in document_ids:
            try:
                document = self.client.get_document(doc_id)
                documents.append(document)
            except ResourceNotFoundError:
                logger.warning(f"Document with ID {doc_id} not found.")
        return documents

    def filter_documents(self, filters: Optional[Dict[str, Any]] = None) -> List[Document]:
        """
        Returns the documents that match the filters provided.

        For a detailed specification of the filters,
        refer to the [documentation](https://docs.haystack.deepset.ai/v2.0/docs/metadata-filtering)

        :param filters: The filters to apply to the document list.
        :returns: A list of Documents that match the given filters.
        """

        # Filters to be implemented with suitable interface for Azure AI Search
        azure_docs = []
        result = self.client.search(search_text="*", top=self.count_documents())
        for doc in result:
            azure_docs.append(doc)

        documents = self._convert_search_result_to_documents(azure_docs)
        return documents

    def _convert_search_result_to_documents(self, azure_docs: List[Dict[str, Any]]) -> List[Document]:

        ## UNDER PROGRESS
        documents = []
        for azure_doc in azure_docs:

            embedding = None
            if "embedding" in azure_doc and azure_doc["embedding"] != self._dummy_vector:
                embedding = azure_doc["embedding"]
            # meta = {key: value for key, value in azure_doc.items() if key not in ["id", "content", "embedding"]}
            doc = Document(
                id=azure_doc["id"],
                content=azure_doc["content"],
                embedding=embedding,
                # meta = meta
            )
            documents.append(doc)
        return documents

    def index_exists(self, index_name: Optional[str]) -> None:
        if self._index_client and index_name:
            return index_name in self._index_client.list_index_names()

    def _default_index_mapping(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """Map the document keys to fields of search index"""

        keys_to_remove = ["dataframe", "blob", "sparse_embedding", "score"]
        index_document = {k: v for k, v in document.items() if k not in keys_to_remove}

        metadata = index_document.pop("meta", None)
        for key, value in metadata.items():
            index_document[key] = value
        if index_document["embedding"] is None:
            self._dummy_vector = [-10.0] * self._embedding_dimension
            index_document["embedding"] = self._dummy_vector

        return index_document

    def _create_metadata_index_fields(self, metadata: Dict[str, Any]) -> List[SimpleField]:
        """Create a list of index fields for storing metadata values."""

        index_fields = []
        metadata_field_mapping = self._map_metadata_field_types(metadata)

        for key, field_type in metadata_field_mapping.items():
            index_fields.append(SimpleField(name=key, type=field_type, filterable=True))

        return index_fields

    def _map_metadata_field_types(self, metadata: Dict[str, type]) -> Dict[str, str]:
        """Map metadata field types to Azure Search field types."""
        metadata_field_mapping = {}

        for key, value_type in metadata.items():
            field_type = type_mapping.get(value_type)
            if not field_type:
                error_message = f"Unsupported field type for key '{key}': {value_type}"
                raise ValueError(error_message)
            metadata_field_mapping[key] = field_type

        return metadata_field_mapping