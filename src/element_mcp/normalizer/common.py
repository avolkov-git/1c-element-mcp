from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import numpy as np

INDEX_SCHEMA_VERSION = 1
VECTOR_DIMENSIONS = 384
MAX_CHUNK_CHARS = 1800
MIN_CHUNK_CHARS = 180


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(stable_json(row) + "\n")
            count += 1
    return count


def iter_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().lower()
    value = re.sub(r"[^0-9a-zа-яё._/-]+", "-", value, flags=re.I)
    return re.sub(r"-+", "-", value).strip("-") or "document"


def normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\u00ad", "")
    # Generated API/stdlib HTML can contain NUL and other C0 separators.
    # They make Markdown look binary to rg and corrupt downstream tokenization.
    text = "".join(character for character in text if character in {"\n", "\t"} or ord(character) >= 32)
    lines = [re.sub(r"[ \t]+$", "", line) for line in text.splitlines()]
    output: list[str] = []
    blank = False
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and not line.strip():
            if output and not blank:
                output.append("")
            blank = True
            continue
        blank = False
        output.append(line)
    return "\n".join(output).strip() + "\n"


class DocusaurusParser(HTMLParser):
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    BLOCK = {"p", "div", "section", "article", "header", "footer", "blockquote", "dl", "dt", "dd"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.capturing = False
        self.capture_depth = 0
        self.skip_depth = 0
        self.pre_depth = 0
        self.code_depth = 0
        self.list_stack: list[str] = []
        self.link_stack: list[str | None] = []
        self.parts: list[str] = []
        self.canonical: str | None = None
        self.modified: str | None = None

    def _attrs(self, attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def _append(self, value: str) -> None:
        if not value:
            return
        if self.pre_depth:
            self.parts.append(value)
            return
        value = re.sub(r"\s+", " ", value)
        if not value.strip():
            if self.parts and not self.parts[-1].endswith((" ", "\n")):
                self.parts.append(" ")
            return
        stripped = value.strip()
        if (
            self.parts
            and not self.parts[-1].endswith((" ", "\n", "[", "`", "("))
            and not stripped.startswith((".", ",", ":", ";", ")", "]"))
        ):
            self.parts.append(" ")
        self.parts.append(stripped)

    def _newline(self, count: int = 1) -> None:
        current = "".join(self.parts[-3:])
        existing = len(current) - len(current.rstrip("\n"))
        if existing < count:
            self.parts.append("\n" * (count - existing))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = self._attrs(attrs)
        if tag == "link" and data.get("rel") == "canonical":
            self.canonical = data.get("href")
        if tag == "time" and data.get("datetime"):
            self.modified = data["datetime"]
        if not self.capturing:
            classes = set(data.get("class", "").split())
            if tag == "div" and "theme-doc-markdown" in classes:
                self.capturing = True
                self.capture_depth = 1
            return
        if tag not in self.VOID:
            self.capture_depth += 1
        if self.skip_depth:
            if tag not in self.VOID:
                self.skip_depth += 1
            return
        if tag in {"script", "style", "svg", "button"}:
            self.skip_depth = 1
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._newline(2)
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self._newline(2)
        elif tag == "br":
            self._newline(1)
        elif tag == "hr":
            self._newline(2)
            self.parts.append("---")
            self._newline(2)
        elif tag in {"ul", "ol"}:
            self.list_stack.append(tag)
            self._newline(1)
        elif tag == "li":
            self._newline(1)
            indent = "  " * max(0, len(self.list_stack) - 1)
            marker = "1. " if self.list_stack and self.list_stack[-1] == "ol" else "- "
            self.parts.append(indent + marker)
        elif tag == "pre":
            self._newline(2)
            self.parts.append("```\n")
            self.pre_depth += 1
        elif tag == "code":
            self.code_depth += 1
            if not self.pre_depth:
                self._append("`")
        elif tag == "a":
            href = data.get("href")
            self.link_stack.append(href)
            if href and not href.startswith("#"):
                self._append("[")
        elif tag == "img":
            alt = data.get("alt", "изображение")
            src = data.get("src", "")
            self._append(f"![{alt}]({src})")
        elif tag == "blockquote":
            self._newline(1)
            self.parts.append("> ")
        elif tag in {"table", "thead", "tbody", "tr"}:
            self._newline(1)
            if tag == "tr":
                self.parts.append("| ")
        elif tag in {"th", "td"}:
            pass

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if not self.capturing:
            return
        if self.skip_depth:
            self.skip_depth -= 1
        else:
            if re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
                self._newline(2)
            elif tag in {"ul", "ol"}:
                if self.list_stack:
                    self.list_stack.pop()
                self._newline(1)
            elif tag == "li":
                self._newline(1)
            elif tag == "pre":
                self.pre_depth = max(0, self.pre_depth - 1)
                self._newline(1)
                self.parts.append("```")
                self._newline(2)
            elif tag == "code":
                self.code_depth = max(0, self.code_depth - 1)
                if not self.pre_depth:
                    self.parts.append("`")
            elif tag == "a":
                href = self.link_stack.pop() if self.link_stack else None
                if href and not href.startswith("#"):
                    self.parts.append(f"]({href})")
            elif tag in {"th", "td"}:
                self.parts.append(" | ")
            elif tag == "tr":
                self._newline(1)
        if tag not in self.VOID:
            self.capture_depth -= 1
            if self.capture_depth <= 0:
                self.capturing = False

    def handle_data(self, data: str) -> None:
        if self.capturing and not self.skip_depth:
            self._append(data)

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n[ \t]+\n", "\n\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" +\|", " |", text)
        return normalize_markdown(text)


def html_to_markdown(source: Path) -> tuple[str, dict]:
    parser = DocusaurusParser()
    parser.feed(source.read_text(encoding="utf-8", errors="replace"))
    text = parser.markdown()
    title_match = re.search(r"^#\s+(.+)$", text, re.M)
    return text, {
        "title": title_match.group(1).strip() if title_match else source.parent.name,
        "source_uri": parser.canonical,
        "modified": parser.modified,
    }


def split_camel(token: str) -> list[str]:
    token = token.replace("::", "_").replace("/", "_").replace(".", "_").replace("-", "_")
    pieces: list[str] = []
    for part in token.split("_"):
        pieces.extend(re.findall(r"[A-ZА-ЯЁ]+(?=[A-ZА-ЯЁ][a-zа-яё]|$)|[A-ZА-ЯЁ]?[a-zа-яё]+|\d+", part))
    return [piece.lower() for piece in pieces if len(piece) > 1]


TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_:.\-/]{2,}")


