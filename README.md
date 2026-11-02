# 历史试卷 AI 双评批改器

**Windows 便携版 · 监督式开源 Beta · Apache-2.0（项目有权许可的部分）**

面向历史教师的试卷批改工具：客观题识别与确定性计分、主观题双模型独立评分建议、教师快捷总分终评、答卷关联、统一成绩、逐题正确率及 Excel/CSV 导出。支持一套试题复制为独立考试、多次上传汇总、批次停止/恢复与删除、手机受限采集。

**AI 辅助开发说明：本项目由维护者发起、设计和维护，开发中大量使用 Codex、DeepSeek 等 AI 编程工具生成、修改、调试和测试代码，并非全部由维护者手工独立编写。** AI 工具与模型供应商不是项目版权所有者；不对不明权利内容或不可版权化输出主张独占版权。详见 [AI_DEVELOPMENT.md](AI_DEVELOPMENT.md)、[NOTICE](NOTICE) 和 [许可范围](LICENSING.md)。

## 下载 Windows 便携版

- [最新发布](https://github.com/blueandnight/history-ai-dual-grader-portable/releases)
- [本次 Windows x64 便携运行 ZIP](https://github.com/blueandnight/history-ai-dual-grader-portable/releases/download/v0.2.2-beta.1/history-ai-dual-grader-windows-x64-v0.2.2-beta.1.zip)
- [对应第三方源码 ZIP](https://github.com/blueandnight/history-ai-dual-grader-portable/releases/download/v0.2.2-beta.1/history-ai-dual-grader-third-party-source-v0.2.2-beta.1.zip)
- [SHA-256 校验文件](https://github.com/blueandnight/history-ai-dual-grader-portable/releases/download/v0.2.2-beta.1/SHA256SUMS.txt)
- [本版本项目源码](https://github.com/blueandnight/history-ai-dual-grader-portable/archive/refs/tags/v0.2.2-beta.1.zip)

普通教师只需运行 ZIP；第三方源码 ZIP 为再分发合规与修改库而提供，不是启动依赖。无需安装完整 Python、Node.js 或开发环境。请从 Release 附件下载运行包，GitHub 的 Source code ZIP 是开发源码。

## 解压与启动

1. 将运行 ZIP **完整解压**到可写目录，保留 `_internal` 资源文件夹。
2. 双击 `启动批改器.bat`，等待浏览器打开本机工作台；启动器自动选择可用端口。
3. 停止时使用 `停止批改器.bat`。不要在评分过程中直接拔出移动硬盘或强制断电。

程序面向 Windows x64。候选构建内部仍显示原构建名称及 `0.2.2-beta-dev` 标识，这是此次发布使用的已审核程序；公开发布版本为 `v0.2.2-beta.1`，本次没有重新编译或改写评分逻辑。未签名程序可能被 Windows 或安全软件提示为未知发布者，不代表已验证所有系统组合。

## 配置模型 API Key

打开工作台“系统管理”，自行选择/配置模型服务、AI-A/AI-B、模型名称和 API Key。也可参考不含凭据的 `.env.example`。本项目不提供或共享作者账号、密钥、额度或私人服务器。

**便携版在本机运行，不代表 AI 完全离线。** 调用第三方模型时，答卷图片/文本会发送给用户自行配置的服务商，可能产生费用；保留上传前实际外发预览、隐私核对及调用授权。用户须遵守所选模型服务条款，不能把源码许可证视为模型服务的授权。

## 教师操作流程

选择/创建考试并核对答案和逐题分值 → 上传答卷 → 核对实际外发内容并授权批改 → 处理异常及必要终评 → 查看成绩、正确率与分析 → 导出 Excel/CSV。

- **客观题**：同一考试跨多次上传汇总；有效结果确定性计分，异常集中复核；可独立统计正确率和导出。
- **主观题**：双模型评分建议、严格共识批量采纳、快捷总分终评；教师可采用某侧分数或自定总分，逐点评阅为可选。
- **整卷成绩**：同源答卷可按现有可靠机制对应；分开上传可由教师核对并手动关联。未关联的两侧成绩仍独立保留、统计和导出，不按上传顺序猜测考生。
- **成绩完整性**：合法零分与缺失值区分；待复核、部分范围、版本不一致不会冒充完整整卷成绩。教师只确认总分时，不伪造教师确认的小题分数。
- **快速匿名模式**：不强制预先创建正式考试或导入花名册。花名册可选，旧身份码系统不作为正常阅卷门槛。

AI 评分用于辅助教师判断，涉及学生正式学业评价的重要结果须经教师审核。本软件不作为无人审核的高风险自动教育决策系统宣传。服务商针对自动教育决策等用途的限制详见 AI_DEVELOPMENT.md。

## 数据与隐私

首次运行为空白系统，不带学生答卷、花名册、成绩或模型配置。`data/`、`config/`、`backups/` 是本机运行资料，不要提交到公开仓库或将使用后的文件夹再次作为发行包。升级前停止任务与服务并备份数据库、图片、配置；保留旧程序，谨慎恢复。

默认管理服务只监听本机。需要手机采集时，在可信局域网使用 `启动批改器.ps1 -EnablePhoneAccess` 并通过受限临时访问码接入；不共享管理员凭据。不应把本机免密服务直接开放到公网。

## 从源码构建 Windows 便携版

需要 Python 3.12 x64、Node.js 22 或更新的兼容版本、pnpm。首次构建需要下载公开依赖。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build-lock.txt
cd frontend
pnpm install --frozen-lockfile
pnpm exec vue-tsc -b
pnpm build
cd ..
.\构建便携版.ps1 -Python ./.venv/Scripts/python.exe -OutputDir ./dist/portable
```

输出目录须为空；不带用户数据。构建器将许可文件一并复制，执行空白启动及换路径检查。详细说明见 [INSTALL.md](INSTALL.md)。此仓库仅公开 Windows 便携运行所需源码及必要共用模块，不含原私人 Git 历史或云端生产部署资产；共用文件中的访问控制代码保留。

## 验证范围与局限

已在隔离环境用合成数据与模拟模型验证：考试创建/复制、跨任务客观题汇总、主观题终评、可靠关联、统一成绩、三种导出、空白启动、中文空格路径搬迁及不依赖 Python/Node 的启动入口。已有136项前端、22项相关后端测试通过；发布准备仅复核文件、哈希和许可，没有再次运行全量测试或付费模型。

这些结果**不证明真实学生答卷的 OCR 或评分准确率**。未验证全部 Windows/安全软件组合；不带数字签名。少数既有独立服务/云端测试前置条件与当前集成入口不同，不能宣称全仓所有测试均通过。问题反馈请使用 [Issues](https://github.com/blueandnight/history-ai-dual-grader-portable/issues)，不要附带真实学生资料、密钥或未脱敏诊断包。

## 许可证与第三方

项目有权许可部分采用 [Apache License 2.0](LICENSE)，具体范围见 [LICENSING.md](LICENSING.md)。第三方库、模型、商标与用户上传材料不归项目作者所有，也不自动改为 Apache-2.0。

OCR 三个权重保留经核验的 Apache-2.0 来源；FFmpeg、GEOS 保留 LGPL、对应源码与替换动态库权利；微软运行组件适用自身再分发条款。完整声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 `third_party/licenses/`。对应第三方源码与运行包在本次 Release 同时提供。
