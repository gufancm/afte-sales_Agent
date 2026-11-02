-- 0011_embedding_hnsw.sql
-- policy_chunks 向量索引（HNSW, cosine）。幂等：可重复执行。
-- 说明：索引必须由表 owner（agent_user）创建；应用角色 aftersale_app 无 DDL 权限。
-- 维度与模型口径见 LLD §5.7：查询必须带相同的 embedding_model 条件。
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON policy_chunks USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

-- 会话默认搜索宽度（工程起始值，LLD §5.7 给出 40）
ALTER DATABASE aftersale SET hnsw.ef_search = 40;