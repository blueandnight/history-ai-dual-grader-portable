# Windows 便携版构建与使用

## 使用运行包

将 ZIP 完整解压到可写目录，双击 `启动批改器.bat`。保留同目录 `_internal` 资源。无需安装 Python 或 Node.js。默认仅监听本机，启动器自动选择可用端口并打开工作台；可用 `停止批改器.bat` 停止。需要手机采集时，在可信局域网运行 `启动批改器.ps1 -EnablePhoneAccess`，使用系统生成的受限访问码。

首次启动为空白系统，没有模型密钥、学生资料或历史成绩。模型服务由用户在系统管理中配置。便携不代表 AI 离线：授权调用第三方模型时，实际答卷图片会发送给用户选定的服务商，可能收费。

## 从源码重新构建

需要 Windows x64、Python 3.12 x64、Node.js 22 或更新的兼容版本、pnpm，以及首次下载依赖的网络。

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

输出目录必须为空。构建器使用显式资源清单，不复制数据库、配置、用户数据及测试目录。运行包附带第三方版权声明。构建工具版本见 requirements-build.txt，锁定前端版本见 pnpm-lock.yaml。软件未签名，Windows 可能提示未知发布者；本地验证不能保证所有 Windows/杀毒软件组合均兼容。

## 数据与维护

`data/`、`config/`、`backups/` 在便携根目录首次运行时生成。不要把使用后的目录作为公开发行包。备份需包含图片与数据库及配置，更新前停止任务和服务。不要直接覆盖、清空现用数据。删除功能沿用原有范围预览及确认。

## 发布范围

仅面向 Windows 本地便携使用。共用源码保留必要访问控制和本地手机接入功能，不提供生产服务器、反向代理、云端运维配置及部署说明。本机免密管理服务不得暴露到公网。自有部分采用 Apache-2.0，四个 Prompt 原样保留；许可范围见 LICENSING.md。
