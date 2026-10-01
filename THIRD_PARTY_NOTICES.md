# Third-Party Notices

InkDoc's own source code is licensed under the MIT License (see [LICENSE](LICENSE)). That license applies only to InkDoc's code. Each third-party component listed below remains under its own license and terms, which are not changed by InkDoc's license. If you use, modify, or redistribute any of these components, you are responsible for reviewing and complying with their licenses. You may use a component or model commercially only if, and to the extent that, its own license allows it, including any conditions it attaches.

Code and model weights are listed separately because they often have different licenses. Full license texts are in [LICENSES/](LICENSES/). See also [DISCLAIMER.md](DISCLAIMER.md).

This file was last checked against upstream sources on 2026-10-01. Where an upstream source does not state something (usually a copyright holder for model weights), it is marked **UNVERIFIED** instead of guessed.

### How InkDoc distributes each component

| Term | Meaning |
| :--- | :--- |
| **Bundled** | Included in the InkDoc installers and archives you download from InkDoc's releases. |
| **Re-hosted** | Built or copied by InkDoc and downloaded from InkDoc's own GitHub releases when you install it from Settings. |
| **Mirrored** | Downloaded from the upstream source first. InkDoc's GitHub release keeps an identical copy (same SHA-256) that is used only if the upstream download fails. |
| **Fetched by your machine** | Not distributed by InkDoc. Your computer downloads it from the upstream source when a feature needs it. |

---

## 1. Bundled in the InkDoc desktop app

