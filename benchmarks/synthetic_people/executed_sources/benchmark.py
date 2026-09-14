#!/usr/bin/env python3
"""Closed synthetic-character benchmark. No real-person images are used."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent / 'outfit_matching' / 'benchmark.py'
spec = importlib.util.spec_from_file_location('outfit_harness', PARENT)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

PROMPT = '''This is a visual consistency benchmark of entirely fictional, AI-generated adult characters.
None of these images depicts a real individual. The reference names are invented labels.
You will see 15 named fictional reference characters, then one new synthetic query image.
Choose the reference character represented in the query, or UNKNOWN if the fictional
character is absent from the reference set. If the image is too unclear to decide,
answer UNCERTAIN. Do not force a nearest match.
Use visible facial appearance. Clothing is shared across characters and changes between
reference and query; clothes, office backgrounds, pose and reference order are not identity cues.
Do not infer any real-world identity, biography, occupation or sensitive attributes.
Each request is independent; the reference order is randomized. Return only JSON with
exactly label and confidence. label must be an allowed invented name, UNKNOWN or UNCERTAIN.
confidence is a number from 0 to 1 for your decision; it is not assumed calibrated.
'''


def setup():
    manifest = json.loads((HERE / 'manifest.json').read_text())
    if manifest.get('synthetic_only') is not True:
        raise ValueError('This runner requires the generated fictional dataset')
    core.HERE = HERE
    core.LABELS = [g['label'] for g in manifest['gallery']]
    core.CHOICES = core.LABELS + ['UNKNOWN', 'UNCERTAIN']
    core.PROMPT = PROMPT
    core.SCHEMA['properties']['label']['enum'] = core.CHOICES
    core.ordered_inputs = ordered_inputs
    return manifest


def ordered_inputs(manifest, case):
    items = [('text', PROMPT)]
    for index in case['gallery_order']:
        item = manifest['gallery'][index]
        items.extend([('text', f"Fictional reference character: {item['label']}"),
                      ('image', core.image_b64(item))])
    items.extend([('text', 'QUERY: which fictional reference character, UNKNOWN, or UNCERTAIN?'),
                  ('image', core.image_b64(case))])
    return items


def validate_inputs(manifest):
    assert len(manifest['gallery']) == 15
    for item in manifest['gallery'] + manifest['cases']:
        assert core.digest(HERE / item['path']) == item['sha256'], item['path']
    for source in manifest['generated_sources']:
        assert core.digest(HERE / source['path']) == source['sha256'], source['path']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['astra', 'gemini'], required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--effort', default='low')
    parser.add_argument('--run-name', required=True)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    manifest = setup()
    validate_inputs(manifest)
    out = HERE / 'results' / args.run_name
    out.mkdir(parents=True, exist_ok=True)
    provenance = {
        'synthetic_only': True,
        'adapter_sha256': core.digest(Path(__file__)),
        'harness_sha256': core.digest(PARENT),
        'manifest_sha256': core.digest(HERE / 'manifest.json'),
        'prompt_sha256': hashlib.sha256(PROMPT.encode()).hexdigest(),
        'image_mime_type': 'image/jpeg',
    }
    p = out / 'adapter.json'
    if p.exists() and json.loads(p.read_text()) != provenance:
        raise ValueError('Adapter changed; use a new run name')
    core.dump(p, provenance)
    core.run(args)


if __name__ == '__main__':
    main()
