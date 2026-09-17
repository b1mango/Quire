# 卷帙图标

已选方向：C「书库抽本」。`C-the-pull-3d.svg` 为本轮可编辑立体版本，`docs/assets/quire-icon.png` 为应用使用的 1024×1024 RGBA 主图。

- 保留五本书与珊瑚色抽本，增加封面厚度、纸页、高光与书脊明暗。
- 背景为真实透明，没有方形底板、背景纹理或投影底座。
- `C-3d-preview.png` 展示浅/深底，以及 16、32、64、96 px 实际缩放效果。
- 构建脚本从 PNG 生成各尺寸 ICNS，避免 Quick Look 输出白底。
- SVG 由项目原生矢量直接制作、macOS `sips` 渲染；没有调用付费图像 API。初始 A–F 候选保留供追溯。

修改 SVG 后更新主图：

```sh
sips -s format png design/icons/C-the-pull-3d.svg --out docs/assets/quire-icon.png
```
