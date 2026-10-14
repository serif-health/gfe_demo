from dataclasses import replace
from io import StringIO

import pytest
from rich.console import Console

from gfe_service.demo import examples as ex
from gfe_service.demo import runner


@pytest.mark.parametrize("live", [False, True])
def test_walkthrough_contains_only_additive_scenarios(settings, monkeypatch, live):
    requests = []
    monkeypatch.setattr(runner, "_post", lambda client, path, body, **kwargs: requests.append((path, body)))
    console = Console(file=StringIO(), color_system=None)
    runner.run_demo(replace(settings, neuron_api_key="test-key" if live else None), "off", False, console)
    expected = [
        ("/fhir/$gfe-estimate?_format=xml", ex.brain_mri_self_pay()),
        ("/fhir/Task/$gfe-contribute", ex.contributor_task_surgeon()),
    ]
    if live:
        expected.extend([
            ("/fhir/$gfe-estimate", ex.emerson_brain_mri_bcbsma()),
            ("/fhir/$gfe-estimate", ex.hoffman_office_visit_aetna()),
            ("/fhir/Task/$gfe-contribute", ex.contributor_task_cleveland_clinic_ct()),
        ])
    assert requests == expected


def test_generated_examples_do_not_duplicate_embedded_surgery_request(tmp_path):
    paths = ex.write_examples(tmp_path)
    assert len(paths) == 7
    assert not (tmp_path / "information-bundle-knee-arthroscopy.json").exists()
    assert (tmp_path / "contributor-task-cleveland-clinic-ct-neuron.json").exists()
