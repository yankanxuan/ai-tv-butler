# Standalone sherpa-onnx ASR PoC

This is an isolated command-line experiment. It does not alter production dependencies, services, configuration, or Xiaomi speech handling. It does not record a microphone, discover audio, read existing personal audio, or download artifacts. Imports of sherpa-onnx occur only after consent, provenance, hash, and WAV checks pass.

**Actual PoC inference completed** through this CLI using the hash-verified compact CTC model and 10 local Piper synthetic test phrases, with explicit user consent for those samples. All samples were mono PCM16 16000 Hz. The run used the isolated sherpa_onnx 2.0.1 runtime on Linux x86_64 with two CPU threads and returned `status: poc_inference_completed`. No existing personal recordings or microphone audio were used; no samples were uploaded. Audio, model weights, and the raw full report are not included here.

| Measurement | Observed value |
| --- | ---: |
| Samples | 10 synthetic phrases |
| Original audio duration | 14.5128125 s |
| Inference wall time | 5.037669586 s |
| RTF | 0.3471187674 |
| Inference process CPU time | 7.317242288 s |
| Peak process RSS, lifetime maximum, not a delta | 112,545,792 bytes (about 107.33 MiB) |
| Separate recognizer initialization wall time | 5.219963135 s |
| Separate recognizer initialization process CPU time | 3.906167575 s |
| Corpus CER | 10 edits / 60 normalized reference characters = 16.6667% |

The timing scopes and normalization policy are defined below. These observations establish a local synthetic speech smoke benchmark, not natural-speech or microphone accuracy. ARM inference remains untested. Unit tests separately use temporary generated WAV silence and mock model artifacts; their results are validation evidence rather than speech recognition measurements.

## Human recording reassessment (2026-11-12)

The separate comparison runner now uses JiWER 4.0.0 (`process_characters`, Apache-2.0) backed by RapidFuzz 3.14.3 (MIT), replacing the non-minimal SequenceMatcher metric. Click 8.1.8 is BSD-3-Clause. License texts were checked in the actual downloaded distributions. Install only in the isolated evaluation environment:

```sh
/tmp/osfv/bin/python -m pip install -r poc/requirements-eval.txt
/tmp/osfv/bin/python -m unittest discover -s tests -p test_asr_compare_metrics.py -v
```

All 12 existing authorized human recordings were decoded again with the pinned model and standard CER: clean 3/46, far-field 4/21, noise 14/8; total 21/75. See [the current human report](../docs/ASR_COMPARISON_B8.md) and [redacted evidence](../docs/evidence/asr_reassessment_b8.json) for exact counts and new measurements. The synthetic benchmark above is a separate historical run. File replay first-partial timings are not real microphone end-to-end latency. Production Xiaomi handling remains the default.

## Runtime and setup

The installed isolated interpreter was inspected directly: `/tmp/osfv/bin/python`, CPython 3.11, sherpa_onnx **2.0.1**, Linux x86_64. Both factory signatures are present:

- `OnlineRecognizer.from_transducer(tokens, encoder, decoder, joiner, ..., sample_rate=16000, feature_dim=80, provider='cpu')`
- `OnlineRecognizer.from_zipformer2_ctc(tokens, model, ..., sample_rate=16000, feature_dim=80, provider='cpu')`

Use the existing isolated environment from the repository root:

```sh
/tmp/osfv/bin/python -c 'import sherpa_onnx; print(sherpa_onnx.__version__)'
/tmp/osfv/bin/python -m unittest discover -s tests -p test_asr_poc_v408.py
/tmp/osfv/bin/python -m poc.sherpa_asr --help
```

For a fresh environment, keep it outside the application environment:

```sh
python3 -m venv /tmp/sherpa-asr-poc
/tmp/sherpa-asr-poc/bin/python -m pip install 'sherpa-onnx==2.0.1'
```

The runner uses stdlib `wave` and `array`. It does not require NumPy. sherpa-onnx and its wheel dependencies belong only in this isolated environment.

