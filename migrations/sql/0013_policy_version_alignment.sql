-- 0013_policy_version_alignment.sql
-- 目的：把真实京东语料（jd_* 文档）的条款 / 父块 / 文档版本号，统一对齐到政策发布版本 "2026.10-jd"。
-- 背景：证据门禁（app/rag/evidence.py）会校验 required_clause.version 与 decision.policy_version 是否一致；
--       若文档版本停留在构建期的 "v1"、而规则发布版本为 "2026.10-jd"，会被误判 CLAUSE_VERSION_DRIFT → STALE → 转人工。
-- 幂等：可重复执行；仅在对应表存在时执行。

SELECT set_config('app.tenant_id', 'tenant_demo', false);

DO $$
BEGIN
  IF to_regclass('public.policy_chunks') IS NOT NULL THEN
    UPDATE policy_chunks SET version = '2026.10-jd'
      WHERE tenant_id = 'tenant_demo' AND version = 'v1' AND substring(doc_id from 1 for 3) = 'jd_';
  END IF;
  IF to_regclass('public.policy_parents') IS NOT NULL THEN
    UPDATE policy_parents SET version = '2026.10-jd'
      WHERE tenant_id = 'tenant_demo' AND version = 'v1' AND substring(doc_id from 1 for 3) = 'jd_';
  END IF;
  IF to_regclass('public.policy_documents') IS NOT NULL THEN
    UPDATE policy_documents SET version = '2026.10-jd'
      WHERE tenant_id = 'tenant_demo' AND version = 'v1' AND substring(doc_id from 1 for 3) = 'jd_';
  END IF;
END $$;