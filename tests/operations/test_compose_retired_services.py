"""The default deployment must not reintroduce retired services."""
from pathlib import Path

import yaml


def test_default_compose_excludes_retired_services_and_dependencies():
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / 'compose.yaml').read_text(encoding='utf-8'))
    services = compose['services']
    retired = {'firefly', 'firefly-cron', 'pams-collector', 'telegram-bot', 'telegram-bot-assistant'}
    assert retired.isdisjoint(services)
    for service in services.values():
        assert retired.isdisjoint(service.get('depends_on', ()))
    assert {'caddy', 'postgres', 'pams-web', 'pams-api', 'pams-scheduler'} <= services.keys()
