# Third-party components

This project is licensed under Apache-2.0 (see `LICENSE`). The components
below are used at runtime or in optional extras. All are permissively
licensed except `libpff-python`, which is LGPL-3.0-or-later and stays a
separate, replaceable module. Versions follow `requirements/`.

## Core retrieval (`requirements-core.txt`)

| Component | Licence |
| --- | --- |
| jieba | MIT |

## Attachment and OCR pipeline (`requirements-attachments.txt`)

| Component | Licence |
| --- | --- |
| pypdf | BSD-3-Clause |
| pypdfium2 (bundles PDFium) | Apache-2.0 and BSD-3-Clause |
| openpyxl | MIT |
| xlrd | BSD-3-Clause |
| python-docx | MIT |
| python-pptx | MIT |
| olefile | BSD-3-Clause |
| striprtf | BSD-3-Clause |
| rapidocr-onnxruntime | Apache-2.0 |
| onnxruntime | MIT |
| opencv-python | Apache-2.0 |
| Pillow | MIT-CMU |
| shapely | BSD-3-Clause |
| pyclipper | MIT |
| numpy | BSD-3-Clause (plus 0BSD, MIT, Zlib and CC0-1.0 parts) |
| libarchive-c | CC0-1.0 |
| libpff-python | LGPL-3.0-or-later |

`libpff-python` (PST/Outlook archive parsing) is optional and used only for
local mail ingestion. It is distributed as an independent package; if you
ship a bundled application, keep it as a separately replaceable module and
include its licence text and source offer as required by LGPL-3.0.

## Optional Agent orchestration (`requirements-agent.txt`)

The default bounded Agent loop uses only the Python standard library. If the
optional `PydanticAI` backend is installed:

| Component | Licence |
| --- | --- |
| pydantic-ai-slim | MIT |
| pydantic | MIT |
| openai | Apache-2.0 |
| httpx | BSD-3-Clause |

## Optional local training (`requirements-training.txt`)

| Component | Licence |
| --- | --- |
| spacy | MIT |
| spacy-pkuseg | MIT |
| transformers | Apache-2.0 |
| peft | Apache-2.0 |
| accelerate | Apache-2.0 |
| safetensors | Apache-2.0 |
| huggingface-hub | Apache-2.0 |
| numpy | BSD-3-Clause |
| Pillow | MIT-CMU |
| beautifulsoup4 | MIT |

## Note on PDF handling

Earlier revisions of this project read PDF files with PyMuPDF, which is
dual-licensed AGPL-3.0/commercial and therefore not compatible with an
Apache-2.0 distribution. PDF text, geometry and embedded images now come
from pypdf, and page rendering from pypdfium2; no AGPL component is
required at runtime.