The [PyPI 2.0.1 release API](https://pypi.org/pypi/sherpa-onnx/2.0.1/json) publishes Linux x86_64, aarch64, and armv7l wheels. Wheel availability is architecture support metadata, not evidence of successful inference on a given board. ARM has **not** been tested. Check the selected wheel's Python ABI and manylinux/glibc requirements on the target; armv7l wheels use manylinux_2_35 while x86_64/aarch64 wheels include manylinux_2_17 tags.

## Reviewed models and provenance

Metadata was retrieved using Python `urllib.request` through `http://127.0.0.1:7890`, using the real upstream Hugging Face model API with `?blobs=true` plus pinned README contents. No speech samples were downloaded. Large weights were not downloaded by this worker.

The example chooses the compact Chinese streaming ZIPformer CTC:

- Repository: `csukuangfj/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01`
- Revision: `a5f60fe00dcfbaf68fcc1c6b5cf53061e144d6da`
- [Pinned ONNX card](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01/blob/a5f60fe00dcfbaf68fcc1c6b5cf53061e144d6da/README.md): identifies the checkpoint source but has **no independent license declaration**.
- [Pinned source checkpoint card](https://huggingface.co/csukuangfj/icefall-streaming-zipformer-small-ctc-zh-2025-04-01/blob/6768aa8b65bc68e128af2ee08a7142dd0abe4be4/README.md): explicitly declares `license: apache-2.0`.
- The manifest records Apache-2.0 based on that documented checkpoint provenance, not a fabricated license field in the ONNX card. This is upstream declaration evidence, not an independent audit of training data rights or permission to use unrelated audio.

| Role | Upstream filename | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| model | model.int8.onnx | 26342340 | `68c9c943840f7d9cf3e8a4970ba50f404feb5277f611fa82b7e72267786fa84a` |
| tokens | tokens.txt | 13366 | `6fed8c6c248516f38e7faa19404b57413e8ce259f1cbc1fa4aebc86eac32fdfd` |

Total selected artifacts: **26,355,706 bytes**, below the 100 MB cap. The ONNX hash comes from upstream LFS metadata; the small tokens file was fetched and SHA-256 computed directly. Downloading the model is a separate explicit step, never a consequence of running the CLI.

The originally requested bilingual transducer is also supported:

- Repository: `csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20`
- Revision: `98590b7ed6443e77b714204da2757d75e1a642f4`
- [Pinned card](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/blob/98590b7ed6443e77b714204da2757d75e1a642f4/README.md) explicitly declares `license: apache-2.0`; it links the [source model](https://huggingface.co/pfluo/k2fsa-zipformer-chinese-english-mixed/blob/6eb615ae77ecac05c5628d5c8ed7037c14a338d5/README.md), whose API also declares Apache-2.0.

| Role | Upstream filename | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| encoder | encoder-epoch-99-avg-1.int8.onnx | 181895032 | `8fa764187a261844f859d7143ebaa563af5d10adfece4c18a8f414c88cba2a9b` |
| decoder | decoder-epoch-99-avg-1.int8.onnx | 13091040 | `1a70c593d71e53f023f5f55b0b4cfff5055abb786ee3992e5f63dc2e273cc4fa` |
| joiner | joiner-epoch-99-avg-1.int8.onnx | 3228404 | `1ed689c5ed19dbaa725d9d191bb4822b5f4855a39e1ffd28cbc1f340d25b2ee0` |
| tokens | tokens.txt | 56317 | `a8e0e4ec53810e433789b54a5c0134a7eaa2ffca595a6334d54c00da858841d3` |

Total selected transducer artifacts: **198,270,793 bytes**. This exceeds the authorized small-download threshold; they were not downloaded. Replacing the compact example's model fields with these exact pins selects `from_transducer`. Its `license_evidence` must be the pinned bilingual card above; `provenance_evidence` is not required for this profile.

## Exact manifest contract

Both JSON documents use `schema_version: 1`. All paths can be absolute; relative paths resolve against the containing manifest's directory, not the current working directory. The CLI only accepts the two reviewed model profiles and rejects altered revision, license evidence, role sets, SHA-256 declarations, source URLs, artifact size declarations, local file hashes, or local sizes. Merely supplying one's own hash for arbitrary model weights does not pass.

The model document contains:

```json
{
  "schema_version": 1,
  "model": {
    "repo": "exact reviewed repository",
    "revision": "exact reviewed 40-character commit",
    "license": "apache-2.0",
    "license_evidence": "exact pinned license card URL",
    "provenance_evidence": "exact pinned ONNX card URL; required for compact CTC",
    "files": {
      "model": {
        "path": "local/model.int8.onnx",
        "sha256": "exact upstream 64-character lowercase SHA-256",
        "size_bytes": 26342340,
        "source_url": "exact pinned resolve URL"
      },
      "tokens": {
        "path": "local/tokens.txt",
        "sha256": "exact upstream SHA-256",
        "size_bytes": 13366,
        "source_url": "exact pinned resolve URL"
      }
    }
  }
}
```

Use the concrete [example](./model_manifest.example.json) rather than the explanatory placeholders. For the transducer, `files` must instead contain exactly `encoder`, `decoder`, `joiner`, and `tokens`, each with the same four fields. Optional `architecture_metadata` is informational and never treated as measured performance.

The separate user-supplied sample manifest contains:

```json
{
  "schema_version": 1,
  "samples": [
    {
      "id": "authorized-synthetic-001",
      "path": "samples/authorized-synthetic-001.wav",
      "origin": "Describe the actual source, generator/voice and authorization",
      "license": "LicenseRef-Explicit-Permission",
      "permission_evidence": "Identify actual explicit permission covering this sample",
      "authorized": true,
      "sha256": "REPLACE_WITH_REAL_64_CHARACTER_LOWERCASE_SHA256",
      "reference": "打开电视，播放新闻。"
    }
  ]
}
```

This is a schema example, **not authorization to generate or read audio**. The caller must supply real metadata and permission. `samples` must be nonempty and IDs unique. `origin` and `reference` must be nonempty strings. `authorized` must be the JSON boolean `true`. Accepted sample licenses are `CC0-1.0`, `CC-BY-4.0`, `CC-BY-SA-4.0`, and `LicenseRef-Explicit-Permission`; the last additionally requires nonempty `permission_evidence`. Supply relevant license attribution and evidence in `origin` for Creative Commons samples. Unknown licenses are rejected. The runner checks declarations and hashes; it cannot establish whether a person truthfully possesses the claimed rights.

Audio must already be uncompressed signed PCM16 little-endian WAV, mono, exactly 16000 Hz, with at least one complete frame. The runner performs no implicit resampling or channel mixing. References must remain nonempty after CER normalization. No upstream test WAV is assumed to have independent sample consent merely because it shares a model repository.

After obtaining an explicitly authorized sample, calculate its digest without reading any other audio:

```sh
sha256sum /absolute/path/to/authorized-synthetic-001.wav
```

## Run

The following optional setup downloads only the reviewed compact model and tokens into an independent cache, with a fixed total-size bound and per-file verification. It does not obtain or synthesize audio. Run from the repository root; it prints the path of the prepared model manifest:

```sh
/tmp/osfv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path
import urllib.request
from poc import sherpa_asr as asr

cache = Path.home() / '.cache' / 'ai-tv-b8-asr'
cache.mkdir(parents=True, exist_ok=True)
pins = asr.CTC_PINNED_FILES
assert sum(size for _, _, size in pins.values()) < 100_000_000
opener = urllib.request.build_opener(urllib.request.ProxyHandler({
    'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}))
manifest = json.loads(Path('poc/model_manifest.example.json').read_text())
for role, (name, expected_sha, size) in pins.items():
    entry = manifest['model']['files'][role]
    target = cache / name
    if target.exists():
        assert target.stat().st_size == size
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha
    else:
        # The bounded read refuses an unexpectedly oversized response.
        with opener.open(entry['source_url'], timeout=120) as response:
            payload = response.read(size + 1)
        assert len(payload) == size
        assert hashlib.sha256(payload).hexdigest() == expected_sha
        target.write_bytes(payload)
    entry['path'] = str(target)
model_manifest = cache / 'model.json'
model_manifest.write_text(json.dumps(manifest, indent=2) + '\n')
print(model_manifest)
PY
```

Prepare model and sample manifests outside the production configuration. Set the example's local paths to the verified downloaded artifacts. Then invoke:

```sh
/tmp/osfv/bin/python -m poc.sherpa_asr \
  --manifest /absolute/path/to/model.json \
  --samples /absolute/path/to/samples.json \
  --consent \
  --num-threads 2 > /absolute/path/to/asr-poc-report.json
```

`--consent` authorizes reading only the explicitly listed local samples and running the local benchmark. Without it, the runner rejects before even reading manifests. All sample authorization metadata is checked before any sample file is opened. No output report is fabricated on failure: stderr receives `status: rejected_or_unavailable`, the process exits 2, and stdout has no report.

## What the measurements mean

A successful report provides per-sample hypotheses, references, CER edit counts, and actual clocks. Corpus CER is total edits / total normalized reference characters, not the average of sample CERs.

- **CER:** Unicode NFKC, then remove Unicode punctuation and whitespace; preserve case. Use Unicode code points, so Chinese text is evaluated per character. `(substitutions + deletions + insertions) / reference_characters`; values above 1 are possible. Empty normalized references are rejected. This is the same character policy for mixed English text, not English word error rate.
- **Audio duration:** original WAV frames / 16000. Padding does not inflate source duration.
- **Wall inference seconds:** `time.perf_counter()` around stream creation, immediate 200 ms audio chunks, decode loops, 660 ms zero padding for right context, end-of-input flush, and text extraction. No real-time sleeps. Audio reads and CER computation are excluded.
- **RTF:** wall inference seconds / original audio duration. Aggregate RTF is summed inference wall time / summed original audio duration.
- **CPU seconds:** `time.process_time()` over the same inference interval, measuring process CPU across threads. It can exceed wall time.
- **Cold initialization:** separate factory wall and process CPU timings, excluding Python import, provenance checks, hashing, and WAV reads. It is not claimed to include OS page-cache eviction or a complete cold process launch.
- **Maximum RSS:** `resource.getrusage(RUSAGE_SELF).ru_maxrss`, converted to bytes (KiB on Linux; bytes on macOS). The JSON label is `peak_process_lifetime_not_delta`: it includes imports, initialization, buffers, earlier samples and allocations. It is not incremental model memory.

The report identifies model revision, recognizer factory, runtime architecture, CPU provider and thread count. These numbers characterize only the machine, sample list, and execution measured. A synthetic voice result is a synthetic smoke benchmark, not an accuracy claim on natural speech, Xiaomi microphones, television environments, or ARM hardware.
