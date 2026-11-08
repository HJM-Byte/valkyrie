## 改了什么

<!-- 一句话说清动作和对象 -->

## 验证

- [ ] `python -m compileall app tools`
- [ ] 相关 `tools\selftest_*.py` 通过
- [ ] 没有写生产 `data/`、`diary/`、`backups/`
- [ ] 没有新增本机绝对路径、真实姓名、IP 或 API Key
- [ ] 涉及记忆/情绪/人格的改动，说明了数据迁移与回滚

## 边界

- [ ] 没有提交 `.env`、`data/`、`models/`、`docs/`、APK keystore
- [ ] 第三方素材在 PR 里写明了来源和许可证
