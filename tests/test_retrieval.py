import pytest
import tiktoken

from fundabot.retrieval.chunk import Chunk, chunk_section
from fundabot.retrieval.embed import FakeEmbedder

ENC = tiktoken.get_encoding("cl100k_base")


def test_chunk_ids_and_overlap():
    text = " ".join(f"w{i}" for i in range(1500))
    chunks = chunk_section("0000320193-25-000079", "item1a", text, size=800, overlap=100)
    assert [c.id for c in chunks] == [f"0000320193-25-000079#item1a-{n}" for n in range(len(chunks))]
    assert all(c.token_count <= 800 for c in chunks) and len(chunks) >= 2
    first_tokens = ENC.encode(chunks[0].text)
    second_tokens = ENC.encode(chunks[1].text)
    assert second_tokens[:100] == first_tokens[-100:]
    assert chunks[0].accession == "0000320193-25-000079" and chunks[0].item == "item1a" and chunks[1].n == 1


def test_fake_embedder_is_deterministic_and_sized():
    e = FakeEmbedder()
    a, b = e.embed(["hello", "hello"])
    assert a == b and len(a) == 1536
    assert e.embed(["other"])[0] != a


@pytest.mark.db
def test_retrieve_cap_and_dedupe(db_conn):
    from fundabot.retrieval.query import embed_missing, retrieve

    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO filings (accession, cik, form, filed, items_ok) VALUES ('acc-1', 1, '10-K', '2025-01-01', true)")
        for i in range(10):
            # ~1,000 tokens each -> chunked into pieces; 10 sections give well over 30 chunks
            cur.execute("INSERT INTO filing_sections (accession, item, text) VALUES ('acc-1', %s, %s)", (f"item{i}", " ".join(f"risk{i} word{j}" for j in range(1200))))
    db_conn.commit()
    embed_missing(db_conn, FakeEmbedder(), ["acc-1"])
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), count(embedding) FROM chunks WHERE accession = 'acc-1'")
        total, embedded = cur.fetchone()
    assert total > 30 and embedded == total
    embed_missing(db_conn, FakeEmbedder(), ["acc-1"])  # idempotent
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE accession = 'acc-1'")
        assert cur.fetchone()[0] == total
    got = retrieve(db_conn, FakeEmbedder(), ["acc-1"], ["risk factors", "liquidity"], top_k=40, max_tokens=30000)
    assert 0 < len(got) <= 40 and len({c.id for c in got}) == len(got)
    assert sum(c.token_count for c in got) <= 30000
    assert all(isinstance(c, Chunk) for c in got)
