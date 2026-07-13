# CHG-20260713-002 CPA refresh_token 刷新与 OIDC 兜底

## 基本信息

- 状态：已完成
- 完成日期：2026-07-13
- 对应需求：REQ-20260713-002
- 部署记录：DEP-20260713-002

## 变更内容

- 成功账户操作区增加“刷新凭证”按钮。
- 点击后确认刷新范围，并启动单账户 CPA 刷新任务。
- 增加 xAI OAuth refresh_token 请求能力。
- 增加现有 CPA 文件读取、Token 刷新、模型验证和原子回写模块。
- refresh_token 失败后自动复用现有完整 OIDC 获取流程。
- OIDC 新凭证验证失败时恢复刷新前的旧凭证。
- 增加 `[CPA-REFRESH-100..130]` 关键节点日志。

## 影响文件

- `webui/app.js:150`
- `webui/styles.css:86`
- `web_dashboard.py:964`
- `cpa_xai/oauth_device.py:181`
- `cpa_xai/refresh.py:36`
- `cpa_xai/__init__.py:1`
- `scripts/backfill_cpa_xai_from_accounts.py:76`

## 行为变化

### 变更前

只要 CPA 文件存在，补全任务就跳过账户。Token 失效后只能手动删除文件或使用命令行强制重新获取。

### 变更后

用户可以直接在成功账户页面点击“刷新凭证”。系统优先快速刷新 Token，失败时自动重新完成 OIDC 获取，并保护旧凭证不被失败结果覆盖。

## 兼容性与数据影响

- CPA JSON 保持 CLIProxyAPI 兼容字段。
- refresh_token 返回新 refresh_token 时同步保存，未返回时保留原值。
- 更新 `access_token`、`refresh_token`、`expired`、`expires_in` 和 `last_refresh`。
- 不记录或输出完整 Token。

## 验证结果

- Python 语法检查通过。
- JavaScript 语法检查通过。
- 模拟测试验证 refresh_token 成功写入。
- 模拟测试验证新 Token 探测失败时旧文件不变。
- Web 自动化测试验证按钮、确认框和任务参数。
- 线上真实账户 refresh_token 刷新成功。
- 线上刷新未触发 OIDC 兜底。
- 刷新后 `grok-4.5` 模型测试返回 HTTP 200。

## 回滚方式

- 恢复变更前的 Web 前端、Web 后端、CPA OAuth 和补全脚本。
- 删除 `cpa_xai/refresh.py`。
- 已成功刷新的 CPA 凭证仍为合法格式，无需回滚数据。

