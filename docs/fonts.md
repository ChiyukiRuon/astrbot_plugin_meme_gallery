# 字体文件夹与混排

在 `font_dir` 填写字体文件夹的绝对路径，不要加引号。Windows 示例为 `D:/Fonts`；Docker 示例为 `/AstrBot/data/fonts`，需要事先把字体复制或挂载到容器内该目录。支持 TTF、OTF、TTC、OTC，只扫描目录第一层；字体集合使用第一个字体。

在 `font_order` 中逐项填写文件名，并按希望的优先级排序，例如：

```json
{
    "font_dir": "/AstrBot/data/fonts",
    "font_order": [
        "NotoSansCJK-Regular.ttc",
        "NotoColorEmoji.ttf",
        "NotoSansArabic-Regular.ttf"
    ]
}
```

第一项是主要字体。渲染时按顺序检查字体的字符映射表，选择包含整个字素的字体；未列出的文件按文件名排序追加。排序列表必须填写目录中实际存在的文件名。若希望 Emoji 优先采用 Emoji 字体，可把它移到前面；其他字符会继续向后寻找支持的字体。配置界面中的列表顺序就是优先级，不需要修改字体文件名。

填写目录后只使用该目录中的字体，不再追加系统字体。留空时自动尝试微软雅黑、Noto Sans CJK、文泉驿正黑以及常见系统 Emoji 字体。升级前已有的 `font_path` 在目录留空时仍作为优先字体读取；建议迁移到新配置。无可用字体时返回中文提示；损坏或运行环境无法读取的字体会跳过并记入日志。

字体映射表、字体选择和文本排版均有有界内存缓存。目录内容、字体修改时间或大小、优先级变化会在下一次渲染时重新选择缓存，不需要每次重读字符映射表。替换字体后如未生效，可重载插件。

## Emoji 与多语言

帮助图、图库名、别名和缩略图标签共用字体回退、宽度计算及绘制逻辑。换行和省略不会拆开组合音标、肤色 Emoji、国旗或 ZWJ 家庭组合。连续使用同一字体的文字一起绘制，避免逐字符绘制破坏连写。

彩色字形使用 Pillow 的内嵌颜色渲染；只提供固定像素尺寸的位图 Emoji 字体会按可用尺寸绘制后缩放。是否最终显示彩色及组合字形，仍取决于字体内容、FreeType/Pillow 支持和文字整形引擎。字符映射表只证明包含组成字符，并不保证字体具有某个组合 Emoji 的合字。所有字体都缺字时使用主要字体的缺字字形；需要补充对应字体。

若 Emoji 从方框变为空白，可能是字体声明包含字符，但当前渲染器无法绘制其彩色格式。选择字体时会检查实际像素；空白或绘制失败会继续回退，并按字体记录一次警告。全部候选字体都无法绘制时显示占位框，避免字符静默消失；正常空格不受影响。这项检查也会缓存。

已用 Google Fonts 的 `NotoColorEmoji-Regular.ttf` 复现乌龟 🐢 空白：该文件为 COLRv1 格式，本地 Pillow 12.3.0 / FreeType 2.14.3 输出无像素。更换为 [Noto Emoji 官方 CBDT 版本 NotoColorEmoji.ttf](https://raw.githubusercontent.com/googlefonts/noto-emoji/main/2D/fonts/NotoColorEmoji.ttf) 后，已验证能绘制彩色乌龟。将新文件放入配置的字体目录，把 `font_order` 中的旧文件名改为 `NotoColorEmoji.ttf`，再重载插件。仅重命名原字体不能转换格式；Docker 中仍需确认实际 Pillow/FreeType 构建支持该格式。

阿拉伯文等复杂文字和 Emoji 合字建议使用带 **libraqm** 的 Pillow。可在 AstrBot 使用的 Python 环境检查：

```sh
python -c "from PIL import features; print(features.check_feature('raqm'))"
```

结果应为 `True`。从右到左文字在没有 libraqm 时会返回明确提示；部分复杂的从左到右文字在缺少整形支持时仍可能显示不正确。双向文字先计算方向及视觉顺序，再按字体分段整形；暂不支持双向隔离控制符等超出当前双向算法支持范围的输入。跨字体的连写效果也受字体覆盖范围限制，应尽量提供覆盖完整语言的字体。
