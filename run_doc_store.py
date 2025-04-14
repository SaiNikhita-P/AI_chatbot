from __future__ import annotations
import json
import logging
import time
import warnings
from dataclasses import dataclass
from typing import Generator, Protocol, TypedDict
import requests
from bs4 import BeautifulSoup
import pathway as pw
import os
from pathway.io.python import ConnectorSubject
from pyngrok import ngrok
from pathway.stdlib.indexing.nearest_neighbors import BruteForceKnnFactory
from pathway.xpacks.llm import llms
from pathway.xpacks.llm.document_store import DocumentStore
from pathway.xpacks.llm.embedders import OpenAIEmbedder,GeminiEmbedder
from pathway.xpacks.llm.parsers import UnstructuredParser, Utf8Parser
from pathway.xpacks.llm.splitters import TokenCountSplitter
from pathway.stdlib.indexing import BruteForceKnnFactory, HybridIndexFactory
from pathway.stdlib.indexing.bm25 import TantivyBM25Factory
from pathway.udfs import DiskCache
from pathway.xpacks.llm import embedders, llms, parsers, splitters
import typing
from pathway.xpacks.llm.servers import DocumentStoreServer
from dotenv import load_dotenv

load_dotenv()


os.environ["PATHWAY_PERSISTENT_STORAGE"] = "/content/pathway_storage"
os.makedirs("/content/pathway_storage", exist_ok=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PATHWAY_PORT = os.getenv("PATHWAY_PORT")
PATHWAY_PORT2 = os.getenv("PATHWAY_PORT2")

# Use the environment variables in your script
os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY

# Ensure the storage directory exists
os.environ["PATHWAY_PERSISTENT_STORAGE"] = "/content/pathway_storage"

print(f"GEMINI_API_KEY: {GEMINI_API_KEY}")
print(f"PATHWAY_PORT: {PATHWAY_PORT}")
print(f"PATHWAY_PORT2: {PATHWAY_PORT2}")



DEFAULT_REQUEST_PARAMS = {
    "timeout": 25,
    "proxies": {},
    "headers": {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/38.0.101.76 Safari/537.36",  # noqa: E501
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/38.0.101.76 Safari/537.36",
    # "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    # "Pragma": "no-cache",
    # "Expires": "0",
}

def scrape_commentary(
    website_urls: list[str],
    refresh_interval: int = 600,
) -> Generator[dict[str, str], None, None]:
    indexed_matches: dict[str, str] = {}  # Stores match URLs and their latest commentary

    logging.info(f"Starting live commentary scraper with {len(website_urls)} URLs.")

    base_url = "https://www.cricbuzz.com"

    while True:
        for website in website_urls:
            logging.info(f"Fetching live match list from: {website}")
            try:
                response = requests.get(website, timeout=10)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')

                # Extract live match links
                matches = soup.find_all('div', class_="cb-col cb-col-100 cb-rank-tabs")
                matches_html = BeautifulSoup(str(matches), 'html.parser')
                match_links = matches_html.find_all('a', class_="cb-lv-scrs-well cb-lv-scrs-well-live")

                match_urls = [base_url + match["href"] for match in match_links]
                # print(match_urls)

            except requests.RequestException as e:
                warnings.warn(f"Failed to fetch match list from {website}: {e}")
                continue

            logging.info(f"Found {len(match_urls)} live matches.")

            for i, match_url in enumerate(match_urls):
                try:
                    match_response = requests.get(match_url, timeout=10)
                    match_response.raise_for_status()
                    match_soup = BeautifulSoup(match_response.text, 'html.parser')

                    # file_path = os.path.join("matches", f"match_{i}.html")
                    # os.makedirs("matches", exist_ok=True)  # Ensure directory exists
                    # with open(file_path, "w", encoding="utf-8") as file:
                    #     file.write(match_soup.prettify())

                    heading=match_soup.find("h1",class_="cb-nav-hdr cb-font-18 line-ht24").get_text(strip=True)
                    # Extract commentary section
                    commentary_section = match_soup.find_all('div', class_="cb-comm-pg")

                    if not commentary_section:
                        continue
                    
                    # Convert extracted content to text
                    latest_commentary = heading+"\n"+"\n".join([comment.get_text(strip=True) for comment in commentary_section])

                    file_path = os.path.join("matches", f"match_{i}.txt")
                    os.makedirs("matches", exist_ok=True)  # Ensure directory exists
                    with open(file_path, "w", encoding="utf-8") as file:
                        file.write(latest_commentary)

                    # **Overwrite the stored commentary**
                    indexed_matches[match_url] = latest_commentary
                    # print(latest_commentary)

                except requests.RequestException as e:
                    warnings.warn(f"Failed to fetch commentary from {match_url}: {e}")
                    continue

                # **Always print the latest commentary for each match**
                # print(f"\nMatch: {match_url}\nUpdated Commentary:\n{indexed_matches[match_url]}\n")

                yield {"url": match_url, "commentary": indexed_matches[match_url]}

        print(indexed_matches)
        time.sleep(refresh_interval)

class ConnectorSchema(pw.Schema):
    data: typing.Any

class MatchScraperSubject(ConnectorSubject):
    _website_urls: list[str]
    _refresh_interval: int

    def __init__(
        self,
        *,
        website_urls: list[str],
        refresh_interval: int,
    ) -> None:
        super().__init__()
        self._website_urls = website_urls
        self._refresh_interval = refresh_interval

    def run(self) -> None:
        for match_here in scrape_commentary(
            self._website_urls,
            refresh_interval=self._refresh_interval,
        ):
            text=match_here["commentary"]
            # print("Scraped Match: ", json.dumps(match_here, indent=2))

            self.next(data=text.encode())

class MyGeminiEmbedder(embedders.GeminiEmbedder):
    def __call__(self, input, **kwargs):
        time.sleep(6)  # Introduce a 1-second delay before each embedding request
        return super().__call__(input, **kwargs)


def load_document_store():
    print("document_store to be loaded")
    website_urls = ["https://www.cricbuzz.com/cricket-match/live-scores"]
    # parser = UnstructuredParser(chunking_mode="by_title")
    parser=Utf8Parser()
    embedder = MyGeminiEmbedder(cache_strategy=DiskCache())
    text_splitter = splitters.TokenCountSplitter(min_tokens=100, max_tokens=150)
    index = HybridIndexFactory(
        [
            TantivyBM25Factory(),
            BruteForceKnnFactory(embedder=embedder),
        ]
    )

    subject = MatchScraperSubject(
        website_urls=website_urls,
        refresh_interval=25,
    )

    matches = pw.io.python.read(subject, schema=ConnectorSchema)
    match_htmls = pw.io.fs.read(
        path="./matches",
        mode="streaming",
        format="binary",
        autocommit_duration_ms=50
    )

    document_store_html = DocumentStore(docs=[match_htmls], retriever_factory=index,parser=parser)
    document_store=DocumentStore(matches,retriever_factory=index)
    print("both document stores formed\n")

    # Run Servers

    server = DocumentStoreServer(
            host="127.0.0.1",
            port=PATHWAY_PORT,
            document_store=document_store,
        )
    server.run(threaded=True, with_cache=False)

    # Try to get public URL for the first server
    try:
        public_url = ngrok.connect(PATHWAY_PORT).public_url
        logging.info(f"Public URL for Server 1: {public_url}")
        os.environ["public_url"] = public_url
    except Exception as e:
        logging.warning(f"Failed to create public URL for server 1: {e}")
        # Leave without terminating, just log the error

    # Second server
    server2 = DocumentStoreServer(
        host="127.0.0.1",
        port=PATHWAY_PORT2,
        document_store=document_store_html,
    )
    server2.run(threaded=True, with_cache=False)

    # Try to get public URL for the second server
    try:
        public_url2 = ngrok.connect(PATHWAY_PORT2).public_url
        logging.info(f"Public URL for Server 2: {public_url2}")
        os.environ["public_url2"] = public_url2
    except Exception as e:
        logging.warning(f"Failed to create public URL for server 2: {e}")
        # Leave without terminating, just log the error

    with open("urls.txt","a") as file:
      file.write(f'{public_url}\n')
      file.write(f'{public_url2}\n')

    with open(".env", "a") as file:
      file.write(f"PUBLIC_URL_1={public_url}\n")
      file.write(f"PUBLIC_URL_2={public_url2}\n")

    os.environ["PUBLIC_URL_1"] = public_url
    os.environ["PUBLIC_URL_2"] = public_url2

if __name__=="__main__":
    website_urls = ["https://www.cricbuzz.com/cricket-match/live-scores"]
    load_document_store()
    while True:
      pass
    
    # To test scrape commentary
    # for match in scrape_commentary(website_urls, refresh_interval=20):
        # print()

