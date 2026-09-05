# 参与贡献

## 先读

- `README.md`：产品边界（不作最终判定结论）、规则分层、已知边界
- `CLAUDE.md`：开源仓的数据纪律与验收口令
- `CLA.md`：首个 PR 需签署

## 什么样的贡献最有价值

1. **通用规则**（A/B/U 类）：有公开标准或通行惯例依据的填写规范判定，附单元测试
2. **规则包**：某类行业/表单的专项规则，作为 `config/packs/<name>.yaml` 提交，附合成样本
3. **误报修复**：附能复现的合成页面（`tools/synth_forms.py` 生成），不要附真实扫描件
4. **文档与翻译**

不接受的贡献：引入联网或大模型依赖的判定路径；把系统输出改成「合格/通过」类结论；
任何真实客户数据。

## 提交前

```bash
python tools/check_scrub.py
python -m pytest tests -q
```

两条都过再开 PR。提交信息用 `feat / fix / refactor / docs / test / chore / perf` 前缀。

## 规则怎么写

- 判定逻辑写在 `qaudit/rules_*.py`，参数与依据写在 `config/rules.yaml` 或规则包
- 每条规则的 `clause` 写依据（国标编号或「通用惯例」），不要复制任何组织的内部标准原文
- 召回优先，但要有语境约束（见 README 第四节五层误报抑制）
- 视觉类规则只作提示，置信度如实标注
