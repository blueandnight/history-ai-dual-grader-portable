# Windows 便携版第三方许可声明

本文件只覆盖本次 Windows x64 便携包实际分发的组件，不将 pip、Vite、Vitest、TypeScript、pefile、altgraph 等纯构建/测试依赖列为运行组件。所有者已选择 Apache-2.0，仅适用于有权许可的项目内容；四个 Prompt 的内容及既存权利来源保持不变。第三方代码、模型、商标的权利仍归各自权利人，不属于项目作者。

A＝无需特别再分发授权（仍保留来源说明）；B＝允许商业使用及二进制分发，须履行所列条件；C＝权利尚待确认；D＝不允许当前方式。下述已识别第三方组件没有 C/D；项目许可已经确定；本次公开发布已获项目所有者授权。

## OCR 模型与引擎

三个模型均来自 RapidOCR 维护者发布的 PaddleOCR 转换模型，适用 Apache-2.0。逐文件 SHA-256 与维护者不可变版本一致；见 third_party/ocr-model-artifacts.json，不能只按文件名判断来源。

- `ch_PP-OCRv4_det_infer.onnx`：PP-OCRv4 检测模型，B。
- `ch_PP-OCRv4_rec_infer.onnx`：PP-OCRv4 识别模型，B。
- `ch_ppocr_mobile_v2.0_cls_infer.onnx`：旧版角度分类模型，B。

来源模型权利归 PaddleOCR/Baidu 及相应权利人；ONNX 转换和包装归 RapidOCR 贡献者。模型未修改。允许商业使用和再分发；随包保留 Apache-2.0、PaddleOCR/RapidOCR 归属及模型卡；不得暗示其为本项目提供认可。模型卡：https://huggingface.co/SWHL/RapidOCR/blob/1cfba2e90fc938db55889873735088de210cc173/README.md 。该模型卡明确涵盖旧版 v1/v2/v3/v4，区别于只列新模型的最新文档。

RapidOCR 1.4.4、ONNX Runtime 1.30.0 分别采用 Apache-2.0、MIT（含其 ThirdPartyNotices），均为 B。代码许可、模型许可分别确认；不向教师附加下载或确认步骤。

## 原生运行组件

| 组件/实际版本 | 许可 | 分类与必要条件 |
|---|---|---|
| CPython 3.12.14 / 标准扩展 | PSF/Python 及历史许可 | B；保留 CPython 完整文本及 Windows 附加条件 |
| OpenSSL 3.5.7 | Apache-2.0 | B；保留许可和归属 |
| libffi（ABI 8，PE 未提供精确补丁版本） | MIT | B；保留 libffi 许可，不把 ABI 编号当上游版本 |
| SQLite 3.53.1 | Public domain | A；保留来源说明 |
| Expat 2.8.3、zlib 1.3.2、bzip2、liblzma、mpdecimal | MIT、zlib、bzip2、0BSD/原公开许可、BSD 等 | B；对应文本置于 CPython 声明目录；嵌入扩展未暴露的补丁版本不臆造 |
| OpenCV 5.0.0（Python 包 5.0.0.93） | Apache-2.0、包装代码及所带组件各自许可 | B；保留完整第三方文本；实际为一个 cv2 二进制，不把同时安装的 headless 元数据算成第二个引擎 |
| FFmpeg n7.1 / OpenCV 插件 06dc20c | LGPL-2.1-or-later | B；提供对应源码/构建资料，允许替换及为调试修改而逆向分析；保持未启用 GPL/nonfree 的构建 |
| libvpx 1.16.0、AOM 3.14.1 | BSD 及相应 PATENTS | B；保留版权、完整许可及专利声明；没有捆绑 Cisco OpenH264 DLL，仅有 API 头/包装支持 |
| Shapely 2.2.0 / GEOS 3.14.1 | BSD-3-Clause / 所带 LGPL-2.1 条款 | B；GEOS 对应源码与 Shapely 构建资料一并提供，保持动态库可替换 |
| NumPy 2.5.3 / OpenBLAS wheel 0.3.34.106.0 / LAPACK | BSD 等；GCC runtime 为 GPL-3.0-or-later WITH GCC-exception-3.1 | B；保留 NumPy 完整文本、GCC 运行库例外，不能简写为全部 BSD；例外不要求将独立应用改为 GPL |
| Pillow 12.3.0 / 图像及字体编解码库 | MIT-CMU 及所带组件各自许可 | B；保留整个 Pillow 版权许可文件，非单一 MIT 声明 |
| pypdfium2 5.14.0 / PDFium 156.0.8076.0 | BSD/Apache 及 PDFium 各依赖许可 | B；保留完整 LICENSES/BUILD_LICENSES 及声明 |
| Pyclipper 1.4.0 / Clipper 6.4.2 | MIT / BSL-1.0 | B；保留两层许可及 Clipper 作者归属 |
| Argon2 bindings 26.1.0 / Argon2；cffi 2.1.1；pydantic-core 2.50.2；LibYAML | MIT/Apache-2.0（Argon2 可选 CC0）等 | B；保留实际库的各层声明，不以 Python 包许可替代原生库 |
| MSVC、UCRT、API-set DLL | Microsoft Distributable Code 条款，不是开源许可 | B（有条件）；只随 Windows 程序分发未修改的可再分发文件，不移除版权、不暗示微软背书、不用于非微软平台或违法程序；完整上游 Windows 附加条件保留 |
| PyInstaller 6.22.3 bootloader/运行钩子 | GPL-2.0-or-later WITH Bootloader-exception；部分钩子 Apache-2.0 | B；应用可另选许可证；本包没有修改打包器/bootloader，也不分发整套打包器 |
| Vue 3.5.44 及其 runtime-core/runtime-dom/reactivity/shared | MIT | B；前端 bundle 随附 Vue 原始许可 |

