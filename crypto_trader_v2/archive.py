"""Seekable, bounded HTTP range reader for Kraken's official split ZIP.

    Reads selected members only. ZIP CRC and selected-member SHA256 are checked;
    this does NOT claim verification of the entire published 9 GB ZIP checksum.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import io
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen


OFFICIAL_PARTS = tuple(
    f"https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_Full_2026Q2.zip.part{i:02d}"
    for i in range(5)
)
SUPPORT_URL = "https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data"


@dataclass(frozen=True)
class RemotePart:
    url: str
    length: int
    etag: str


class SplitHTTPFile(io.RawIOBase):
    def __init__(self, urls=OFFICIAL_PARTS, *, budget=128 * 1024 * 1024, opener=urlopen):
        super().__init__()
        self.opener, self.budget = opener, budget
        self.parts, self.offsets = [], [0]
        self.position = self.downloaded = 0
        self.cache = {}
        for url in urls:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "assets.kraken.com" or not parsed.path.startswith("/marketing/institutions/"):
                raise ValueError("Archive URL is outside the official Kraken allowlist")
            with opener(Request(url, method="HEAD", headers={"Accept-Encoding": "identity", "User-Agent": "crypto-trader-v2/0.2 historical-data-research"}), timeout=30) as response:
                length = int(response.headers["Content-Length"])
                etag = response.headers.get("ETag", "")
                if length <= 0 or not etag:
                    raise ValueError("Archive requires positive size and immutable ETag")
                self.parts.append(RemotePart(url, length, etag))
                self.offsets.append(self.offsets[-1] + length)
        if not self.parts:
            raise ValueError("No archive parts")

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        target = offset if whence == io.SEEK_SET else self.position + offset if whence == io.SEEK_CUR else self.offsets[-1] + offset if whence == io.SEEK_END else -1
        if target < 0:
            raise ValueError("Invalid seek")
        self.position = target
        return target

    def _range(self, index: int, start: int, length: int) -> bytes:
        key = index, start, length
        if key in self.cache:
            return self.cache[key]
        if self.downloaded + length > self.budget:
            raise ValueError("Selective archive download exceeds the configured byte budget")
        part = self.parts[index]
        end = start + length - 1
        request = Request(part.url, headers={"Range": f"bytes={start}-{end}", "If-Match": part.etag, "Accept-Encoding": "identity", "User-Agent": "crypto-trader-v2/0.2 historical-data-research"})
        with self.opener(request, timeout=45) as response:
            # Never read a full multi-GB response if a server ignores Range.
            if response.status != 206:
                raise ValueError("Server ignored HTTP range; full archive download refused")
            if response.headers.get("Content-Range") != f"bytes {start}-{end}/{part.length}":
                raise ValueError("Unexpected Content-Range")
            if response.headers.get("ETag", part.etag) != part.etag:
                raise ValueError("Archive changed during import")
            payload = response.read(length + 1)
        if len(payload) != length:
            raise ValueError("Truncated/oversized archive range")
        self.downloaded += length
        if length <= 8 * 1024 * 1024:
            self.cache[key] = payload
        return payload

    def read(self, size=-1):
        if self.closed:
            raise ValueError("Archive is closed")
        available = max(self.offsets[-1] - self.position, 0)
        count = available if size < 0 else min(size, available)
        if count > self.budget:
            raise ValueError("Read exceeds archive byte budget")
        output = []
        while count:
            index = bisect_right(self.offsets, self.position) - 1
            offset = self.position - self.offsets[index]
            chunk = min(count, self.parts[index].length - offset)
            output.append(self._range(index, offset, chunk))
            self.position += chunk
            count -= chunk
        return b"".join(output)

    def manifest(self) -> dict:
        return {"urls": [p.url for p in self.parts], "etags": [p.etag for p in self.parts],
                "archive_bytes": self.offsets[-1], "downloaded_bytes": self.downloaded,
                "integrity": "HTTPS + consistent ETags + ZIP member CRC + selected-member SHA256; full-archive SHA256 not checked"}


def match_member(names: list[str], symbol: str, minutes: int) -> str:
    pair = symbol.replace("BTC/", "XBT/").replace("/", "")
    candidates = [name for name in names if re.fullmatch(rf"(?:.*/)?{re.escape(pair)}_{minutes}\.csv", name, re.IGNORECASE)]
    if len(candidates) != 1:
        raise ValueError(f"Expected one {pair}_{minutes}.csv member; found {len(candidates)}")
    return candidates[0]
