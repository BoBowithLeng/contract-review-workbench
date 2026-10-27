# 第三方组件

本项目自有代码使用MIT许可证，第三方代码、依赖及组件仍适用各自许可。

- Marked 18.0.14：MIT，见 `reader_ui/vendor/LICENSE.marked`；来源 https://github.com/markedjs/marked 。
- DOMPurify 3.4.16：采用其双许可中的Apache-2.0选项，见 `reader_ui/vendor/LICENSE.dompurify`；来源 https://github.com/cure53/DOMPurify 。
- Windows构建涉及Tcl与Microsoft WebView2 SDK，其许可原文在 `desktop/third_party/`。运行时依赖和Python组件的许可由构建脚本收集到分发包的“第三方许可”目录。
- Python与桌面构建依赖分别见 `requirements.txt`、`desktop/requirements-build.txt`；这些依赖不因本仓库MIT许可而改变各自许可。

公开源码不包含旧演示包使用的第三方合同模板及由其产生的真实审查记录。自有合成测试输入不用于声称实际模型审查质量。
