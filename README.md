# 卷帙 / Quire

一款面向 macOS 的本地漫画与小说采集工具，把网页内容整理成方便离线阅读的文件，数据保存在本机。

目前支持公开漫画网页和本地图片，漫画可导出为 PDF、CBZ 或图片 ZIP。小说采集、桌面界面和安装包正在开发中，当前通过命令行使用。

## 开始使用

需要 Python 3.12 或更新版本。在终端中执行：

```sh
git clone https://github.com/b1mango/Quire.git
cd Quire
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[core]'
```

将漫画网页保存为 PDF：

```sh
quire manga '漫画网页地址'
```

将本地图片整理为 PDF：

```sh
quire local '图片文件夹路径'
```

替换示例中的地址或路径后运行，完成时会显示文件保存位置。

[使用示例与测试站点](测试站点.md) · [项目进度](项目进度.md)
