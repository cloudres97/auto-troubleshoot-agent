# 自动诊断 Agent

面向 **Windows 11 执行器 + WSL2 工作台** 的通用只读诊断原型。工单使用环境、对象类型、对象 ID 和故障描述，适用于登记容器、虚机、Java/Go 服务、网络、存储和 YARN 应用。

**当前使用确定性检查，尚未接入公司模型。** `demo` 返回明确标识的模拟数据。真实 YARN 适配器经过协议模拟测试，尚未在公司环境验收。FAILED 仅表示应用失败，不自动认定根因。日志/配置采集、变更执行、知识库和群机器人尚未实现。

## 一键启动（推荐）

在 WSL2 的仓库目录只需执行：

```bash
bash start.sh
```

无需激活虚拟环境、`source .env` 或开三个终端。脚本自动创建 `.venv`，缺少依赖时安装；首次没有 `.env` 时生成三个不同的随机令牌，然后统一启动执行器、网页 API、Worker，并验证认证和对象目录。Ubuntu 如未安装 venv 支持，先执行 `sudo apt install python3.10-venv`（Ubuntu 22.04）。首次下载依赖需要可用的包源/公司代理。

出现 `READY` 后，Windows 浏览器打开本机端口 8080，从 `.env` 复制 `DIAG_API_TOKEN` 的值到网页顶部，点击“加载环境、对象与工单”，然后选择环境、类型及诊断对象。示例包含 `demo-java`、`demo-go`、`demo-docker` 等演示对象；不会要求填写 YARN ID。所有演示均不代表真实采集能力。

按 `Ctrl+C` 统一停止三个进程；再次运行同一命令即可重启。日志在 `data/logs/`，工单和令牌保留。已有 `.env` 不会被覆盖；若配置为空或三个令牌重复，启动会失败，需要先修正。程序直接读取 `.env`，兼容 CRLF 和 BOM，`$`、`#` 均按字面值读取，不作 Shell 展开；不要在配置值末尾添加行内注释。

已有三个服务运行时，先在各窗口按 `Ctrl+C` 停止再使用脚本。端口冲突时脚本不会杀掉现有进程。API 端口可用 `bash start.sh --port 8082` 指定。此入口用于本机开发/演示；配置远程 Windows 执行器时请按下文分组件部署。

更新旧版本：先停止所有旧进程并备份数据库，再在当前功能分支执行 `git pull --ff-only`，最后运行 `bash start.sh`。默认 SQLite 可在停机后备份：

```bash
mkdir -p backups
if [ -f data/diagnosis.db ]; then
  python3 -c 'import sqlite3,datetime; src=sqlite3.connect("data/diagnosis.db"); dst=sqlite3.connect("backups/diagnosis-"+datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")+".db"); src.backup(dst); dst.close(); src.close()'
fi
```

`backups/` 已被 Git 忽略，备份仍需按公司要求限制访问。首次新版本启动会在 cases/evidence 表增加通用对象字段，将旧 application_id 回填为 YARN 对象，不删除工单、证据或报告。不支持直接用旧代码降级写入新数据库；需要回退时使用备份。PostgreSQL 部署仍须单独验收备份、事务与升级。

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

浏览器访问本机端口 8080，输入操作员令牌，加载对象目录后选择 `demo / Java 服务 / demo-java` 创建工单；刷新详情查看结果，填写复核员令牌与说明后批准报告。令牌仅留在页面内存中。两个服务的 `/health` 用于就绪检查，`/docs` 提供接口定义。

默认 SQLite WAL，数据库位于 `data/diagnosis.db`，用于单机试用。PostgreSQL 可配置 `DIAG_DATABASE_URL=postgresql+psycopg://...`，凭据通过环境注入。当前支持初始表创建与 v0.1→v0.2 的有限增量升级，复杂升级仍需引入 Alembic 迁移。不能把 SQLite 验证视为 PostgreSQL 的生产验收。

## Windows 执行器与真实 YARN

Windows Python 3.13 可安装同一包，使用 `.venv\Scripts\python.exe`。为执行器安全注入 `DIAG_EXECUTOR_TOKEN` 和 `DIAG_ENVIRONMENTS_FILE`；后者指向管理员维护的注册文件：

```json
{
  "test-yarn": {
    "targets": {
      "flink-orders": {
        "target_type": "yarn_application",
        "label": "订单 Flink 作业",
        "adapter": "yarn",
        "base_url": "https://your-resource-manager.example:8090",
        "application_id": "application_123456789_0001"
      }
    }
  }
}
```

调用方不能提供 URL。执行器只调用固定 `/ws/v1/cluster/apps/{application_id}`，检查应用白名单、不跟随重定向、保留 TLS 验证、限制输出 64 KiB、最多同时采集两次。当前仅支持可直接访问的 REST API，**不包含 Kerberos/SPNEGO、堡垒机、MFA 或 HA 重定向认证**；这些场景需要扩展并验收认证适配器。

网页选择的是 `flink-orders`；具体 YARN application_id 仅存在于此类型的执行器配置中。旧版 applications 白名单与旧 API 的 application_id 字段暂保留兼容。其他对象类型的登记和采集能力边界见[通用诊断对象](docs/通用诊断对象.md)。

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
| `GET /v1/targets` | 查询已登记环境与对象，仅包含公开元数据 |
| `POST /v1/cases` | 创建，字段 environment_id/target_type/target_id/symptom |
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