def text_features(text: str) -> list[str]:
    raw = [token.lower() for token in TOKEN_RE.findall(unicodedata.normalize("NFKC", text))]
    words: list[str] = []
    for token in raw:
        words.append("w:" + token)
        words.extend("p:" + part for part in split_camel(token))
        compact = re.sub(r"[^0-9a-zа-яё]", "", token)
        if 5 <= len(compact) <= 80:
            padded = "^" + compact + "$"
            words.extend("c:" + padded[i : i + 3] for i in range(len(padded) - 2))
    lexical = [feature for feature in words if feature.startswith(("w:", "p:"))]
    words.extend("b:" + lexical[i] + "+" + lexical[i + 1] for i in range(len(lexical) - 1))
    return words


def feature_bucket(feature: str, dimensions: int = VECTOR_DIMENSIONS) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8, person=b"e1c-docs").digest()
    value = struct.unpack("<Q", digest)[0]
    return value % dimensions, -1.0 if value & (1 << 63) else 1.0


def split_oversized_paragraph(paragraph: str, max_chars: int) -> list[str]:
    lines = paragraph.splitlines()
    fenced = len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```")
    fence_open = lines[0] if fenced else ""
    body = lines[1:-1] if fenced else lines
    allowance = max_chars - len(fence_open) - 8 if fenced else max_chars
    result: list[str] = []
    current: list[str] = []
    current_size = 0
    overlap = 3
    for line in body:
        pieces = [line[i : i + allowance] for i in range(0, len(line), allowance)] or [""]
        for piece in pieces:
            addition = len(piece) + (1 if current else 0)
            if current and current_size + addition > allowance:
                payload = "\n".join(current)
                result.append(f"{fence_open}\n{payload}\n```" if fenced else payload)
                current = current[-overlap:]
                current_size = sum(len(value) + 1 for value in current)
            current.append(piece)
            current_size += len(piece) + (1 if len(current) > 1 else 0)
    if current:
        payload = "\n".join(current)
        result.append(f"{fence_open}\n{payload}\n```" if fenced else payload)
    return result or [paragraph[:max_chars]]


