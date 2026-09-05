# 发布流程（Gitee + GitHub 双仓）

> 推送与建仓由维护者本人操作；自动化工具只准备到本地提交为止。

## 0. 发布前门禁

```bash
python tools/check_scrub.py          # 必须 scrub OK
python -m pytest tests -q            # 必须全绿
python tools/gen_rules_appendix.py --check
git status --short                   # 必须干净
git check-ignore docs/superpowers/plans  # 必须被忽略
```

## 1. 建仓（一次性）

| 平台 | 仓库 | 设置 |
|---|---|---|
| Gitee | `https://gitee.com/<你的账号>/recordlint` | 开源 · AGPL-3.0 · 语言 Python · 标签：质量管理、OCR、印章检测、离线部署、制造业 |
| GitHub | `https://github.com/<你的账号>/recordlint` | Public · License AGPL-3.0 · Topics：qms, ocr, quality-records, seal-detection, offline, manufacturing, china |

两边的简介一致：

> 扫描件质量记录填写规范自动预审：OCR + 视觉判定 + 复核工作台，全离线，输出疑点清单供人工终审。RecordLint（质录检）

## 2. 推送

```bash
git remote add gitee  https://gitee.com/<你的账号>/recordlint.git
git remote add github https://github.com/<你的账号>/recordlint.git
git push gitee main
git push github main
git tag -a v1.2.0 -m "RecordLint 开源首版"
git push gitee v1.2.0
git push github v1.2.0
```

Gitee 建议开启「镜像仓库 → 从 GitHub 同步」或反向，二选一作主仓，避免两边手工同步。

## 3. 首发后 24 小时内

- Gitee：补「项目介绍」页（复制 README 第一节）、上传 1 张复核工作台截图、开「Issues」与「PR」
- GitHub：Release v1.2.0 附 `samples/synthetic/` 说明与 `report.html` 截图
- 首发内容（知乎长文、公众号稿）见 `D:\dev\pattern-radar\报告\蒸馏\C3-开源云托管SaaS\发布\`（不在本仓）

## 4. 每次发版

1. `pyproject.toml` 与 `qaudit/cli.py` 的 `ENGINE_VERSION` 同步改
2. 跑第 0 节门禁
3. `git tag -a vX.Y.Z` 并推送到两个远端
4. 规则库有变动时，Release 说明里列出变动的规则号与理由（与界面「变更理由」口径一致）
