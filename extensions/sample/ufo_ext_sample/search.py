from dataclasses import dataclass

from ufo.sdk.search import FetchedPage, FetchRequest, SearchHit, SearchQuery, SearchResults

SEARCH_PROVIDER = "sample_search"
SAMPLE_SEARCH_URL = "https://sample.test/result"
SAMPLE_SEARCH_TITLE = "Sample result"
SAMPLE_SEARCH_TEXT = "the sample search backend answers a canned hit"
SAMPLE_SEARCH_ANSWER = "the sample search backend answers directly"
SAMPLE_FETCH_TEXT = "the sample search backend fetched a canned page"


@dataclass(frozen=True)
class SampleSearchProvider:
    """A trivial SearchProvider the probe registers through the `search_providers` Manifest point:
    `search` answers a canned SearchResults (carrying an `answer` to exercise that field) and
    `fetch` a canned FetchedPage. A real object consumed through the protocol, so a test drives it
    as core selects and the research tools call it; the Perplexity backend keeps its HTTP proof."""

    supports_fetch: bool = True

    async def search(self, query: SearchQuery) -> SearchResults:
        return SearchResults(
            hits=(
                SearchHit(
                    url=SAMPLE_SEARCH_URL, title=SAMPLE_SEARCH_TITLE, text=SAMPLE_SEARCH_TEXT
                ),
            ),
            answer=SAMPLE_SEARCH_ANSWER,
        )

    async def fetch(self, request: FetchRequest) -> FetchedPage:
        return FetchedPage(url=request.url, text=SAMPLE_FETCH_TEXT)
