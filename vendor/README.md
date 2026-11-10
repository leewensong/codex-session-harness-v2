# 内置 adaptor 源码

`claude-code-bridge/` 是 2026-11-11 从同一工作区的共享 adaptor 工程复制的源码快照，供 V2 独立克隆与开发。原工程未改动，V1 仍可继续使用原目录。

- `agent_adaptors/` 和 `pyproject.toml` 是 V2 unified 模式的实际依赖；`uv.lock` 记录相对仓库内的 editable 路径。
- `tests/`、示例配置、`bridge.py` 与原 README 作为回归及参考保留。原 README 中提到的 `.kiro`、兄弟工程、外部 MCP 服务是历史背景，不是 V2 安装步骤。
- `SOURCE-MANIFEST.json` 记录本次快照的源文件 SHA-256（统一换行为 LF 后计算，避免 Git 跨平台换行转换影响校验）。以后有意修改内置源码时应说明来源和差异；该清单不是自动更新的构建产物。
- 未复制运行时状态、虚拟环境、日志、凭据或包缓存。
- 原目录未附带许可证文件，本次不添加开源许可证声明。

更新时先审查共享工程差异，复制需要的源码变更，再执行 V2 和内置 adaptor 回归；不要用整个上级目录覆盖此目录。
