"""Bounded research and PC actions. Documents are observations, never tool programs."""

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx
from pydantic import Field, field_validator

from esi.models import uid, utc
from esi02.world import Model

PUBLIC_SOURCES = ["ja.wikipedia.org", "en.wikipedia.org", "lite.duckduckgo.com", "www.bing.com"]
TEXT_EXTENSIONS = {".txt", ".md", ".rst", ".csv", ".json", ".toml", ".yaml", ".yml"}


class Policy(Model):
    web_enabled: bool = True
    search_provider: Literal["wikipedia", "duckduckgo", "bing"] = "bing"
    allowed_domains: list[str] = Field(default_factory=lambda: list(PUBLIC_SOURCES), max_length=40)
    read_roots: list[str] = Field(default_factory=list, max_length=12)
    note_root: str = Field(default="", max_length=2000)
    allow_open_url: bool = False
    allow_open_file: bool = False
    allow_save_note: bool = False
    max_document_bytes: int = Field(default=524288, ge=1024, le=2097152)
    max_text_chars: int = Field(default=16000, ge=1000, le=32000)
    timeout_seconds: float = Field(default=15, ge=3, le=30)
    search_results: int = Field(default=3, ge=1, le=5)

    @field_validator("allowed_domains")
    @classmethod
    def domains(cls, values):
        result = []
        for value in values:
            value = value.lower().rstrip(".")
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value) or ".." in value:
                raise ValueError("Webの範囲はホスト名で指定してください。")
            if value in {"localhost", "local"} or value.endswith((".local", ".localhost")):
                raise ValueError("Web調査には公開ホストを指定してください。")
            result.append(value)
        return sorted(set(result))

    @field_validator("read_roots")
    @classmethod
    def absolute_roots(cls, roots):
        result = []
        for root in roots:
            path = Path(root).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("読める範囲には存在するフォルダーの絶対パスを指定してください。")
            resolved = path.resolve()
            if resolved == Path(resolved.anchor):
                raise ValueError("ドライブ全体ではなく調査用のフォルダーを選んでください。")
            result.append(str(resolved))
        return list(dict.fromkeys(result))


class Evidence(Model):
    id: str = Field(min_length=1, max_length=64)
    kind: Literal["web", "file"]
    title: str = Field(max_length=200)
    text: str = Field(max_length=32000)
    retrieved_at: str = Field(max_length=100)
    sha256: str = Field(min_length=64, max_length=64)
    url: str | None = Field(default=None, max_length=2000)
    path: str | None = Field(default=None, max_length=2000)
    unverified: Literal[True] = True


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.skip = 0
        self.title = ""
        self.in_title = False
        self.links = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3"}:
            self.text.append("\n")
        if tag == "a" and not self.skip:
            self.anchor = {"url": attrs.get("href", ""), "text": "", "class": attrs.get("class", "")}

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.anchor:
            self.links.append(self.anchor)
            self.anchor = None
        if tag in {"p", "div", "li"}:
            self.text.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        self.text.append(data)
        if self.in_title:
            self.title += data
        if self.anchor:
            self.anchor["text"] += data

    def plain(self):
        return "\n".join(line.strip() for line in "".join(self.text).splitlines() if line.strip())


class SearchHTML(HTMLText):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.current_card = None
        self.li_depth = 0

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attributes = dict(attrs)
        if tag == "li" and "b_algo" in attributes.get("class", "").split():
            self.current_card = {"text": [], "links": []}
            self.li_depth = 1
        elif tag == "li" and self.current_card is not None:
            self.li_depth += 1
        if tag == "a" and self.current_card is not None:
            self.current_card["links"].append(attributes.get("href", ""))

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "li" and self.current_card is not None:
            self.li_depth -= 1
            if self.li_depth == 0:
                self.current_card["text"] = " ".join(self.current_card["text"])
                self.cards.append(self.current_card)
                self.current_card = None

    def handle_data(self, data):
        super().handle_data(data)
        if self.current_card is not None and not self.skip:
            self.current_card["text"].append(data)


def related_query(text, query):
    terms = [re.sub(r"\W", "", word).lower() for word in re.split(r"[\s、。]+", query)]
    normalized = re.sub(r"\W", "", text).lower()
    return any(term and term in normalized for term in terms)


