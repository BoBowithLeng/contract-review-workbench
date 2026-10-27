# 合同审查台

一个把合同审查、补充事实、讨论意见、确认改稿和 Word 交付放在同一工作台里的个人项目。由 **波波与棱**一起设计和开发。

[2分钟视频演示](https://www.bilibili.com/video/BV1H3pY6WEHQ/) · [Windows 0.1.7 试用包](https://pan.baidu.com/s/1nSsNvOT3U6mc6Mgp7KR0sA?pwd=zc9y)（提取码 `zc9y`）

## 维护节奏

**更新会很慢，很慢。** 目前主要精力在求职，暂时没有稳定的更新计划。

审查清单需要结合具体业务、交易立场和合同阶段严格定制，不能靠塞进一份通用清单就覆盖所有合同。我们后续整理的清单还没有全部进入这个版本，现有内置清单只是起点。前端在长文阅读、讨论、修改确认等环节的流畅度和使用体验也还有不少需要打磨的地方。

愿意试用、讨论或提交改进都欢迎；Issues 和 PR 的回复、合并可能比较慢。源码先放出来，供有需要的人研究、修改和继续做。

## 目前能做什么

- 上传 DOCX，说明我方立场、合同类型和审查要求。
- 根据检查清单整理准备问题，补充交易事实后进行全文审查。
- 将审查意见与原文位置对应，逐条阅读和讨论。
- 对照修改内容，由人工确认后写入合同。
- 导出清稿、修订／批注版 Word 和审查报告，必要时对改后合同复审。
- 管理检查清单，复制编辑或使用 Excel 导入。
- 通过兼容模型 API 调用模型，在设置中配置自己的地址、密钥及模型名称。

内置清单当前包括通用合同24项、软件开发合同客户立场31项。它们是可调整的检查要求，不是完整法条数据库。模型的风险判断和修改文字仍需结合具体事实人工核对。

## 直接试用

下载上方 **Windows 10/11 x64 的0.1.7历史试用包**，完整解压后双击 `合同审查台.exe`。该包自带演示记录，看示例不需要模型密钥；新审查、讨论和复审需要填写自己的模型 API，并使用相应服务额度。操作说明在包内 `先读我.txt`。

**这个公开源码版本从空工作台开始。** 它不包含旧包中的第三方合同模板与真实演示记录，也不包含个人合同、运行记录或模型密钥。因此自行构建的程序与历史视频／试用包在初始示例数据上有所不同，主体审查流程相同。历史包中的第三方演示材料不属于本仓库的MIT授权范围。

## 从源码运行

当前主要运行和验证环境是 **Windows、Python 3.12**。模型密钥的本机持久化使用 Windows DPAPI；其他系统暂未提供对应持久化实现。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe reader_server.py --port 8519
```

打开 <http://127.0.0.1:8519/>。首次列表为空，在界面上传你有权使用的 DOCX。终端需要保持运行，Ctrl+C停止服务；也可使用 `启动阅读台.ps1`。

服务只绑定本机回环地址。合同与操作记录保存在项目的 `runtime/` 中；密钥由当前 Windows 账户加密保存。使用模型时，合同内容会发送到你配置的模型服务。

源码还保留可选的 Codex CLI 引擎。使用它需自行安装并登录 Codex；可设置 `CONTRACT_REVIEW_CODEX_BIN` 指定程序、`CONTRACT_REVIEW_CODEX_WORKSPACE` 指定独立工作目录。默认目录是本项目 `runtime/codex-workspace`。它不是运行兼容 API 模式的必需依赖，桌面包不包含这条引擎路线。

`app.py` 和 `启动审查台.ps1` 是早期 Streamlit 兼容入口；当前完整流程在 `reader_server.py` 与 `reader_ui/`。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 前端测试使用Node.js，无需npm安装
Get-ChildItem tests\test_*.cjs | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "前端检查失败：$($_.Name)" }
}
```

测试使用临时目录、合成输入和模拟模型回答。少数历史真实样本检查会在对应材料不存在时跳过；公开仓库不提供这些材料。不要把夹具测试结果当作法律意见正确率。

开发 HTTP 服务时可以调用 `create_server(root=..., resource_root=..., state_dir=..., jobs_dir=...)` 指定独立数据目录；单独换端口不会自动隔离 `runtime/`。

## Windows桌面构建

需要Windows x64和Python 3.12 x64：

```powershell
python -m venv runtime/desktop-build-env
.\runtime\desktop-build-env\Scripts\python.exe -m pip install -r desktop/requirements-build.txt
.\runtime\desktop-build-env\Scripts\python.exe desktop/build_windows.py --output-dir 'D:/contract-review-build-0.1.7' --version 0.1.7
```

输出目录必须为空。公开源码构建空数据种子，程序首次打开没有演示合同。桌面界面使用 WebView2 Runtime，构建会收集第三方组件许可。程序未购买代码签名证书；分发前应在另一台Windows机器实际检查启动、审查与Word交付。

## 当前范围

- 输入为普通DOCX正文及受支持表格，不提供OCR；复杂修订、文本框、嵌套表格等内容可能被拒绝，以免静默漏读。
- Word修改支持已实现的段落与新增位置，复杂跨段重写等结构仍受限制。
- 尚无多人账户、企业权限审批、法律专用数据库或自动更新。
- 更广泛合同类型、复杂Word排版、模型误报漏报和前端体验还需要持续验证。

## 目录

| 位置 | 内容 |
| --- | --- |
| `reader_server.py`、`reader_ui/` | 当前网页工作台与本机API |
| `review_jobs.py`、`review_preparation.py`、`general_review.py` | 准备、审查及任务记录 |
| `prompts/`、`builtin_checklists.json` | 提示词与内置检查清单 |
| `clause_discussion.py`、`draft_plan.py`、`docx_delivery.py` | 讨论、修改计划与Word交付 |
| `desktop/`、`desktop_launcher.py` | 桌面构建与启动 |
| `tests/` | Python及前端测试 |

## 联系与许可

反馈可提交 Issue，或发邮件给棱：**lengwithbobo@163.com**。报告问题时请提供复现步骤、版本与已脱敏的必要片段，勿直接上传真实交易合同或API密钥。

自有代码及随库文档采用 [MIT许可证](LICENSE)。第三方组件保留各自许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
