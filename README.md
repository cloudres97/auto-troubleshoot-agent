# 自动诊断 Agent

面向 **Windows 11 执行器 + WSL2 工作台** 的只读诊断首版。实现工单、持久化队列、YARN 状态采集、证据校验、人工复核和网页。

**当前使用确定性检查，尚未接入公司模型。** `demo` 返回明确标识的模拟数据。真实 YARN 适配器经过协议模拟测试，尚未在公司环境验收。FAILED 仅表示应用失败，不自动认定根因。日志/配置采集、变更执行、知识库和群机器人尚未实现。

## 本地运行（Linux / WSL）

要求 Python 3.10+，推荐 3.12。直接使用现有 checkout，无需新建 worktree。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints.txt -e '.[dev]'
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，为三个令牌分别填入不同随机值。可分别用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成；不要分享或提交真实值。应用不会自动加载 `.env`，每个终端先执行：

```bash
source .venv/bin/activate
set -a
source .env
set +a
```

在仓库根目录的三个终端分别运行：

```bash
# 终端 1：演示执行器；Windows 部署另见下文
python -m uvicorn troubleshoot.executor:create_executor --factory --host 127.0.0.1 --port 18081
# 终端 2：工单 API 与网页
python -m uvicorn troubleshoot.api:create_app --factory --host 127.0.0.1 --port 8080
# 终端 3：持久化队列消费者
python -m troubleshoot.worker
```

浏览器访问本机端口 8080，输入操作员令牌，以 `demo / application_1_0001` 创建工单；刷新详情查看结果，填写复核员令牌与说明后批准报告。令牌仅留在页面内存中。两个服务的 `/health` 用于就绪检查，`/docs` 提供接口定义。

默认 SQLite WAL，数据库位于 `data/diagnosis.db`，用于单机试用。PostgreSQL 可配置 `DIAG_DATABASE_URL=postgresql+psycopg://...`，凭据通过环境注入。当前仅自动创建初始表结构，正式升级前需引入 Alembic 迁移。不能把 SQLite 验证视为 PostgreSQL 的生产验收。

## Windows 执行器与真实 YARN

Windows Python 3.13 可安装同一包，使用 `.venv\Scripts\python.exe`。为执行器安全注入 `DIAG_EXECUTOR_TOKEN` 和 `DIAG_ENVIRONMENTS_FILE`；后者指向管理员维护的注册文件：

```json
{
  "test-yarn": {
    "adapter": "yarn",
    "base_url": "https://your-resource-manager.example:8090",
    "applications": ["application_123456789_0001"]
  }
}
```

调用方不能提供 URL。执行器只调用固定 `/ws/v1/cluster/apps/{application_id}`，检查应用白名单、不跟随重定向、保留 TLS 验证、限制输出 64 KiB、最多同时采集两次。当前仅支持可直接访问的 REST API，**不包含 Kerberos/SPNEGO、堡垒机、MFA 或 HA 重定向认证**；这些场景需要扩展并验收认证适配器。

Windows 绑定当次实际 WSL 网关接口地址，WSL 的 `DIAG_EXECUTOR_URL` 指向该地址的 18081 端口。NAT 重建后重新发现地址，端口、防火墙和代理规则见[原方案](docs/自动诊断Agent设计与实施方案.md)。本机通信在 `NO_PROXY` 中加入实际网关地址，外部请求继续使用受支持的代理和 CA。跨机器访问需 HTTPS 与受控身份入口；默认 loopback 服务不要直接暴露到局域网或公网。

## 状态、证据和恢复

```text
queued → collecting → awaiting_review → completed
                   ↘ collection_unknown
queued / collecting / collection_unknown → cancelled
collecting / collection_unknown → 人工核实并 recover → queued
```

- 原子领取工单，不持有事务等待网络。取消/恢复后旧领取结果不能覆盖新状态。
- 中断保留 collecting；超时或响应无效进入 collection_unknown。重启不会自动重放，复核员核实后调用 recover。取消阻止结果发布，无法保证已经发出的 HTTP 请求立即停止。
- 原始响应存数据库，记录 SHA-256、工具版本、UTC 时间、工单/环境/应用归属、失败和截断标记。摘要不能提供抗数据库管理员篡改能力。
- 普通操作员仅查看元数据和固定结构事实；原文仅复核员可下载。尚无通用脱敏，不向外部模型发送原始日志。磁盘访问及备份权限由部署方负责。
- 复核检查归属、摘要和引用，版本号拒绝过期操作。审阅批准不会把假设变成原因。
- 共享角色令牌适用于单机原型；尚无个人身份审计、用户级隔离或环境级 RBAC，多人部署前需补齐。

请求均携带 `Authorization: Bearer <token>`。普通操作使用操作员令牌，复核、恢复及原文下载使用独立复核员令牌。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /v1/cases` | 创建，字段 environment_id/application_id/symptom |
| `GET /v1/cases` | 最近 100 个工单 |
| `GET /v1/cases/{id}` | 报告、证据元数据与审计 |
| `POST /v1/cases/{id}/cancel` | 取消尚未生成报告的工单 |
| `POST /v1/cases/{id}/review` | 复核，字段 revision/note |
| `POST /v1/cases/{id}/recover` | 人工重试只读采集，字段 revision/note |
| `GET /v1/cases/{id}/evidence/{evidence_id}/raw` | 下载当前工单原始证据 |

## 测试与构建

```bash
python -m pytest -q
python tools/smoke.py
python -m build
```

测试覆盖闭环、重建应用后的持久性、权限/参数拒绝、竞争领取、取消、人工恢复、跨工单隔离、证据篡改、YARN 错误/截断/重定向及日志指令。HTTP smoke 启动实际服务和一次性 Worker，完成采集与复核，再清理自己启动的进程。

GitHub Actions 推送/PR 时测试并构建，实际结果以 GitHub 为准。架构取舍和后续路线见[架构评审与首版范围](docs/架构评审与首版范围.md)。