def chunk_document(document: dict, max_chars: int = MAX_CHUNK_CHARS) -> list[dict]:
    text = document["text"].strip()
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    heading = document.get("title", "")
    buffer: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and re.match(r"^#{1,6}\s+", line):
            if buffer:
                sections.append((heading, buffer))
            heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            buffer = [line]
        else:
            buffer.append(line)
    if buffer:
        sections.append((heading, buffer))

    raw_chunks: list[tuple[str, str]] = []
    for section, section_lines in sections:
        paragraphs: list[str] = []
        current: list[str] = []
        fence = False
        for line in section_lines:
            if line.startswith("```"):
                fence = not fence
            if not fence and not line.strip() and current:
                paragraphs.append("\n".join(current).strip())
                current = []
            else:
                current.append(line)
        if current:
            paragraphs.append("\n".join(current).strip())

        current_text = ""
        for paragraph in paragraphs:
            slices = split_oversized_paragraph(paragraph, max_chars) if len(paragraph) > max_chars else [paragraph]
            for part in slices:
                candidate = (current_text + "\n\n" + part).strip() if current_text else part
                if current_text and len(candidate) > max_chars:
                    raw_chunks.append((section, current_text))
                    current_text = part
                else:
                    current_text = candidate
        if current_text:
            if raw_chunks and len(current_text) < MIN_CHUNK_CHARS and raw_chunks[-1][0] == section:
                prev_section, prev = raw_chunks[-1]
                raw_chunks[-1] = (prev_section, prev + "\n\n" + current_text)
            else:
                raw_chunks.append((section, current_text))

    chunks: list[dict] = []
    for position, (section, chunk_text) in enumerate(raw_chunks):
        chunk_hash = sha256_text(chunk_text)[:12]
        chunk_id = f"{document['id']}#c{position:04d}-{chunk_hash}"
        chunks.append(
            {
                "id": chunk_id,
                "document_id": document["id"],
                "logical_id": document["logical_id"],
                "corpus": document["corpus"],
                "kind": document["kind"],
                "title": document["title"],
                "section": section,
                "position": position,
                "product_version": document["product_version"],
                "source_version": document["source_version"],
                "is_current": document["is_current"],
                "source_path": document.get("source_path"),
                "normalized_path": document.get("normalized_path"),
                "provenance": document["provenance"],
                "tags": document.get("tags", []),
                "sha256": sha256_text(chunk_text),
                "text": chunk_text,
            }
        )
    return chunks


