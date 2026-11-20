"""The C7 contracts audit must fail closed, never pass on an empty suite."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('contracts', REPO/'scripts/contracts.py')
contracts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contracts)


def evidence(registry=contracts.CONTRACTS):
    """Anchors plus realistic filler so the C7 minimum counts are meaningful."""
    return dict(
        pytest_ids={item for contract in registry for item in contract.get('pytest', [])}
                   | {f'tests/test_bulk.py::test_{index}' for index in range(320)},
        e2e_titles={item for contract in registry for item in contract.get('e2e', [])}
                   | {f'filler e2e {index}' for index in range(31)},
        ui_titles={item for contract in registry for item in contract.get('ui', [])}
                  | {f'filler ui {index}' for index in range(31)},
        runner_text=' '.join(marker for contract in registry for marker in contract.get('runner_markers', [])),
        marker_texts={script: ' '.join(needles) for script, needles in contracts.GATE_MARKERS.items()},
        existing_docs={item for contract in registry for item in contract.get('docs', [])},
    )


def run(payload):
    return contracts.evaluate(contracts.CONTRACTS, pytest_ids=payload['pytest_ids'],
                              e2e_titles=payload['e2e_titles'], ui_titles=payload['ui_titles'],
                              runner_text=payload['runner_text'],
                              gate_markers=contracts.GATE_MARKERS,
                              marker_texts=payload['marker_texts'],
                              existing_docs=payload['existing_docs'])


def test_registry_is_exactly_the_seven_frozen_rows():
    assert [entry['phase'] for entry in contracts.CONTRACTS] == [f'C{index}' for index in range(1, 8)]
    for entry in contracts.CONTRACTS:
        anchors = sum(len(entry.get(key, [])) for key in ('pytest', 'e2e', 'ui', 'runner_markers', 'docs'))
        assert anchors, entry['phase']


def test_parsers_ignore_noise_and_keep_node_ids():
    collected = contracts.parse_pytest_collection(
        '\n'.join([
            'tests/test_chat_authz.py::test_owner_isolation',
            'tests/test_chat_quota.py::test_per_user[param]',
            '=' * 20 + ' warnings summary ' + '=' * 20,
            '  tests/test_noise.py::test_indented',
            '323 tests collected in 0.19s',
        ]))
    assert collected == {'tests/test_chat_authz.py::test_owner_isolation',
                         'tests/test_chat_quota.py::test_per_user[param]'}
    listed = contracts.parse_playwright_listing(
        'Listing tests:\n  chat_authz.spec.js:14:5 › C3 developer only reads own conversations\n')
    assert listed == {'C3 developer only reads own conversations'}


def test_complete_evidence_passes():
    report, ok = run(evidence())
    assert ok and report['missing'] == []
    assert [entry['covered'] for entry in report['contracts']] == [True] * 7


@pytest.mark.parametrize('drop,key', [('all', None), ('pytest', 'pytest_ids'),
                                      ('e2e', 'e2e_titles'), ('ui', 'ui_titles')])
def test_an_empty_or_shrunken_suite_fails(drop, key):
    payload = evidence()
    if key is None:
        for name in ('pytest_ids', 'e2e_titles', 'ui_titles'):
            payload[name] = set()
    else:
        payload[key] = set()
    report, ok = run(payload)
    assert not ok and report['missing']


def test_dropping_the_browser_suite_from_full_fails():
    payload = evidence()
    payload['marker_texts'] = {
        script: text.replace('browser-ui', '') if script == 'scripts/ci.py' else text
        for script, text in payload['marker_texts'].items()}
    report, ok = run(payload)
    assert not ok
    assert any('browser-ui' in item for item in report['missing'])


def test_registry_cannot_pass_with_a_missing_document():
    payload = evidence()
    payload['existing_docs'] = payload['existing_docs'] - {'docs/C7Validation.md'}
    report, ok = run(payload)
    assert not ok
    assert any('C7Validation' in item for item in report['missing'])
