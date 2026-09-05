# RecordLint（质录检）

**先读 [README.md](README.md)。** 规则分层（通用层 A/B/U + 规则包 F）、安装运行、产出物、
印章状态模型、已知边界，全部写在那里，本文件不复述。全景架构见 `docs/系统全景手册.md`。

## 不可逾越的产品边界

**本系统不作最终判定结论。** 输出是「疑点清单」，供质量部人工终审。
任何把系统输出表述为「合格 / 通过」的改动都越界了。

## 开源仓的数据纪律

- **任何真实客户的质量记录、OCR 缓存、印章裁图、判定数据库一律不得进入本仓**。
  仓内样本只允许 `tools/synth_forms.py` 生成的合成表单（`samples/synthetic/`）。
- `out/`、`*.db`、`config/archives.yaml`、`*.npz` 已在 `.gitignore`；清理 `out/` 前确认没有别人的数据。
- 规则包（`config/packs/`）里只放示例包；任何组织的真实规则包属于该组织，不进本仓。
- `tools/check_scrub.py` 是脱敏门禁（纳入 pytest）：仓内不得出现原客户、原行业、内部标准的标识。
  新增文档或夹具前先跑一遍。

## 依赖例外

`qaudit/web/static/vendor/` 下的 htmx 与 Chart.js **要纳入版本管理**——它们是随包分发的
交付件，离线环境无法联网重新获取，不是可再生依赖。

全系统离线运行：OCR 用 RapidOCR，视觉判定用 OpenCV 经典算法，**不调用大模型、不联网**。
引入任何联网或大模型依赖前必须先确认。模型训练依赖 sklearn，部署侧推理只用 numpy——
不要给推理路径增加运行时依赖。

## 许可

AGPL-3.0-or-later（见 `LICENSE`）。外部贡献需签署 `CLA.md`。**不要改成 BSL/SSPL/闭源**。

## 验收口令

改完代码或文档后按顺序跑，全过才算完成：

1. `python tools/check_scrub.py` 输出 `scrub OK`
2. `python -m pytest tests -q` 全绿
3. `python -m qaudit.cli audit samples/synthetic/batch-0001 --out out/smoke` 能出 `report.html`
4. 改了 `config/rules.yaml` 或规则包：`docs/系统全景手册.md` 附录 A 与之一致（由脚本重生成，不手改）
5. 涉及界面的改动：`python -m qaudit.cli serve` 起服务，六个入口各开一次无报错
6. **对外发布前**：`docs/superpowers/` 是内部计划目录（含脱敏词表与原项目背景），不得随公开仓发布——发布分支上删除该目录，或把它移回 `D:\dev\pattern-radar`

## 其他

- `pip install -r requirements.txt` 重建环境。
- 未经明确指示不提交、不推送。
