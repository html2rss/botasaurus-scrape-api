"""Main-document HTTP metadata for the browser tier."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DocumentResponse:
    """Main-frame document response captured from CDP ``Network.responseReceived``."""

    status_code: int
    headers: dict[str, str]
    url: str


@dataclass(frozen=True, slots=True)
class MetadataResult:
    status_code: int | None
    headers: dict[str, str] | None
    final_url: str
    metadata_error: str | None = None


def resolve_metadata(
    document: DocumentResponse | None, current_url: str | None, target_url: str
) -> MetadataResult:
    """Real status/headers when a document response was captured, else a 200 default."""
    final_url = current_url or (document.url if document else None) or target_url
    if document is None:
        return MetadataResult(status_code=200, headers=None, final_url=final_url)
    return MetadataResult(
        status_code=document.status_code, headers=document.headers, final_url=final_url
    )
