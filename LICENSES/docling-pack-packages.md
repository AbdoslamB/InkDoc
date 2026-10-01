# Docling engine pack: contents and licenses

InkDoc builds the Docling engine pack itself (`scripts/build_pack.py`) and publishes it on its own GitHub release [`docling-pack-v1`](https://github.com/AbdoslamB/InkDoc/releases/tag/docling-pack-v1). When you install Docling from Settings, InkDoc downloads the pack from there, not from IBM or the Docling project. Everything listed below stays under its own license.

This list was generated on 2026-10-01 from the per-file manifest in [`app/core/manifest.json`](../app/core/manifest.json) (pack version 1). Licenses come from each package's PyPI metadata for the exact version shipped.

## Where the license texts are

- **Python packages:** each package inside the pack carries its own license files in its `.dist-info` folder, except the five marked *supplement*. Their texts are in [`docling-pack-supplement.txt`](docling-pack-supplement.txt), [`Docling-MIT.txt`](Docling-MIT.txt) and [`RapidOCR-Apache-2.0.txt`](RapidOCR-Apache-2.0.txt).
- **Python runtime:** CPython 3.11.16 from python-build-standalone. The pack includes CPython's `LICENSE.txt`; a copy is in [`CPython-PSF-2.0.txt`](CPython-PSF-2.0.txt).
- **Model weights:** the pack has no license files for the models. Their texts are in this folder, listed in the next section.

## Model weights in the pack

| Model | Upstream | License | Text |
| :--- | :--- | :--- | :--- |
| Layout model "heron" (PyTorch and ONNX) | [docling-project/docling-layout-heron](https://huggingface.co/docling-project/docling-layout-heron), [-onnx](https://huggingface.co/docling-project/docling-layout-heron-onnx) | Apache-2.0 | [`Docling-layout-heron-Apache-2.0.txt`](Docling-layout-heron-Apache-2.0.txt) |
| TableFormer (fast and accurate) | [docling-project/docling-models](https://huggingface.co/docling-project/docling-models) @ `fc0f2d4` | CDLA-Permissive-2.0 | [`Docling-models-CDLA-Permissive-2.0.txt`](Docling-models-CDLA-Permissive-2.0.txt) |
| RapidOCR PP-OCR models (detection, recognition, classification) | [RapidAI/RapidOCR on ModelScope](https://www.modelscope.cn/models/RapidAI/RapidOCR), from [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | Apache-2.0 | [`RapidOCR-Apache-2.0.txt`](RapidOCR-Apache-2.0.txt), [`PaddleOCR-Apache-2.0.txt`](PaddleOCR-Apache-2.0.txt) |

The optional **Code & Formula Recognition** add-on (release [`docling-addon-v1`](https://github.com/AbdoslamB/InkDoc/releases/tag/docling-addon-v1)) contains [docling-project/CodeFormulaV2](https://huggingface.co/docling-project/CodeFormulaV2) @ `ecedbe1`, licensed CDLA-Permissive-2.0 ([`Docling-models-CDLA-Permissive-2.0.txt`](Docling-models-CDLA-Permissive-2.0.txt)).

## Native libraries that need human review

Some wheels in the pack bundle native libraries under copyleft or non-OSI terms. They are listed here for review; nothing has been removed or replaced.

| Platform | Library | Comes with | License and where it is stated |
| :--- | :--- | :--- | :--- |
| Linux, macOS, Windows | FFmpeg (`libavcodec`, `libavformat`, `libavutil`, `libswscale`, `libswresample`; on Windows `opencv_videoio_ffmpeg500`) | opencv-python 5.0.0.93 | LGPL-2.1, per opencv-python's `LICENSE-3RD-PARTY.txt` (shipped in the pack) |
| macOS | libbluray, libgnutls, libnettle, libmp3lame | opencv-python 5.0.0.93 | LGPL-2.1, same file and section as FFmpeg |
| Linux | Qt 5 (`libQt5Core`, `libQt5Gui`, `libQt5Widgets`, `libQt5Test`, `libQt5XcbQpa`) | opencv-python 5.0.0.93 | LGPL-3.0, per opencv-python's `LICENSE-3RD-PARTY.txt` |
| macOS | libgmp, libidn2, libunistring | opencv-python 5.0.0.93 | LGPL-3.0, same file and section as Qt 5 |
| Linux, macOS | `libgfortran`, `libquadmath` | numpy / scipy | Usually GPL-3.0 with the GCC Runtime Library Exception (libquadmath: LGPL-2.1). Not checked against these exact wheels. |
| Linux | `libgomp` | torch | Usually GPL-3.0 with the GCC Runtime Library Exception. Not checked against this exact wheel. |
| Windows | `libiomp5md.dll`, `libiompstubs5md.dll` (Intel OpenMP) | torch | UNVERIFIED |
| macOS | `libomp.dylib` (LLVM OpenMP) | torch | Probably Apache-2.0 WITH LLVM-exception. Not checked against this exact wheel. |

opencv-python's notice says Qt 5 comes only with the non-headless wheels; the pack uses the non-headless `opencv-python`.

## Python packages

| Package | Version | License (PyPI metadata) | Platforms | License file in pack |
| :--- | :--- | :--- | :--- | :--- |
| [accelerate](https://github.com/huggingface/accelerate) | 1.15.0 | Apache-2.0 | Linux, Windows, macOS | yes |
| [annotated_doc](https://github.com/fastapi/annotated-doc) | 0.0.5 | MIT | Linux, Windows, macOS | yes |
| [annotated_types](https://github.com/annotated-types/annotated-types) | 0.8.0 | MIT | Linux, Windows, macOS | yes |
| [antlr4_python3_runtime](http://www.antlr.org) | 4.9.3 | BSD | Linux, Windows, macOS | supplement |
| [anyio](https://anyio.readthedocs.io/en/stable/versionhistory.html) | 4.15.1 | MIT | Linux, Windows, macOS | yes |
| [attrs](https://www.attrs.org/en/stable/changelog.html) | 26.1.0 | MIT | Linux, Windows, macOS | yes |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/bs4/) | 4.15.0 | MIT License | Linux, Windows, macOS | yes |
| [certifi](https://github.com/certifi/python-certifi) | 2026.7.22 | MPL-2.0 | Linux, Windows, macOS | yes |
| [charset_normalizer](https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md) | 3.5.1 | MIT | Linux, Windows, macOS | yes |
| [click](https://github.com/pallets/click/) | 8.5.0 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [colorama](https://github.com/tartley/colorama) | 0.4.6 | BSD License | Windows | yes |
| [colorlog](https://github.com/borntyping/python-colorlog) | 6.12.0 | MIT License | Linux, Windows, macOS | yes |
| [defusedxml](https://github.com/tiran/defusedxml) | 0.7.1 | PSFL | Linux, Windows, macOS | yes |
| [dill](https://github.com/uqfoundation/dill) | 0.4.1 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [doclang](https://www.doclang.ai/) | 0.7.3 | Apache-2.0 | Linux, Windows, macOS | yes |
| [docling](https://github.com/docling-project/docling/blob/main/CHANGELOG.md) | 2.130.0 | MIT | Linux, Windows, macOS | supplement |
| [docling_core](https://github.com/docling-project/docling-core/blob/main/CHANGELOG.md) | 2.98.0 | MIT | Linux, Windows, macOS | yes |
| [docling_ibm_models](https://github.com/docling-project/docling-ibm-models/blob/main/CHANGELOG.md) | 4.0.3 | MIT | Linux, Windows, macOS | yes |
| [docling_parse](https://github.com/docling-project/docling-parse) | 7.21.0 | MIT | Linux, Windows, macOS | yes |
| [docling_slim](https://github.com/docling-project/docling/blob/main/CHANGELOG.md) | 2.130.0 | MIT | Linux, Windows, macOS | yes |
| [et_xmlfile](https://foss.heptapod.net/openpyxl/et_xmlfile) | 2.0.0 | MIT | Linux, Windows, macOS | yes |
| [faker](https://github.com/joke2k/faker) | 40.39.0 | MIT License | Linux, Windows, macOS | yes |
| [filelock](https://github.com/tox-dev/py-filelock) | 4.0.4 | MIT | Linux, Windows, macOS | yes |
| [filetype](https://github.com/h2non/filetype.py) | 1.2.0 | MIT | Linux, Windows, macOS | yes |
| [fsspec](https://github.com/fsspec/filesystem_spec) | 2026.9.0 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [h11](https://github.com/python-hyper/h11) | 0.16.0 | MIT | Linux, Windows, macOS | yes |
| [hf_xet](https://github.com/huggingface/xet-core) | 1.6.0 | Apache-2.0 | Linux, Windows, macOS | yes |
| [httpcore](https://www.encode.io/httpcore/) | 1.0.9 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [httpx](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [huggingface_hub](https://github.com/huggingface/huggingface_hub) | 1.32.0 | Apache-2.0 | Linux, Windows, macOS | yes |
| [idna](https://github.com/kjd/idna) | 3.20 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [jinja2](https://github.com/pallets/jinja/) | 3.1.6 | BSD License | Linux, Windows, macOS | yes |
| [jsonref](https://jsonref.readthedocs.io/en/latest/) | 1.1.0 | MIT | Linux, Windows, macOS | yes |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | 4.26.0 | MIT | Linux, Windows, macOS | yes |
| [jsonschema_specifications](https://github.com/python-jsonschema/jsonschema-specifications) | 2025.9.1 | MIT | Linux, Windows, macOS | yes |
| [langcodes](https://github.com/georgkrause/langcodes) | 3.5.1 | MIT License | Linux, Windows, macOS | yes |
| [latex2mathml](https://www.buymeacoffee.com/roniemartinez) | 3.81.1 | MIT | Linux, Windows, macOS | supplement |
| [lxml](https://lxml.de/) | 6.1.3 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [mail_parser](https://github.com/SpamScope/mail-parser) | 4.6.5 | Apache-2.0 | Linux, Windows, macOS | yes |
| [markdown_it_py](https://github.com/executablebooks/markdown-it-py) | 4.2.0 | MIT License | Linux, Windows, macOS | yes |
| [marko](https://marko-py.readthedocs.io) | 2.2.4 | MIT | Linux, Windows, macOS | yes |
| [markupsafe](https://github.com/pallets/markupsafe/) | 3.0.3 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [mdurl](https://github.com/executablebooks/mdurl) | 0.1.2 | MIT License | Linux, Windows, macOS | yes |
| [mpire](https://github.com/sybrenjansen/mpire) | 2.10.2 | MIT | Linux, Windows, macOS | yes |
| [mpmath](http://mpmath.org/) | 1.3.0 | BSD | Linux, Windows, macOS | yes |
| [multiprocess](https://github.com/uqfoundation/multiprocess) | 0.70.19 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [networkx](https://networkx.org/) | 3.6.1 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [numpy](https://numpy.org/doc/) | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Linux, Windows, macOS | yes |
| [olefile](https://www.decalage.info/python/olefileio) | 0.47 | BSD | Linux, Windows, macOS | yes |
| [omegaconf](https://github.com/omry/omegaconf) | 2.3.1 | BSD License | Linux, Windows, macOS | yes |
| [opencv_python](https://github.com/opencv/opencv-python) | 5.0.0.93 | Apache 2.0 | Linux, Windows, macOS | yes |
| [openpyxl](https://openpyxl.readthedocs.io) | 3.1.5 | MIT | Linux, Windows, macOS | yes |
| [packaging](https://github.com/pypa/packaging) | 26.3 | Apache-2.0 OR BSD-2-Clause | Linux, Windows, macOS | yes |
| [pandas](https://pandas.pydata.org/docs/) | 3.0.6 | BSD License | Linux, Windows, macOS | yes |
| [pillow](https://python-pillow.github.io) | 12.3.0 | MIT-CMU | Linux, Windows, macOS | yes |
| [pip](https://pip.pypa.io/) | 26.2.1 | MIT | Linux, Windows, macOS | yes |
| pluggy | 1.6.0 | MIT | Linux, Windows, macOS | yes |
| [polyfactory](https://blog.litestar.dev) | 3.3.0 | MIT | Linux, Windows, macOS | yes |
| [psutil](https://github.com/giampaolo/psutil) | 7.2.2 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [pyclipper](https://github.com/fonttools/pyclipper) | 1.4.0 | MIT | Linux, Windows, macOS | yes |
| [pydantic](https://github.com/pydantic/pydantic) | 2.13.5 | MIT | Linux, Windows, macOS | yes |
| [pydantic_core](https://github.com/pydantic) | 2.46.5 | MIT | Linux, Windows, macOS | yes |
| [pydantic_settings](https://github.com/pydantic/pydantic-settings) | 2.15.0 | MIT | Linux, Windows, macOS | yes |
| [pygments](https://pygments.org) | 2.21.0 | BSD-2-Clause | Linux, Windows, macOS | yes |
| [pylatexenc](https://github.com/phfaist/pylatexenc) | 2.11 | MIT | Linux, Windows, macOS | yes |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | 5.13.0 | BSD-3-Clause, Apache-2.0, dependency licenses | Linux, Windows, macOS | yes |
| [python_dateutil](https://github.com/dateutil/dateutil) | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause (dual) | Linux, Windows, macOS | yes |
| [python_docx](https://github.com/python-openxml/python-docx) | 1.2.0 | MIT | Linux, Windows, macOS | yes |
| [python_dotenv](https://github.com/theskumar/python-dotenv) | 1.2.3 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [python_oxmsg](https://github.com/scanny/python-oxmsg) | 0.0.2 | MIT | Linux, Windows, macOS | yes |
| [python_pptx](https://github.com/scanny/python-pptx) | 1.0.2 | MIT | Linux, Windows, macOS | yes |
| [pywin32](https://github.com/mhammond/pywin32) | 312 | PSF | Windows | yes |
| [pyyaml](https://pyyaml.org/) | 6.0.3 | MIT | Linux, Windows, macOS | yes |
| [rapidocr](https://github.com/RapidAI/RapidOCR/releases) | 3.9.2 | Apache-2.0 | Linux, Windows, macOS | supplement |
| [referencing](https://github.com/python-jsonschema/referencing) | 0.37.0 | MIT | Linux, Windows, macOS | yes |
| [regex](https://github.com/mrabarnett/mrab-regex) | 2026.9.10 | Apache-2.0 AND CNRI-Python | Linux, Windows, macOS | yes |
| [requests](https://github.com/psf/requests) | 2.34.2 | Apache-2.0 | Linux, Windows, macOS | yes |
| [rich](https://github.com/Textualize/rich) | 15.0.0 | MIT | Linux, Windows, macOS | yes |
| [rpds_py](https://github.com/crate-py/rpds) | 2026.6.3 | MIT | Linux, Windows, macOS | yes |
| [rtree](https://rtree.readthedocs.io) | 1.4.1 | MIT | Linux, Windows, macOS | yes |
| [safetensors](https://github.com/huggingface/safetensors) | 0.8.0 | Apache Software License | Linux, Windows, macOS | yes |
| [scipy](https://docs.scipy.org/doc/scipy/) | 1.17.1 | BSD License | Linux, Windows, macOS | yes |
| [semchunk](https://github.com/isaacus-dev/semchunk) | 3.2.5 | MIT | Linux, Windows, macOS | yes |
| [setuptools](https://github.com/pypa/setuptools) | 84.0.0 | MIT | Linux, Windows, macOS | yes |
| [shapely](https://shapely.readthedocs.io/) | 2.1.2 | BSD 3-Clause | Linux, Windows, macOS | yes |
| [shellingham](https://github.com/sarugaku/shellingham) | 1.5.4 | ISC License | Linux, Windows, macOS | yes |
| [six](https://github.com/benjaminp/six) | 1.17.0 | MIT | Linux, Windows, macOS | yes |
| [soupsieve](https://github.com/facelessuser/soupsieve) | 2.10 | MIT | Linux, Windows, macOS | yes |
| [sympy](https://sympy.org) | 1.14.0 | BSD | Linux, Windows, macOS | yes |
| [tabulate](https://github.com/astanin/python-tabulate) | 0.10.0 | MIT | Linux, Windows, macOS | yes |
| [tokenizers](https://github.com/huggingface/tokenizers) | 0.23.2 | Apache Software License | Linux, Windows, macOS | supplement |
| [torch](https://pytorch.org) | 2.14.0 / 2.14.0+cpu | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT | Linux, Windows, macOS | yes |
| [torchvision](https://github.com/pytorch/vision) | 0.29.0 / 0.29.0+cpu | BSD | Linux, Windows, macOS | yes |
| [tqdm](https://tqdm.github.io/releases) | 4.70.1 | MPL-2.0 AND MIT | Linux, Windows, macOS | yes |
| [transformers](https://github.com/huggingface/transformers) | 5.17.0 | Apache 2.0 License | Linux, Windows, macOS | yes |
| [tree_sitter](https://tree-sitter.github.io/tree-sitter/) | 0.26.0 | MIT License | Linux, Windows, macOS | yes |
| [tree_sitter_c](https://github.com/tree-sitter/tree-sitter-c) | 0.24.2 | MIT | Linux, Windows, macOS | yes |
| [tree_sitter_javascript](https://github.com/tree-sitter/tree-sitter-javascript) | 0.25.0 | MIT | Linux, Windows, macOS | yes |
| [tree_sitter_python](https://github.com/tree-sitter/tree-sitter-python) | 0.25.0 | MIT | Linux, Windows, macOS | yes |
| [tree_sitter_typescript](https://github.com/tree-sitter/tree-sitter-typescript) | 0.23.2 | MIT | Linux, Windows, macOS | yes |
| [typer](https://github.com/fastapi/typer) | 0.26.8 | MIT | Linux, Windows, macOS | yes |
| [typing_extensions](https://github.com/python/typing_extensions/issues) | 4.16.0 | PSF-2.0 | Linux, Windows, macOS | yes |
| [typing_inspection](https://github.com/pydantic/typing-inspection) | 0.4.4 | MIT | Linux, Windows, macOS | yes |
| [tzdata](https://github.com/python/tzdata) | 2026.4 | Apache-2.0 | Windows | yes |
| [urllib3](https://github.com/urllib3/urllib3/blob/main/CHANGES.rst) | 2.8.0 | MIT | Linux, Windows, macOS | yes |
| [websockets](https://github.com/python-websockets/websockets) | 16.1.1 | BSD-3-Clause | Linux, Windows, macOS | yes |
| [xlsxwriter](https://github.com/jmcnamara/XlsxWriter) | 3.2.9 | BSD-2-Clause | Linux, Windows, macOS | yes |
