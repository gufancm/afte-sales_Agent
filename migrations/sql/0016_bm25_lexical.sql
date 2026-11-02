-- 0016_bm25_lexical.sql 真实 BM25 词法检索：jieba 分词后的 term 频率 + 文档长度
-- tokens: {term: tf}（ingest 时用 app.rag.tokenize 计算）；doc_len: 文档 token 数（长度归一化用）
ALTER TABLE policy_chunks
    ADD COLUMN IF NOT EXISTS tokens  JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS doc_len INT;

CREATE INDEX IF NOT EXISTS idx_chunks_tokens ON policy_chunks USING gin (tokens jsonb_path_ops);