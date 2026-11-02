-- 0009_roles.sql  应用/Worker 数据库角色（LLD §19.2）。
-- 应用角色不得为超级用户、不得拥有 BYPASSRLS，RLS 才会对业务生效。
-- Worker 需要跨租户领取命令/对账，单独授予 BYPASSRLS，仅用于系统事务（system_transaction）。
-- 注意：以下口令仅用于本地/联调基线，生产必须改为密钥管理注入并轮换。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aftersale_app') THEN
    EXECUTE format('CREATE ROLE aftersale_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', 'app_pw_change_me');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aftersale_worker') THEN
    EXECUTE format('CREATE ROLE aftersale_worker LOGIN NOSUPERUSER BYPASSRLS PASSWORD %L', 'worker_pw_change_me');
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO aftersale_app, aftersale_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aftersale_app, aftersale_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aftersale_app, aftersale_worker;

-- 便于后续由迁移角色新建对象时，自动授权给业务/Worker 角色
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aftersale_app, aftersale_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO aftersale_app, aftersale_worker;
