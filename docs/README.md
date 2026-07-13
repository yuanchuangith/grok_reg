# 开发文档中心

这里是项目需求、设计、变更、运维和技术决策的统一入口。

## 文档树

```text
docs/
├─ README.md
├─ 00-project-status/
│  ├─ README.md
│  ├─ current-status.md
│  ├─ known-issues.md
│  └─ progress-log.md
├─ 01-requirements/
│  ├─ README.md
│  └─ requirement-index.md
├─ 02-design/
│  ├─ README.md
│  └─ system-overview.md
├─ 03-changes/
│  ├─ README.md
│  └─ change-index.md
├─ 04-operations/
│  ├─ README.md
│  └─ deployment-index.md
├─ 05-decisions/
│  ├─ README.md
│  └─ decision-index.md
└─ templates/
   ├─ requirement-template.md
   ├─ change-template.md
   ├─ deployment-template.md
   ├─ decision-template.md
   └─ status-update-template.md
```

## 后续开发读取顺序

开始新的开发任务时，按以下顺序了解项目：

1. `docs/00-project-status/current-status.md`：查看当前能力、进度、部署和下一步。
2. `docs/00-project-status/known-issues.md`：查看仍存在的问题、风险和规避方式。
3. `docs/01-requirements/requirement-index.md`：找到相关需求。
4. `docs/03-changes/change-index.md`：确认已经实现过哪些变更。
5. 根据任务继续读取对应的设计、运维或技术决策文档。

不要求每次把所有模板都读一遍；状态页和问题页是必须读取的交接入口。

## 记录规则

开发和讨论过程中默认不自动归档。只有收到明确的写入命令后，才更新这里的文档。

可使用的命令示例：

- `把本次需求写入开发文档`
- `记录本次变更`
- `把部署结果写入文档`
- `记录这个技术决策`
- `把本次需求和实现一起归档`

执行归档时：

1. 新需求写入 `01-requirements/`，并更新需求索引。
2. 已完成的代码或界面改动写入 `03-changes/`，并更新变更索引。
3. 架构、接口和数据结构说明写入 `02-design/`。
4. 部署、回滚和验证结果写入 `04-operations/`。
5. 有明显取舍的技术选择写入 `05-decisions/`。
6. 同步更新 `00-project-status/` 中受影响的当前状态、已知问题和进度日志。

## 编号规范

- 需求：`REQ-YYYYMMDD-NNN`
- 变更：`CHG-YYYYMMDD-NNN`
- 部署：`DEP-YYYYMMDD-NNN`
- 决策：`ADR-YYYYMMDD-NNN`

同一天从 `001` 开始递增。详细文档文件名使用编号加简短英文标识，例如：

```text
REQ-20260713-001-account-model-tags.md
CHG-20260713-001-account-model-tags.md
```

## 安全要求

文档中不得记录密码、私钥、访问令牌、带 Token 的订阅地址、完整代理认证信息或完整账户凭证。服务器地址如确有必要记录，也应与密钥和密码分离。