逐个 EXE/DLL/PYD/ONNX 共117个文件的相对路径、实际版本信息、SHA-256、官方来源、商业使用及再分发条件见 third_party/native-components.csv。缺少 PE 版本资源的库明确标注，不凭文件名推断补丁版。

## 其他实际冻结的 Python 包

以下代码确实位于 PYZ 或运行资源中。完整许可文本见 third_party/licenses；setuptools 所含、实际冻结的 vendored 部分保留其组合声明。依赖的子组件各自许可继续适用。

| 包 | 实际版本 | 许可 | 分类 |
|---|---|---|---|
| annotated-types | 0.8.0 | MIT | B |
| argon2-cffi | 25.1.0 | MIT | B |
| argon2-cffi-bindings | 26.1.0 | MIT | B |
| blinker | 1.9.0 | see license text | B |
| cffi | 2.1.1 | MIT-0 | B |
| click | 8.5.0 | BSD-3-Clause | B |
| colorama | 0.4.6 | see license text | B |
| Flask | 3.1.3 | BSD-3-Clause | B |
| itsdangerous | 2.2.0 | see license text | B |
| Jinja2 | 3.1.6 | see license text | B |
| MarkupSafe | 3.0.4 | BSD-3-Clause | B |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | B |
| onnxruntime | 1.30.0 | MIT License | B |
| opencv-python | 5.0.0.93 | Apache 2.0 | B |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | B |
| pillow | 12.3.0 | MIT-CMU | B |
| pyclipper | 1.4.0 | MIT | B |
| pydantic | 2.14.2 | MIT | B |
| pydantic_core | 2.50.2 | MIT | B |
| pypdf | 6.21.4 | BSD-3-Clause | B |
| pypdfium2 | 5.14.0 | BSD-3-Clause, Apache-2.0, dependency licenses | B |
| PyYAML | 6.0.3 | MIT | B |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 | B |
| setuptools | 84.2.1 | MIT | B |
| shapely | 2.2.0 | BSD-3-Clause | B |
| typing-inspection | 0.4.4 | MIT | B |
| typing_extensions | 4.16.0 | PSF-2.0 | B |
| Werkzeug | 3.1.9 | BSD-3-Clause | B |

## 对应源码与替换权

随本运行包在同一下载位置发布 `third-party-corresponding-source.zip`，不得仅以库首页链接代替提供对应源码。该源码包含 FFmpeg n7.1、准确 OpenCV 包装层/构建脚本及其相应 codec 源码、GEOS 3.14.1、Shapely 2.2.0；来源和哈希见包内 SOURCE_MANIFEST.json。DLL 未改动；原生库目录及构建/替换说明见该包 README。

不禁止为调试修改的 LGPL 库而逆向分析；不以主项目 MIT/Apache-2.0 覆盖第三方的 LGPL 或微软条款。Microsoft 部分条件仅约束微软组件，不扩大为本项目全部源码的限制。

许可证允许代码使用/分发不等于对所有国家的全部编解码专利、模型训练素材或商标提供保证；本项目不承诺上游未授予的权利，不将这一一般保留扩大为无依据的功能阻断。

## 官方依据

- OCR 旧模型：https://huggingface.co/SWHL/RapidOCR/tree/1cfba2e90fc938db55889873735088de210cc173
- PaddleOCR：https://github.com/PaddlePaddle/PaddleOCR/blob/release/2.7/LICENSE
- RapidOCR：https://github.com/RapidAI/RapidOCR/blob/v1.4.4/LICENSE
- FFmpeg：https://ffmpeg.org/legal.html
- 实际 FFmpeg 发布/构建：https://github.com/opencv/opencv_3rdparty/tree/06dc20cad65dc7fcf784f70c95d46750520889a7
- GEOS：https://github.com/libgeos/geos/blob/3.14.1/COPYING
- 微软再分发清单：https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution
- PyInstaller：https://www.pyinstaller.org/en/stable/license.html
