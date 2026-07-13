# pgvector RAG output port

A **vector / embeddings output port** — the contract declares a `pgvector`
binding, and `fluid` emits the embeddings-table + ANN-index DDL a
Retrieval-Augmented-Generation (RAG) pipeline writes into, plus a provenance
manifest a retriever consumes.

It **consumes the `ai-embeddable` column labels** the built-in `ai_ready`
agent stamps (PR #405): only columns labelled `ai-embeddable: "true"` become
embedding targets. Here that's `title` and `body`; `article_id` (the key) and
`locale` (metadata) are deliberately *not* embedded.

## The binding

```yaml
binding:
  platform: pgvector          # selects the vector output port
  format: pgvector_table
  location: { database: rag, schema: public, table: kb_articles }
  vectorConfig:
    dimensions: 1536          # → embedding vector(1536)
    embeddingModel: text-embedding-3-small
    indexType: hnsw           # hnsw | ivfflat | none
    distanceMetric: cosine    # → vector_cosine_ops
    sourceKeyColumn: article_id
    table: kb_article_embeddings
    hnsw: { m: 16, efConstruction: 64 }
```

## Try it

```bash
# Validate (the pgvector binding is checked at validate time).
fluid validate contract.fluid.yaml

# Emit the pgvector target: embeddings.sql + vector_manifest.json.
fluid generate vector contract.fluid.yaml --out runtime/vector
```

`runtime/vector/embeddings.sql` contains:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS "kb_article_embeddings" (
    id bigserial PRIMARY KEY,
    source_key text,
    source_column text NOT NULL,
    content text NOT NULL,
    embedding vector(1536),
    embedding_model text,
    chunk_index integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS "kb_article_embeddings_embedding_idx"
    ON "kb_article_embeddings" USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

`runtime/vector/vector_manifest.json` records, per expose: the source columns
to embed, the embedding model + dimensions + distance metric, the target
table, and the `sourceKeyColumn` that ties each embedding chunk back to its
source row — everything a RAG ingestion pipeline needs.

## What this port does and does not do

- **Does**: emit the vector-store *target* (table + index DDL) and the RAG
  provenance manifest — a pure, credential-free compile step.
- **Does not** (yet): generate the embeddings themselves, or write to
  LanceDB / Qdrant. Those are the scoped follow-ups layered on this seam; the
  manifest is the hand-off contract to whatever runs the embedding model.
