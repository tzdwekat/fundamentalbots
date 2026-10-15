from dataclasses import dataclass

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")


@dataclass
class Chunk:
    id: str
    accession: str
    item: str
    n: int
    text: str
    token_count: int


def chunk_section(accession: str, item: str, text: str, size: int = 800, overlap: int = 100) -> list[Chunk]:
    """Token windows of `size` with `overlap` tokens carried over; ids {accession}#{item}-{n}."""
    tokens = ENC.encode(text)
    chunks, start, n = [], 0, 0
    while start < len(tokens):
        window = tokens[start:start + size]
        chunks.append(Chunk(f"{accession}#{item}-{n}", accession, item, n, ENC.decode(window), len(window)))
        if start + size >= len(tokens):
            break
        start += size - overlap
        n += 1
    return chunks
