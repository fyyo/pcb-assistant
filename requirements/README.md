# Dependency Sets

All project dependency files live in this directory; the project root does not
keep a second copy.

- `requirements-core.txt`: minimal retrieval and evidence runtime (`jieba`).
- `requirements-test.txt`: complete synthetic regression suite, including CPU OCR.
- `requirements.txt`: mail cleaning and PST processing, including the core runtime.
- `requirements.lock.txt`: locked full local environment, including attachments and OCR.
- `requirements-attachments.txt`: attachment extraction dependencies.
- `requirements-ocr.lock.txt`: separate GPU OCR environment.
- `requirements-training.txt`: local model training dependencies.
- `requirements-training.lock.txt`: locked training environment.

Run dependency commands from the project root. The main CLI does not require the training or GPU OCR sets.

```powershell
# Existing Catalog: retrieval, evidence answers and MCP
python -m pip install -r requirements/requirements-core.txt

# Development and CI: synthetic tests, without private data or GPU dependencies
python -m pip install -r requirements/requirements-test.txt
python -m pip check
```

The test set installs the spaCy library and CPU OCR engine. It does not require
PST parsing, spaCy language models, private datasets, GPU OCR or training weights.
OCR tests use the CPU models bundled with the OCR package. Real mail cleaning
requires the full base set and its language models, as described in the data guides.

The `*.lock.txt` files record the existing local processing environments. They
are retained for reproducing those runs; CI installs `requirements-test.txt` on
Windows and Linux rather than copying a machine-specific environment snapshot.