class ToolRunner:
    def __init__(self, policy, cancellation, client_factory=httpx.Client, resolver=socket.getaddrinfo):
        self.policy = Policy.model_validate(policy.model_dump() if isinstance(policy, Policy) else policy)
        self.cancellation = cancellation
        self.client_factory, self.resolver = client_factory, resolver

    def public_url(self, value):
        if not isinstance(value, str) or len(value) > 2000 or re.search(r"[\x00-\x20\\]", value):
            raise ValueError("URLの形式が正しくありません。")
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or not host:
            raise ValueError("公開WebのHTTP/HTTPS URLを指定してください。")
        if parsed.port not in {None, 80, 443}:
            raise ValueError("Web調査は通常のHTTP/HTTPSポートに限定されています。")
        allowed = any(host == domain or host.endswith("." + domain) for domain in self.policy.allowed_domains)
        if not allowed:
            raise PermissionError("このWebホストは調査範囲に含まれていません。")
        addresses = self.resolver(
            host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise PermissionError("Web調査からローカル機器や内部ネットワークには接続しません。")
        return value

    def _get(self, url):
        if not self.policy.web_enabled:
            raise PermissionError("Web調査がOFFです。")
        url = self.public_url(url)
        with self.client_factory(
            timeout=self.policy.timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "ESI-03/0.3 (personal research; evidence retained)"},
        ) as client:
            self.cancellation.attach(client.close)
            try:
                for _ in range(4):
                    self.cancellation.check()
                    with client.stream("GET", url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = self.public_url(urljoin(url, response.headers["location"]))
                            continue
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0]
                        if content_type not in {
                            "text/html",
                            "text/plain",
                            "application/json",
                            "application/xhtml+xml",
                        }:
                            raise ValueError("Web調査ではテキストとJSONのみ取得します。")
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            self.cancellation.check()
                            raw.extend(chunk)
                            if len(raw) > self.policy.max_document_bytes:
                                raise ValueError("資料の容量が設定した上限を超えています。")
                        text = bytes(raw).decode(response.encoding or "utf-8", errors="replace")
                        return {
                            "url": url,
                            "text": text,
                            "type": content_type,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        }
                raise ValueError("Web資料の転送回数が上限を超えました。")
            finally:
                self.cancellation.detach()

    def _evidence(self, kind, title, text, digest, **location):
        text = text.strip()[: self.policy.max_text_chars]
        if not text:
            raise ValueError("資料から本文を取得できませんでした。")
        identity = (
            kind
            + ":"
            + str(location.get("url") or location.get("path"))
            + ":"
            + hashlib.sha256(text.encode()).hexdigest()
        )
        return Evidence(
            id=hashlib.sha256(identity.encode()).hexdigest()[:32],
            kind=kind,
            title=title.strip()[:200],
            text=text,
            retrieved_at=utc(),
            sha256=digest,
            **location,
        ).model_dump()

    def fetch(self, url):
        result = self._get(url)
        if result["type"] in {"text/html", "application/xhtml+xml"}:
            parser = HTMLText()
            parser.feed(result["text"])
            title, text = parser.title, parser.plain()
        else:
            title, text = urlsplit(url).hostname, result["text"]
        return self._evidence("web", title or "Web資料", text, result["sha256"], url=result["url"])

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("調べる内容は1〜200文字です。")
        query = query.strip()
        if self.policy.search_provider == "wikipedia":
            url = (
                "https://ja.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit="
                + str(self.policy.search_results)
                + "&srsearch="
                + quote(query)
            )
            data = json.loads(self._get(url)["text"])
            evidence = []
            for hit in data.get("query", {}).get("search", [])[: self.policy.search_results]:
                self.cancellation.check()
                page = (
                    "https://ja.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&exintro=1&format=json&pageids="
                    + str(int(hit["pageid"]))
                )
                result = self._get(page)
                document = json.loads(result["text"])["query"]["pages"][str(hit["pageid"])]
                if document.get("extract", "").strip():
                    evidence.append(
                        self._evidence(
                            "web",
                            document["title"],
                            document["extract"],
                            result["sha256"],
                            url="https://ja.wikipedia.org/?curid=" + str(int(hit["pageid"])),
                        )
                    )
            if not evidence:
                raise ValueError("検索した資料に本文がありませんでした。別の語や検索先を試してください。")
            return {"query": query, "provider": "wikipedia", "evidence": evidence}
        endpoint = (
            "https://lite.duckduckgo.com/lite/?q="
            if self.policy.search_provider == "duckduckgo"
            else "https://www.bing.com/search?q="
        )
        result = self._get(
            endpoint
            + quote(query)
            + ("&mkt=ja-JP&setlang=ja" if self.policy.search_provider == "bing" else "")
        )
        parser = SearchHTML()
        parser.feed(result["text"])
        urls = []
        relevant_cards = [card for card in parser.cards if related_query(card["text"], query)]
        card_urls = {urljoin(result["url"], url) for card in relevant_cards for url in card["links"]}
        for link in parser.links:
            url = urljoin(result["url"], link["url"])
            if self.policy.search_provider == "bing" and url not in card_urls:
                continue
            parsed = urlsplit(url)
            params = parse_qs(parsed.query)
            if "uddg" in params:
                url = unquote(params["uddg"][0])
            elif parsed.hostname == "www.bing.com" and params.get("u", [""])[0].startswith("a1"):
                encoded = params["u"][0][2:]
                try:
                    url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
                except (ValueError, UnicodeError):
                    continue
            if urlsplit(url).hostname == urlsplit(endpoint).hostname:
                continue
            if link["text"].strip() and url not in urls:
                try:
                    self.public_url(url)
                except (ValueError, PermissionError, OSError):
                    continue
                urls.append(url)
        evidence, errors = [], []
        for url in urls[: self.policy.search_results]:
            try:
                document = self.fetch(url)
                if related_query(document["title"] + document["text"], query):
                    evidence.append(document)
                else:
                    errors.append({"url": url, "reason": "検索語との関連を確認できませんでした。"})
            except (httpx.HTTPError, ValueError, OSError) as exc:
                errors.append({"url": url, "reason": type(exc).__name__})
        if not evidence:
            # Search snippets are acquired observations too, with their own URL.
            # They are not mislabeled as full source pages and remain unverified.
            snippet = (
                "\n\n".join(card["text"] for card in relevant_cards)
                if self.policy.search_provider == "bing"
                else parser.plain()
            )
            if len(snippet) < 200 or re.search(
                r"captcha|verify you are human|anomaly-modal|challenge-form", result["text"], re.IGNORECASE
            ):
                raise ValueError(
                    "検索先の制限・調査範囲・本文の取得を確認できませんでした。検索先や対象ホストを変更してください。"
                )
            evidence.append(
                self._evidence(
                    "web",
                    "検索結果（元記事は未取得）: " + query,
                    snippet,
                    result["sha256"],
                    url=result["url"],
                )
            )
        return {
            "query": query,
            "provider": self.policy.search_provider,
            "evidence": evidence,
            "failed_sources": errors,
        }

    def _path(self, value, directory=False):
        if not isinstance(value, (str, Path)):
            raise ValueError("ファイルのパスを指定してください。")
        candidate = Path(value)
        if not candidate.is_absolute():
            raise PermissionError("指定フォルダー内の絶対パスが必要です。")
        path = candidate.resolve(strict=True)
        roots = [Path(root) for root in self.policy.read_roots]
        root = next((root for root in roots if path == root or path.is_relative_to(root)), None)
        if root is None:
            raise PermissionError("このパスは読める範囲に含まれていません。")
        # Do not follow a symlink/junction supplied below an authorised root.
        original = candidate.absolute()
        original_root = next(
            (root for root in roots if original == root or original.is_relative_to(root)), None
        )
        if original_root is None:
            raise PermissionError("範囲外のリンク経由でファイルを読みません。")
        for ancestor in [original, *original.parents]:
            if ancestor == original_root:
                break
            info = ancestor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PermissionError("リンク・ジャンクション経由の資料は読みません。")
        if (
            directory != path.is_dir()
            or not directory
            and (path.suffix.lower() not in TEXT_EXTENSIONS or path.name.startswith("."))
        ):
            raise ValueError("テキスト資料または指定フォルダーを選んでください。")
        return path

    def list_folder(self, path):
        folder = self._path(path, directory=True)
        entries = []
        with os.scandir(folder) as scan:
            for entry in scan:
                self.cancellation.check()
                if len(entries) >= 200:
                    break
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False) or Path(entry.name).suffix.lower() in TEXT_EXTENSIONS:
                    try:
                        candidate = self._path(entry.path, directory=entry.is_dir(follow_symlinks=False))
                        entries.append({"path": str(candidate), "directory": candidate.is_dir()})
                    except (ValueError, PermissionError, OSError):
                        continue
        return {
            "folder": str(folder),
            "entries": sorted(entries, key=lambda item: item["path"]),
            "limit": 200,
        }

    def read_file(self, value):
        path = self._path(value)
        self.cancellation.check()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.policy.max_document_bytes:
                raise ValueError("資料の種類か容量が読み取り範囲を超えています。")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(self.policy.max_document_bytes + 1)
            self._path(path)  # Revalidate after reading before exposing any content.
            if len(raw) > self.policy.max_document_bytes:
                raise ValueError("資料の容量が設定した上限を超えています。")
        finally:
            os.close(descriptor)
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        try:
            text = raw.decode(encoding)
        except UnicodeError:
            text = raw.decode("cp932")
        if "\x00" in text:
            raise ValueError("この資料はテキストとして読めません。")
        return self._evidence("file", path.name, text, hashlib.sha256(raw).hexdigest(), path=str(path))

    def research_folder(self, query):
        if not self.policy.read_roots:
            raise PermissionError("読めるフォルダーが設定されていません。")
        query_words = [word for word in re.split(r"[\s、。]+", query) if word]
        evidence = []
        scanned = 0
        for root in self.policy.read_roots:
            folders = [root]
            for _ in range(20):
                if not folders or len(evidence) >= self.policy.search_results:
                    break
                folder = folders.pop(0)
                for entry in self.list_folder(folder)["entries"]:
                    if entry["directory"]:
                        if len(folders) < 20:
                            folders.append(entry["path"])
                        continue
                    if scanned >= 50:
                        break
                    scanned += 1
                    try:
                        document = self.read_file(entry["path"])
                    except (ValueError, UnicodeError, OSError):
                        continue
                    if any(word in document["text"] or word in document["title"] for word in query_words):
                        evidence.append(document)
                        if len(evidence) >= self.policy.search_results:
                            break
        if not evidence:
            raise ValueError("指定した範囲に疑問と関連する資料が見つかりませんでした。")
        return {
            "query": query,
            "provider": "designated_folders",
            "evidence": evidence,
            "scanned_files": scanned,
        }

    def execute(self, action, arguments):
        self.cancellation.check()
        if action == "web_search" and set(arguments) == {"query"}:
            return self.search(arguments["query"])
        if action == "fetch" and set(arguments) == {"url"}:
            return {"evidence": [self.fetch(arguments["url"])]}
        if action == "read_folder" and set(arguments) == {"query"}:
            return self.research_folder(arguments["query"])
        if action == "list_folder" and set(arguments) == {"path"}:
            return self.list_folder(arguments["path"])
        if action == "read_file" and set(arguments) == {"path"}:
            return {"evidence": [self.read_file(arguments["path"])]}
        if action == "open_url" and set(arguments) == {"url"}:
            if not self.policy.allow_open_url or not self.policy.web_enabled:
                raise PermissionError("ブラウザーを開く機能がOFFです。")
            url = self.public_url(arguments["url"])
            if not webbrowser.open(url, new=2):
                raise RuntimeError("ブラウザーへURLを渡せませんでした。")
            return {"opened_url": url, "external_result_confirmed": False}
        if action == "open_file" and set(arguments) == {"path"}:
            if not self.policy.allow_open_file:
                raise PermissionError("資料を別のアプリで開く機能がOFFです。")
            path = self._path(arguments["path"])
            if os.name != "nt":
                raise RuntimeError("資料を別のアプリで開く操作はWindows版に対応しています。")
            os.startfile(path)
            return {"opened_path": str(path), "external_result_confirmed": False}
        if action == "save_note" and set(arguments) == {"text"}:
            if not self.policy.allow_save_note or not self.policy.note_root:
                raise PermissionError("学習ノートの保存先が設定されていません。")
            text = arguments["text"]
            if not isinstance(text, str) or len(text) > 16000:
                raise ValueError("学習ノートは16000文字以内です。")
            directory = Path(self.policy.note_root)
            if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
                raise PermissionError("学習ノート用のフォルダーを指定してください。")
            path = directory / ("ESI-03-note-" + uid() + ".md")
            with path.open("x", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            return {
                "saved_path": str(path),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "external_result_confirmed": True,
            }
        raise ValueError("操作は定義済みの機能と正しい引数だけを指定できます。")
