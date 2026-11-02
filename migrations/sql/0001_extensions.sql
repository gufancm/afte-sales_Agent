-- 0001_extensions.sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector (dense)
-- BM25 走独立服务或 pg_search 扩展（部署时锁定镜像与 preload）；此处不假设 pg_trgm 为 BM25。