| Component | Used for | Upstream | Copyright holder | License | Distribution | Text |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| MarkItDown (code) | Core file-to-Markdown conversion | [microsoft/markitdown](https://github.com/microsoft/markitdown) | Microsoft Corporation | MIT | Bundled | [MarkItDown-MIT.txt](LICENSES/MarkItDown-MIT.txt) |
| CPython 3.11 | Python runtime inside the executable | [python.org](https://www.python.org/) | Python Software Foundation and others (see text) | PSF-2.0, plus licenses of bundled libraries | Bundled | [CPython-PSF-2.0.txt](LICENSES/CPython-PSF-2.0.txt) |
| FastAPI | Local REST API | [fastapi/fastapi](https://github.com/fastapi/fastapi) | Sebastián Ramírez | MIT | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| Uvicorn, Starlette | Local web server | [encode/uvicorn](https://github.com/encode/uvicorn), [Kludex/starlette](https://github.com/Kludex/starlette) | Encode OSS Ltd. | BSD-3-Clause | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| pywebview | Desktop window | [r0x0r/pywebview](https://github.com/r0x0r/pywebview) | Roman Sirokov | BSD-3-Clause | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| pypdfium2 and PDFium | PDF probing and the missing-text check | [pypdfium2-team/pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | pypdfium2: not named in its license text; PDFium: The PDFium Authors; bundled libraries: see text | BSD-3-Clause, Apache-2.0, plus the licenses of PDFium's bundled libraries | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| Magika (code and its built-in model) | File-type detection inside MarkItDown | [google/magika](https://github.com/google/magika) | Not named in its LICENSE | Apache-2.0 | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| SpeechRecognition | Audio transcription inside MarkItDown | [Uberi/speech_recognition](https://github.com/Uberi/speech_recognition) | Anthony Zhang | BSD-3-Clause | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| FLAC encoder binaries (inside SpeechRecognition) | Audio encoding for transcription | [xiph/flac](https://github.com/xiph/flac) | UNVERIFIED (not stated in the copy shipped by SpeechRecognition) | GPL-2.0 | Bundled | [FLAC-GPL-2.0.txt](LICENSES/FLAC-GPL-2.0.txt) |
| CMU Sphinx en-US language data (inside SpeechRecognition) | Shipped with SpeechRecognition; InkDoc does not use offline recognition | [cmusphinx](https://github.com/cmusphinx) | Carnegie Mellon University | BSD-style (see text) | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| PyInstaller bootloader | Starts the packaged executable | [pyinstaller/pyinstaller](https://github.com/pyinstaller/pyinstaller) | PyInstaller Development Team | GPL-2.0-or-later with the Bootloader Exception | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| All other Python packages (78 in total, including numpy, pandas, lxml, Pillow, cryptography, certifi) | Dependencies of the above | See text | See text | MIT, BSD, Apache-2.0, PSF-2.0, MPL-2.0 (certifi) and others, listed per package | Bundled | [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) |
| marked 15.0.12 | Markdown preview | [markedjs/marked](https://github.com/markedjs/marked) | MarkedJS; Christopher Jeffrey; John Gruber (see text) | MIT (and a BSD-style notice for Markdown) | Bundled | [marked-MIT.txt](LICENSES/marked-MIT.txt) |
| DOMPurify 3.1.6 | Sanitising the preview | [cure53/DOMPurify](https://github.com/cure53/DOMPurify) | Dr.-Ing. Mario Heiderich, Cure53 | Apache-2.0 or MPL-2.0, at your choice | Bundled | [DOMPurify-Apache-2.0-OR-MPL-2.0.txt](LICENSES/DOMPurify-Apache-2.0-OR-MPL-2.0.txt) |
| highlight.js 11.12.0 (custom build) | Code highlighting in the preview | [highlightjs/highlight.js](https://github.com/highlightjs/highlight.js) | Ivan Sagalaev | BSD-3-Clause | Bundled | [highlight.js-BSD-3-Clause.txt](LICENSES/highlight.js-BSD-3-Clause.txt) |

The package list in [python-packages-desktop.txt](LICENSES/python-packages-desktop.txt) was generated from a clean Windows build environment. macOS builds also bundle pyobjc packages, and Linux builds bundle PyGObject and GTK/WebKitGTK libraries from the build machine. Those are not yet listed (see [CONTRIBUTING.md](CONTRIBUTING.md#licenses-for-new-dependencies-models-and-binaries)).

## 2. Docling engine pack (optional, re-hosted by InkDoc)

InkDoc builds this pack from the upstream projects and publishes it on its GitHub release [`docling-pack-v1`](https://github.com/AbdoslamB/InkDoc/releases/tag/docling-pack-v1). It is downloaded only when you install Docling from Settings. The full list of its 107 Python packages and their licenses is in [LICENSES/docling-pack-packages.md](LICENSES/docling-pack-packages.md).

| Component | Used for | Upstream | Copyright holder | License | Distribution | Text |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Docling (code) | Layout analysis, table and OCR pipeline | [docling-project/docling](https://github.com/docling-project/docling) | The Docling Contributors (project started by IBM Research Zurich) | MIT | Re-hosted | [Docling-MIT.txt](LICENSES/Docling-MIT.txt) |
| docling-ibm-models (code) | Model runtime code for Docling | [docling-project/docling-ibm-models](https://github.com/docling-project/docling-ibm-models) | International Business Machines | MIT | Re-hosted | License file inside the pack |
| Docling layout model "heron" (weights, PyTorch and ONNX) | Page layout detection | [docling-project/docling-layout-heron](https://huggingface.co/docling-project/docling-layout-heron) | UNVERIFIED (not stated on the model card) | Apache-2.0 | Re-hosted | [Docling-layout-heron-Apache-2.0.txt](LICENSES/Docling-layout-heron-Apache-2.0.txt) |
| TableFormer (weights) | Table structure recovery | [docling-project/docling-models](https://huggingface.co/docling-project/docling-models) @ `fc0f2d4` | UNVERIFIED (not stated on the model card) | CDLA-Permissive-2.0 | Re-hosted | [Docling-models-CDLA-Permissive-2.0.txt](LICENSES/Docling-models-CDLA-Permissive-2.0.txt) |
| RapidOCR (code) | OCR for scanned pages | [RapidAI/RapidOCR](https://github.com/RapidAI/RapidOCR) | RapidOCR Authors / RapidAI | Apache-2.0 | Re-hosted | [RapidOCR-Apache-2.0.txt](LICENSES/RapidOCR-Apache-2.0.txt) |
| PP-OCR models, ONNX conversions by RapidOCR (weights) | OCR text detection and recognition | [RapidAI/RapidOCR on ModelScope](https://www.modelscope.cn/models/RapidAI/RapidOCR), originally [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | PaddlePaddle Authors; RapidAI | Apache-2.0 | Re-hosted | [RapidOCR-Apache-2.0.txt](LICENSES/RapidOCR-Apache-2.0.txt), [PaddleOCR-Apache-2.0.txt](LICENSES/PaddleOCR-Apache-2.0.txt) |
| PyTorch, torchvision, Transformers, OpenCV and the other pack packages | Running the models | See the pack list | See the pack list | Listed per package; some bundle LGPL or GCC-runtime libraries | Re-hosted | [docling-pack-packages.md](LICENSES/docling-pack-packages.md) |
| CPython 3.11.16 (python-build-standalone build) | Python runtime for the pack | [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone) | Python Software Foundation and others | PSF-2.0, plus licenses of bundled libraries | Re-hosted | [CPython-PSF-2.0.txt](LICENSES/CPython-PSF-2.0.txt) |

The Docling pack does not include EasyOCR. If you run InkDoc from source with your own `pip install docling`, the OCR engines and models Docling uses depend on your installation, and Docling may download models from Hugging Face itself.

## 3. Code & Formula Recognition add-on (optional, re-hosted by InkDoc)

| Component | Used for | Upstream | Copyright holder | License | Distribution | Text |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| CodeFormulaV2 (weights) | Reading code blocks and formulas in Docling | [docling-project/CodeFormulaV2](https://huggingface.co/docling-project/CodeFormulaV2) @ `ecedbe1` | UNVERIFIED (not stated on the model card) | CDLA-Permissive-2.0 | Re-hosted on [`docling-addon-v1`](https://github.com/AbdoslamB/InkDoc/releases/tag/docling-addon-v1) | [Docling-models-CDLA-Permissive-2.0.txt](LICENSES/Docling-models-CDLA-Permissive-2.0.txt) |

## 4. GLM-OCR (optional download)

| Component | Used for | Upstream | Copyright holder | License | Distribution | Text |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| GLM-OCR (weights) | OCR for scans and photos | [zai-org/GLM-OCR](https://huggingface.co/zai-org/GLM-OCR) | UNVERIFIED (the model card states the license but no copyright holder) | MIT, per the model card | Mirrored | [GLM-OCR-MIT.txt](LICENSES/GLM-OCR-MIT.txt) |
| GLM-OCR GGUF conversion | The model files InkDoc actually downloads | [ggml-org/GLM-OCR-GGUF](https://huggingface.co/ggml-org/GLM-OCR-GGUF) @ `65a42de` | UNVERIFIED | Not stated on its own card; base model is MIT | Mirrored | [GLM-OCR-MIT.txt](LICENSES/GLM-OCR-MIT.txt) |
| llama.cpp b11307 (code and binaries) | `llama-server`, which runs GLM-OCR | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | The ggml authors | MIT | Mirrored | [llama.cpp-MIT.txt](LICENSES/llama.cpp-MIT.txt) |
| Code compiled into llama.cpp: nlohmann/json, cpp-httplib, stb_image, miniaudio, subprocess.h | Parts of `llama-server` and `mtmd` | See text | Niels Lohmann; yhirose; Sean Barrett; David Reid; public-domain dedication | MIT; MIT; MIT or public domain; public domain or MIT-0; Unlicense | Mirrored (inside the binaries) | [llama.cpp-bundled-components.txt](LICENSES/llama.cpp-bundled-components.txt) |
| LLVM OpenMP runtime (`libomp.dll`, Windows only) | Multithreading in the Windows llama.cpp builds | [llvm/llvm-project](https://github.com/llvm/llvm-project) | LLVM Project contributors | Apache-2.0 WITH LLVM-exception | Mirrored (inside the Windows archives) | [LLVM-OpenMP-Apache-2.0-WITH-LLVM-exception.txt](LICENSES/LLVM-OpenMP-Apache-2.0-WITH-LLVM-exception.txt) |

The official GLM-OCR pipeline also uses PP-DocLayoutV3 (Apache-2.0) and the GLM-OCR SDK (Apache-2.0). InkDoc uses neither: it runs only the model, through llama.cpp.

## 5. Run on your machine, not distributed by InkDoc

| Component | Used for | Upstream | Copyright holder | License | Distribution | Text |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| @shiftlabs/markit 0.6.1 | The Markit engine on macOS and Linux when a `markit` command or Node.js `npx` is installed (on Windows, through WSL for some file types) | [shift-labs-ai/markit](https://github.com/shift-labs-ai/markit) | Michael Liv | MIT | Fetched by your machine (`npx` downloads it from the npm registry) | [Shift-Labs-Markit-MIT.txt](LICENSES/Shift-Labs-Markit-MIT.txt) |
| ffmpeg | Converting MP3, M4A and MP4 audio before transcription, if you install it | [ffmpeg.org](https://ffmpeg.org/) | FFmpeg developers | Depends on your build (LGPL or GPL) | Not distributed; you install it yourself | Your ffmpeg build |

When `markit`/`npx` is not available, the Markit engine uses InkDoc's own Python extractors (part of InkDoc's MIT-licensed code) or falls back to MarkItDown.

## 6. Online services

Some features contact services run by other companies, whose own terms and privacy policies apply. These are not software components and are not distributed by InkDoc. [DISCLAIMER.md](DISCLAIMER.md#network-use-and-privacy) lists exactly which features go online:

- Google's speech-recognition service (audio transcription)
- YouTube (transcripts for YouTube links)
- Google Fonts (the app's Inter and JetBrains Mono fonts)
- Hugging Face, GitHub and the npm registry (downloads and update checks)
- Any website whose URL you convert
