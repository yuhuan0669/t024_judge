# 被测模型系统提示词

`system_prompt.txt` 是被测模型（`octa-carbon-sft-v0114`，基座 `octa-carbon-v1`，上下文 8192 token）推理时使用的系统提示词。文件名不带版本号，版本以 git 历史为准，改动用 `git diff` / `git log -p tested_model/system_prompt.txt` 查看。

- 模型微调时未带系统提示词，推理时带；下次微调须统一（见 `HANDOVER.md` 第 6 节）。
- 哈希为去除首尾空白后 UTF-8 内容的 sha256 前 12 位，与 `gen_answers.py` 记录的 `system_sha` 一致。

| 版本 | 提交 | 哈希 | Qwen3 token | 状态 | 依据 |
|---|---|---|---|---|---|
| 现行版（来源 `D:\system.txt`） | fa86474 | 4bde2874cd92 | 643 | 已部署 | A/B 的 B 组 |
| 精简新稿（当前文件） | 本目录最新提交 | 66133d91c7df | 566 | 候选：A/B 通过，待上线确认 | A/B 的 C 组，`results/ab_system_prompt_20261028.md` |

新稿上线前需确认：部署的服务确实不调用工具、不获取实时数据（新稿写明"只输出文本，不调用工具，不控制设备"）。确认上线后，把状态改为"已部署"，并把现行版改为"已替换"。
