"""Local Korean NLI inference with pinned weights and a reusable pair cache."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from prism_core.common.files import write_json
from prism_core.common.paths import BASE

MODEL_ID = 'Huffon/klue-roberta-base-nli'
MODEL_REVISION = '3778d23ecb30a63babb17f5efb37b1493b08d975'


def prepare_model():
    root = BASE / '.models'
    target = root / 'korean-nli'
    manifest = target / 'provenance.json'
    if (target / 'model.safetensors').exists() and manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved.get('revision') == MODEL_REVISION:
            return target
    # Only standard library model classes and weights-only deserialization are used.
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    from huggingface_hub import snapshot_download
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    print(json.dumps({'model_setup': 'downloading', 'model': MODEL_ID, 'revision': MODEL_REVISION}), flush=True)
    downloaded = snapshot_download(MODEL_ID, revision=MODEL_REVISION,
        cache_dir=str(root / 'download-cache'), allow_patterns=['*.json', '*.txt', 'pytorch_model.bin'])
    model = AutoModelForSequenceClassification.from_pretrained(
        downloaded, local_files_only=True, trust_remote_code=False, weights_only=True)
    tokenizer = AutoTokenizer.from_pretrained(downloaded, local_files_only=True, trust_remote_code=False)
    model.save_pretrained(target, safe_serialization=True)
    tokenizer.save_pretrained(target)
    write_json(manifest, {'repo': MODEL_ID, 'revision': MODEL_REVISION, 'labels': model.config.id2label})
    return target


class LocalNLI:
    def __init__(self, cache_path=None):
        self.cache_path = cache_path or BASE / 'data' / 'nli_cache.sqlite3'
        self.model = self.tokenizer = self.torch = None
        self.device = None
        self.inferred_pairs = self.cached_pairs = 0

    def _load(self):
        if self.model is not None:
            return
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        path = prepare_model()
        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        self.device = os.environ.get('PRISM_DEVICE', 'mps' if torch.backends.mps.is_available() else 'cpu')
        if self.device not in ('cpu', 'mps', 'cuda'):
            raise ValueError('PRISM_DEVICE must be cpu, mps or cuda.')
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            path, local_files_only=True, trust_remote_code=False, use_safetensors=True,
            attn_implementation='eager').to(self.device).eval()
        self.torch = torch
        labels = {str(label).lower() for label in self.model.config.id2label.values()}
        if labels != {'entailment', 'neutral', 'contradiction'}:
            raise ValueError('The NLI model has an unexpected label mapping.')
        print(json.dumps({'semantic_model': MODEL_ID, 'revision': MODEL_REVISION, 'device': self.device}), flush=True)

    def scores(self, pairs):
        if not pairs:
            return []
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        keys = [hashlib.sha256((MODEL_REVISION + '\0' + a + '\0' + b).encode()).hexdigest() for a, b in pairs]
        results, missing = {}, {}
        with sqlite3.connect(self.cache_path) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS pairs (key TEXT PRIMARY KEY, scores TEXT NOT NULL)')
            for key, pair in zip(keys, pairs):
                row = connection.execute('SELECT scores FROM pairs WHERE key=?', (key,)).fetchone()
                if row:
                    results[key] = json.loads(row[0])
                    self.cached_pairs += 1
                else:
                    missing[key] = pair
            if missing:
                self._load()
                todo = list(missing.items())
                for start in range(0, len(todo), 8):
                    batch = todo[start:start + 8]
                    encoded = self.tokenizer([p[0] for _, p in batch], [p[1] for _, p in batch], padding=True,
                                             truncation=True, max_length=384, return_tensors='pt',
                                             return_token_type_ids=False)
                    lengths = [len(self.tokenizer.encode(a, b, truncation=False)) for _, (a, b) in batch]
                    with self.torch.inference_mode():
                        probabilities = self.model(**{name: value.to(self.device) for name, value in encoded.items()}).logits.softmax(-1).cpu().tolist()
                    for ((key, _), probabilities_row, length) in zip(batch, probabilities, lengths):
                        score = {self.model.config.id2label[index].lower(): round(value, 6)
                                 for index, value in enumerate(probabilities_row)}
                        score['input_truncated'] = length > 384
                        results[key] = score
                        connection.execute('INSERT OR REPLACE INTO pairs VALUES (?, ?)', (key, json.dumps(score)))
                    connection.commit()
                    self.inferred_pairs += len(batch)
                    if start % 128 == 0:
                        print(json.dumps({'nli_pairs': min(start + 8, len(todo)), 'total': len(todo)}), flush=True)
        return [results[key] for key in keys]

    def report(self):
        return {'id': MODEL_ID, 'revision': MODEL_REVISION, 'device': self.device,
                'inferred_pairs': self.inferred_pairs, 'cached_pairs': self.cached_pairs,
                'human_validated': False, 'kind': 'korean_nli',
                'used': self.model is not None or self.cached_pairs > 0}
