-- 0008_seed.sql  演示租户与政策参考数据（生产由业务系统同步）
-- 注意：本迁移由迁移角色（超级用户）执行，可写入启用 RLS 的政策表。

INSERT INTO tenants (tenant_id, name, status) VALUES ('tenant_demo', 'Demo Tenant', 'ACTIVE')
ON CONFLICT (tenant_id) DO NOTHING;

INSERT INTO policy_documents (doc_id, tenant_id, title, version, status, effective_from) VALUES
    ('doc_fresh',     'tenant_demo', '生鲜售后政策',   '2026.10', 'PUBLISHED', '2026-01-01T00:00:00Z'),
    ('doc_apparel',   'tenant_demo', '服饰退货政策',   '2026.10', 'PUBLISHED', '2026-01-01T00:00:00Z'),
    ('doc_intercept', 'tenant_demo', '在途拦截政策',   '2026.10', 'PUBLISHED', '2026-01-01T00:00:00Z')
ON CONFLICT (doc_id) DO NOTHING;

INSERT INTO policy_rules
    (rule_id, tenant_id, version, priority, when_json, decision, reason_code,
     required_clause_ids, amount_cap_minor, scope, status, effective_from) VALUES
    ('R-REFUND-FRESH-001', 'tenant_demo', '2026.10', 100,
     '[{"fact":"claim.reason_code","op":"in","value":["DAMAGED","SPOILED","FRESH_DAMAGED"]},
       {"fact":"order.category","op":"eq","value":"FRESH"}]'::jsonb,
     'ALLOW', 'FRESH_DAMAGED_WITHIN_WINDOW', ARRAY['CL-FRESH-A','CL-FRESH-EX1'], 20000,
     '{"category":"FRESH"}'::jsonb, 'PUBLISHED', '2026-01-01T00:00:00Z'),
    ('R-RETURN-APPAREL-001', 'tenant_demo', '2026.10', 90,
     '[{"fact":"claim.reason_code","op":"in","value":["NO_LONGER_NEEDED","SIZE_ISSUE"]},
       {"fact":"order.category","op":"eq","value":"APPAREL"}]'::jsonb,
     'ALLOW', 'APPAREL_RETURN_WINDOW', ARRAY['CL-APPAREL-A'], 50000,
     '{"category":"APPAREL"}'::jsonb, 'PUBLISHED', '2026-01-01T00:00:00Z'),
    ('R-INTERCEPT-001', 'tenant_demo', '2026.10', 90,
     '[{"fact":"logistics.parcel_status","op":"eq","value":"IN_TRANSIT"}]'::jsonb,
     'ALLOW', 'IN_TRANSIT_INTERCEPT', ARRAY['CL-INTERCEPT-A'], NULL,
     '{}'::jsonb, 'PUBLISHED', '2026-01-01T00:00:00Z')
ON CONFLICT (rule_id) DO NOTHING;

INSERT INTO policy_chunks
    (tenant_id, doc_id, version, clause_id, clause_group_id, is_exception, text) VALUES
    ('tenant_demo', 'doc_fresh',     '2026.10', 'CL-FRESH-A',     NULL,        false, '生鲜类商品签收后 24 小时内可申请仅退款。'),
    ('tenant_demo', 'doc_fresh',     '2026.10', 'CL-FRESH-EX1',   'GRP-FRESH', true,  '例外：已开封且非质量问题不予仅退款。'),
    ('tenant_demo', 'doc_apparel',   '2026.10', 'CL-APPAREL-A',   NULL,        false, '服饰类支持 7 天无理由退货退款。'),
    ('tenant_demo', 'doc_intercept', '2026.10', 'CL-INTERCEPT-A', NULL,        false, '在途包裹可申请拦截，拦截成功后按退款流程处理。')
ON CONFLICT (tenant_id, clause_id, version) DO NOTHING;
