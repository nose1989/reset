# Coding Prompts

这个文件保存需要在本项目中长期参考的提示词和上下文。以后在这个仓库执行开发、脚本或交付任务时，先阅读本文件，再结合 `RULES.md` 判断当前任务的范围和完成标准。

## Create zip with 10 txt

### 用途

作为历史任务上下文参考。它说明了一个已经完成的交付：生成 10 个 txt 文件，并打包为一个 zip。

### 原始上下文摘要

用户请求将 10 组 `email:password` 组合分别写入 10 个 txt 文件，并打包为一个 zip 归档。任务已经完成。

关键细节：

- zip 文件位置：`/home/ubuntu/10_txt_files.zip`
- txt 文件命名：`01.txt` 到 `10.txt`
- 内容格式：每个 txt 文件包含一组 `email:password`
- 临时目录：`/home/ubuntu/txt_package`
- 验证结果：zip 内包含正好 10 个 txt 文件，命名正确

当前状态：

- 任务已完成
- zip 文件已生成并验证
- 没有待处理事项或阻塞

### 后续使用方式

- 如果再次处理类似任务，保持文件命名、内容格式和验证方式一致。
- 不要假设 `/home/ubuntu/10_txt_files.zip` 或 `/home/ubuntu/txt_package` 在新的环境中仍然存在；需要时重新生成并验证。
- 不要把真实密码、密钥或个人凭据提交到仓库。
