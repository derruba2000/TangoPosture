# Third-party notices

Inventory of the installed, locked Python 3.11 macOS arm64 environment. Original application code is Apache-2.0. Exact transitive versions and distribution hashes are in `uv.lock`. Licence texts shipped in the installed wheels govern bundled native components as well as the Python wrappers. Source installations do not bundle weights; desktop installers include the verified model described below. No training data are bundled.

| Distribution | Version | SPDX / licence | Upstream licence |
| --- | --- | --- | --- |
| absl-py | 2.5.1 | Apache-2.0 | [licence](https://github.com/abseil/abseil-py/blob/main/LICENSE) |
| annotated-types | 0.8.0 | MIT | [licence](https://github.com/annotated-types/annotated-types/blob/main/LICENSE) |
| ast-serialize | 0.13.1 | MIT | [licence](https://github.com/mypyc/ast_serialize/blob/master/LICENSE) |
| colorama | 0.4.6 (Windows only, locked; not installed on tested Mac) | BSD-3-Clause | [licence](https://github.com/tartley/colorama/blob/master/LICENSE.txt) |
| cffi | 2.1.1 | MIT-0 | [licence](https://github.com/python-cffi/cffi/blob/main/LICENSE) |
| flatbuffers | 25.12.19 | Apache-2.0 | [licence](https://github.com/google/flatbuffers/blob/master/LICENSE) |
| iniconfig | 2.3.1 | MIT | [licence](https://github.com/pytest-dev/iniconfig/blob/main/LICENSE) |
| librt | 0.16.0 | MIT | [licence](https://github.com/mypyc/librt/blob/master/LICENSE) |
| mediapipe | 0.10.31 | Apache-2.0; bundled notices | [licence](https://github.com/google-ai-edge/mediapipe/blob/v0.10.31/LICENSE) |
| mypy | 2.4.1 | MIT | [licence](https://github.com/python/mypy/blob/master/LICENSE) |
| mypy-extensions | 1.1.0 | MIT | [licence](https://github.com/python/mypy_extensions/blob/master/LICENSE) |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | [licence](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| opencv-python | 5.0.0.93 | Apache-2.0; bundled library licences | [licence](https://github.com/opencv/opencv-python/blob/master/LICENSE.txt) |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | [licence](https://github.com/pypa/packaging/blob/main/LICENSE) |
| pathspec | 1.1.1 | MPL-2.0 | [licence](https://github.com/cpburnz/python-pathspec/blob/master/LICENSE) |
| pluggy | 1.7.0 | MIT | [licence](https://github.com/pytest-dev/pluggy/blob/main/LICENSE) |
| pycparser | 3.12 | BSD-3-Clause | [licence](https://github.com/eliben/pycparser/blob/main/LICENSE) |
| pydantic | 2.14.2 | MIT | [licence](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| pydantic-core | 2.50.2 | MIT | [licence](https://github.com/pydantic/pydantic/blob/main/pydantic-core/LICENSE) |
| pygments | 2.21.0 | BSD-2-Clause | [licence](https://github.com/pygments/pygments/blob/master/LICENSE) |
| pytest | 9.2.0 | MIT | [licence](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |
| ruff | 0.17.3 | MIT | [licence](https://github.com/astral-sh/ruff/blob/main/LICENSE) |
| sounddevice | 0.5.6 | MIT; PortAudio MIT | [licence](https://github.com/spatialaudio/python-sounddevice/blob/master/LICENSE) |
| typing-inspection | 0.4.4 | MIT | [licence](https://github.com/pydantic/typing-inspection/blob/main/LICENSE) |
| typing-extensions | 4.16.0 | PSF-2.0 | [licence](https://github.com/python/typing_extensions/blob/main/LICENSE) |

## Tutorial renderer

The offline tutorial uses Python-Markdown 3.10.2 (BSD-3-Clause). Preserve its
installed distribution licence when redistributing the application.

## Desktop icon generation

Pillow 12.0.0 (HPND) is a packaging-only dependency used to convert the project
logo into `.ico` and `.icns` files. Its [licence](https://github.com/python-pillow/Pillow/blob/12.0.0/LICENSE)
governs Pillow; the generated icons use the project's logo.

## Model weights

Google BlazePose GHUM Lite, Full and Heavy, Pose Landmarker float16 bundle
version 1: Apache-2.0 according to the [official linked model card, page 2](https://storage.googleapis.com/mediapipe-assets/Model%20Card%20BlazePose%20GHUM%203D.pdf).
[models/manifest.json](models/manifest.json) records the exact sources, SHA-256
identities and provenance limitations. Desktop installers bundle Lite;
Full/Heavy can be downloaded for offline analysis. This is separate evidence
from the MediaPipe code licence. The training dataset is not provided or
licensed by this project.

## Bundled native libraries and tools

OpenCV wheels include FFmpeg (LGPL-2.1-or-later) and other third-party components; see the preserved `opencv-python` LICENSE-3RD-PARTY.txt for individual copyright notices and source references. Preserve these notices and satisfy native-library redistribution obligations if shipping binaries. NumPy includes numerical-library notices in its wheel licence. MediaPipe native runtime includes TensorFlow Lite and other upstream components under its supplied notices. Sounddevice provides optional microphone capture.

Build tools include Hatchling and PyInstaller; desktop installers contain PyInstaller's bootloader and the Python runtime. Environment manager: uv, via https://github.com/astral-sh/uv. Python 3.11: PSF-2.0, https://docs.python.org/3.11/license.html. Desktop builds collect installed wheel licence and notice files in the bundled `notices` directory. Wheel metadata is authoritative for the selected distributions; additional target platforms require their own native notice review.