def build_sqlite(path: Path, documents: list[dict], chunks: list[dict], metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=OFF;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE documents(
            id TEXT PRIMARY KEY, logical_id TEXT NOT NULL, corpus TEXT NOT NULL,
            kind TEXT NOT NULL, title TEXT NOT NULL, product_version TEXT NOT NULL,
            source_version TEXT NOT NULL, is_current INTEGER NOT NULL,
            source_path TEXT, normalized_path TEXT, provenance TEXT NOT NULL,
            tags TEXT NOT NULL, sha256 TEXT NOT NULL
        );
        CREATE TABLE chunks(
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL, logical_id TEXT NOT NULL,
            corpus TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
            section TEXT, position INTEGER NOT NULL, product_version TEXT NOT NULL,
            source_version TEXT NOT NULL, is_current INTEGER NOT NULL,
            source_path TEXT, normalized_path TEXT, provenance TEXT NOT NULL,
            tags TEXT NOT NULL, sha256 TEXT NOT NULL, text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            title, section, tags, text,
            content='chunks', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE INDEX idx_chunks_logical ON chunks(logical_id);
        CREATE INDEX idx_chunks_current ON chunks(is_current, corpus);
    """)
    db.executemany(
        "INSERT INTO metadata(key,value) VALUES (?,?)", [(key, stable_json(value)) for key, value in metadata.items()]
    )
    doc_columns = [
        "id",
        "logical_id",
        "corpus",
        "kind",
        "title",
        "product_version",
        "source_version",
        "is_current",
        "source_path",
        "normalized_path",
        "provenance",
        "tags",
        "sha256",
    ]
    db.executemany(
        f"INSERT INTO documents VALUES ({','.join('?' for _ in doc_columns)})",
        [
            tuple(
                stable_json(row.get(c)) if c == "tags" else int(row.get(c)) if c == "is_current" else row.get(c)
                for c in doc_columns
            )
            for row in documents
        ],
    )
    chunk_columns = [
        "id",
        "document_id",
        "logical_id",
        "corpus",
        "kind",
        "title",
        "section",
        "position",
        "product_version",
        "source_version",
        "is_current",
        "source_path",
        "normalized_path",
        "provenance",
        "tags",
        "sha256",
        "text",
    ]
    db.executemany(
        f"INSERT INTO chunks VALUES ({','.join('?' for _ in chunk_columns)})",
        [
            tuple(
                stable_json(row.get(c)) if c == "tags" else int(row.get(c)) if c == "is_current" else row.get(c)
                for c in chunk_columns
            )
            for row in chunks
        ],
    )
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    db.commit()
    db.execute("PRAGMA optimize")
    db.close()


def build_vectors(directory: Path, chunks: list[dict], dimensions: int = VECTOR_DIMENSIONS) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    counts: list[Counter[int]] = []
    document_frequency = np.zeros(dimensions, dtype=np.int64)
    for chunk in chunks:
        counter: Counter[int] = Counter()
        for feature in text_features(chunk["title"] + "\n" + chunk.get("section", "") + "\n" + chunk["text"]):
            bucket, _ = feature_bucket(feature, dimensions)
            counter[bucket] += 1
        counts.append(counter)
        for bucket in counter:
            document_frequency[bucket] += 1
    idf = np.log((len(chunks) + 1.0) / (document_frequency + 1.0)) + 1.0
    matrix = np.zeros((len(chunks), dimensions), dtype=np.float32)
    for row_index, (chunk, counter) in enumerate(zip(chunks, counts, strict=True)):
        sign_by_bucket: dict[int, float] = {}
        for feature in text_features(chunk["title"] + "\n" + chunk.get("section", "") + "\n" + chunk["text"]):
            bucket, sign = feature_bucket(feature, dimensions)
            sign_by_bucket[bucket] = sign_by_bucket.get(bucket, 0.0) + sign
        for bucket, count in counter.items():
            sign = 1.0 if sign_by_bucket.get(bucket, 1.0) >= 0 else -1.0
            matrix[row_index, bucket] = sign * (1.0 + math.log(count)) * float(idf[bucket])
        norm = np.linalg.norm(matrix[row_index])
        if norm:
            matrix[row_index] /= norm
    np.save(directory / "vectors.f16.npy", matrix.astype(np.float16), allow_pickle=False)
    np.save(directory / "vector-idf.npy", idf.astype(np.float32), allow_pickle=False)
    write_jsonl(directory / "vector-ids.jsonl", ({"row": i, "chunk_id": chunk["id"]} for i, chunk in enumerate(chunks)))
    input_hash = sha256_text("\n".join(chunk["id"] + ":" + chunk["sha256"] for chunk in chunks))
    metadata = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "algorithm": "signed-feature-hashing-word-bigram-char-trigram-tfidf-l2",
        "dimensions": dimensions,
        "dtype": "float16",
        "rows": len(chunks),
        "input_sha256": input_hash,
        "created_at": utc_now(),
    }
    write_json(directory / "vector-meta.json", metadata)
    return metadata


def document_record(
    *,
    logical_id: str,
    corpus: str,
    kind: str,
    title: str,
    product_version: str,
    source_version: str,
    current: bool,
    source_path: str | None,
    normalized_path: str | None,
    provenance: str,
    tags: list[str],
    text: str,
    source_uri: str | None = None,
    extra: dict | None = None,
) -> dict:
    text = normalize_markdown(text)
    record = {
        "id": f"{logical_id}@{product_version}|{source_version}",
        "logical_id": logical_id,
        "corpus": corpus,
        "kind": kind,
        "title": title,
        "product_version": product_version,
        "source_version": source_version,
        "is_current": bool(current),
        "language": "ru",
        "source_path": source_path,
        "source_uri": source_uri,
        "normalized_path": normalized_path,
        "provenance": provenance,
        "tags": sorted(set(tags)),
        "sha256": sha256_text(text),
        "text": text,
    }
    if extra:
        record.update(extra)
    return record


def build_corpus_index(corpus_dir: Path, documents: list[dict]) -> dict:
    documents = sorted(documents, key=lambda row: (row["logical_id"], row["source_version"]))
    chunks = [chunk for document in documents for chunk in chunk_document(document)]
    chunks.sort(key=lambda row: row["id"])
    corpus_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(corpus_dir / "documents.jsonl", documents)
    write_jsonl(corpus_dir / "chunks.jsonl", chunks)
    metadata = {
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "documents": len(documents),
        "chunks": len(chunks),
        "created_at": utc_now(),
    }
    build_sqlite(corpus_dir / "index.sqlite", documents, chunks, metadata)
    vector_meta = build_vectors(corpus_dir, chunks)
    metadata["vectors"] = vector_meta
    metadata["documents_sha256"] = sha256_text("\n".join(row["id"] + ":" + row["sha256"] for row in documents))
    write_json(corpus_dir / "manifest.json", metadata)
    return metadata
